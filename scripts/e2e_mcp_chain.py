#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""端到端验证：MCP 服务器 → 真实 DSA → 按用户隔离。

单元测试各自验证了一半：

* ``tests/test_tenancy_api.py`` 验证了 DSA 的隔离（用 TestClient，不经过 MCP）
* ``tests/test_mcp_server.py`` 验证了 MCP 工具打出的请求（用 MockTransport，不经过真实 DSA）

**没有任何一个测试证明这两半接起来是对的。** 这个脚本补上那一环：真的起一个
DSA 服务，真的用 stdio 拉起 MCP 服务器，真的走 HTTP 带 Bearer Token 调过去，
然后断言两个用户互相看不到对方的数据。

用法::

    python scripts/e2e_mcp_chain.py
    python scripts/e2e_mcp_chain.py --verbose   # 打印服务端日志

退出码 0 = 全通，1 = 有断言失败。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent

ADMIN_PASSWORD = "verify-admin-123"
ALICE_PASSWORD = "verify-alice-123"
BOB_PASSWORD = "verify-bob-123"

#: 两个用户各自操作的自选股，用来验证隔离
ALICE_CODE = "600519"
BOB_CODE = "000858"


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

class Report:
    def __init__(self) -> None:
        self.passed: List[str] = []
        self.failed: List[str] = []

    def check(self, label: str, ok: bool, detail: str = "") -> bool:
        if ok:
            self.passed.append(label)
            print(f"  \u2713 {label}")
        else:
            self.failed.append(label)
            print(f"  \u2717 {label}" + (f"\n      {detail}" if detail else ""))
        return ok

    def section(self, title: str) -> None:
        print(f"\n{title}")

    def summary(self) -> int:
        total = len(self.passed) + len(self.failed)
        print("\n" + "=" * 60)
        if self.failed:
            print(f"结果: {len(self.passed)}/{total} 通过，{len(self.failed)} 失败")
            for item in self.failed:
                print(f"  \u2717 {item}")
            return 1
        print(f"结果: {total}/{total} 全部通过 \u2713")
        return 0


# ---------------------------------------------------------------------------
# DSA 服务
# ---------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_dsa(workdir: Path, port: int, log_path: Path) -> Tuple[subprocess.Popen, Any]:
    """启动 DSA。

    ⚠️ ``ADMIN_AUTH_ENABLED`` **不是**从环境变量读的 ——
    ``src/auth.py::_is_auth_enabled_from_env()`` 直接 ``dotenv_values()`` 解析
    ``.env`` 文件。所以光在 env 里设它没用，必须落成文件。
    这里用 ``ENV_FILE`` 指向临时文件（该变量在 ``src/auth.py`` 与
    ``src/config.py`` 里都被尊重），避免污染仓库目录。
    """
    env_file = workdir / "verify.env"
    env_file.write_text(
        "\n".join(
            [
                f"DATABASE_PATH={workdir / 'verify.db'}",
                "DSA_MULTIUSER_ENABLED=true",
                "ADMIN_AUTH_ENABLED=true",
                "",
            ]
        ),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.update(
        {
            "ENV_FILE": str(env_file),
            "DATABASE_PATH": str(workdir / "verify.db"),
            "DSA_MULTIUSER_ENABLED": "true",
            "ADMIN_AUTH_ENABLED": "true",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        }
    )
    log_file = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    return proc, log_file


def wait_for_health(base_url: str, proc: subprocess.Popen, timeout: float = 180.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            resp = httpx.get(f"{base_url}/api/v1/health", timeout=3.0)
            if resp.status_code < 500:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1.0)
    return False


def print_log_tail(log_path: Path, lines: int = 30) -> None:
    """打印服务端日志尾部。

    启动失败时**自动**调用 —— 十有八九是缺依赖或端口被占，
    让人再手动加 --verbose 重跑一遍纯属浪费时间。
    """
    if not log_path.exists():
        return
    content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not content:
        return
    print("\n--- DSA 服务端日志（尾部）---")
    print("\n".join(content[-lines:]))
    print("--- 日志结束 ---")


# ---------------------------------------------------------------------------
# MCP 会话
# ---------------------------------------------------------------------------

@contextlib.asynccontextmanager
async def mcp_session(base_url: str, token: str):
    """以 stdio 拉起 MCP 服务器。

    ⚠️ 这里**显式**传 env —— 实测 MCP 子进程不继承父进程环境变量，
    这也是 CowAgent 的 mcp.json 必须写 env 字段的原因。
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = os.environ.copy()
    env.update({"DSA_BASE_URL": base_url, "DSA_API_TOKEN": token})
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server"],
        cwd=str(REPO_ROOT),
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def mcp_call(session, name: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    result = await session.call_tool(name, args or {})
    text = result.content[0].text if result.content else ""
    try:
        payload = json.loads(text)
    except ValueError:
        payload = {"_raw": text}
    if getattr(result, "isError", False):
        payload.setdefault("_protocol_error", True)
    return payload


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def provision_users(base_url: str, report: Report) -> Dict[str, str]:
    """设置管理员密码 → 建两个用户 → 各自签发 Token。"""
    report.section("① 准备账号")
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        resp = client.post(
            "/api/v1/auth/login",
            json={"password": ADMIN_PASSWORD, "passwordConfirm": ADMIN_PASSWORD},
        )
        report.check(
            "管理员密码设置成功",
            resp.status_code < 400,
            f"HTTP {resp.status_code}: {resp.text[:200]}",
        )

        resp = client.post(
            "/api/v1/tenancy/auth/token",
            json={"username": "admin", "password": ADMIN_PASSWORD},
        )
        if not report.check(
            "管理员换取 Token",
            resp.status_code == 200 and "token" in resp.json(),
            f"HTTP {resp.status_code}: {resp.text[:200]}",
        ):
            return {}
        admin_token = resp.json()["token"]
        admin_headers = {"Authorization": f"Bearer {admin_token}"}

        tokens: Dict[str, str] = {"admin": admin_token}
        for name, password in (("alice", ALICE_PASSWORD), ("bob", BOB_PASSWORD)):
            created = client.post(
                "/api/v1/tenancy/users",
                headers=admin_headers,
                json={"username": name, "password": password, "display_name": name.title()},
            )
            report.check(
                f"创建用户 {name}",
                created.status_code < 400,
                f"HTTP {created.status_code}: {created.text[:200]}",
            )

            issued = client.post(
                "/api/v1/tenancy/auth/token",
                json={"username": name, "password": password},
            )
            ok = issued.status_code == 200 and "token" in issued.json()
            report.check(
                f"{name} 换取 Token",
                ok,
                f"HTTP {issued.status_code}: {issued.text[:200]}",
            )
            if ok:
                tokens[name] = issued.json()["token"]
        return tokens


async def verify_chain(base_url: str, tokens: Dict[str, str], report: Report) -> None:
    if not {"alice", "bob"} <= tokens.keys():
        report.check("拿不到两个用户的 Token，跳过后续", False, "账号准备失败")
        return

    # -- 身份 ---------------------------------------------------------------
    report.section("② MCP 身份识别（Token → 用户）")
    alice_id: Optional[int] = None
    async with mcp_session(base_url, tokens["alice"]) as session:
        tools = await session.list_tools()
        report.check("MCP 工具全部注册", len(tools.tools) == 26, f"实际 {len(tools.tools)} 个")

        alice_me = await mcp_call(session, "whoami")
        report.check(
            "alice 的 Token 解析为 alice",
            (alice_me.get("user") or {}).get("username") == "alice",
            json.dumps(alice_me, ensure_ascii=False)[:200],
        )
        alice_id = (alice_me.get("user") or {}).get("id")

        alice_watch_before = await mcp_call(session, "get_watchlist")
        report.check(
            "alice 能读到自选股",
            "stock_codes" in alice_watch_before,
            json.dumps(alice_watch_before, ensure_ascii=False)[:200],
        )

        # -- 写入 -----------------------------------------------------------
        added = await mcp_call(session, "add_to_watchlist", {"stock_code": ALICE_CODE})
        report.check(
            f"alice 加入自选 {ALICE_CODE}",
            added.get("ok") is True and ALICE_CODE in (added.get("stock_codes") or []),
            json.dumps(added, ensure_ascii=False)[:300],
        )
        report.check(
            "写入落到「个人」而非「全局」",
            added.get("source") == "user",
            f"source={added.get('source')}（应为 user）",
        )

    # -- 隔离（核心） -------------------------------------------------------
    report.section("③ 跨用户隔离（本脚本存在的意义）")
    async with mcp_session(base_url, tokens["bob"]) as session:
        bob_me = await mcp_call(session, "whoami")
        report.check(
            "bob 的 Token 解析为 bob",
            (bob_me.get("user") or {}).get("username") == "bob",
            json.dumps(bob_me, ensure_ascii=False)[:200],
        )

        bob_watch = await mcp_call(session, "get_watchlist")
        bob_codes = bob_watch.get("stock_codes") or []
        report.check(
            f"bob 看不到 alice 的自选 {ALICE_CODE}",
            ALICE_CODE not in bob_codes,
            f"bob 的自选 = {bob_codes} —— 数据串号了！",
        )

        added_bob = await mcp_call(session, "add_to_watchlist", {"stock_code": BOB_CODE})
        report.check(
            f"bob 加入自选 {BOB_CODE}",
            added_bob.get("ok") is True and BOB_CODE in (added_bob.get("stock_codes") or []),
            json.dumps(added_bob, ensure_ascii=False)[:300],
        )

    async with mcp_session(base_url, tokens["alice"]) as session:
        alice_watch = await mcp_call(session, "get_watchlist")
        alice_codes = alice_watch.get("stock_codes") or []
        report.check(
            f"alice 的自选仍含 {ALICE_CODE}",
            ALICE_CODE in alice_codes,
            f"alice 的自选 = {alice_codes}",
        )
        report.check(
            f"alice 看不到 bob 的自选 {BOB_CODE}",
            BOB_CODE not in alice_codes,
            f"alice 的自选 = {alice_codes} —— 数据串号了！",
        )

        removed = await mcp_call(session, "remove_from_watchlist", {"stock_code": ALICE_CODE})
        report.check(
            "alice 移除自选生效",
            removed.get("ok") is True and ALICE_CODE not in (removed.get("stock_codes") or []),
            json.dumps(removed, ensure_ascii=False)[:300],
        )

    async with mcp_session(base_url, tokens["bob"]) as session:
        bob_after = await mcp_call(session, "get_watchlist")
        report.check(
            "alice 的移除没有影响 bob",
            BOB_CODE in (bob_after.get("stock_codes") or []),
            json.dumps(bob_after, ensure_ascii=False)[:300],
        )

    # -- 用量归属 -----------------------------------------------------------
    report.section("④ 用量按用户归属")
    async with mcp_session(base_url, tokens["alice"]) as session:
        usage = await mcp_call(session, "get_my_usage", {"limit_days": 7})
        report.check(
            "alice 的用量归属到 alice 本人",
            isinstance(usage, dict) and usage.get("tenant_id") == alice_id,
            f"tenant_id={usage.get('tenant_id')}，应为 alice 的 id={alice_id}",
        )
        report.check(
            "用量包含汇总字段",
            isinstance(usage.get("totals"), dict),
            json.dumps(usage, ensure_ascii=False)[:200],
        )

    # -- 越权与错误处理 -----------------------------------------------------
    report.section("⑤ 越权与错误处理")
    async with mcp_session(base_url, tokens["alice"]) as session:
        bad = await mcp_call(session, "add_to_watchlist", {"stock_code": "!!!invalid!!!"})
        report.check(
            "非法股票代码被拒绝且不崩",
            bad.get("ok") is False,
            json.dumps(bad, ensure_ascii=False)[:200],
        )

    async with mcp_session(base_url, "v1.999.1.9999999999.abc.deadbeef") as session:
        forged = await mcp_call(session, "whoami")
        report.check(
            "伪造 Token 被拒绝",
            forged.get("ok") is False,
            json.dumps(forged, ensure_ascii=False)[:200],
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="端到端验证 MCP → DSA → 多租户隔离")
    parser.add_argument("--verbose", action="store_true", help="结束时打印 DSA 服务端日志")
    args = parser.parse_args()

    if importlib.util.find_spec("mcp") is None:
        print("缺少 mcp SDK：pip install -r mcp_server/requirements.txt", file=sys.stderr)
        return 2

    report = Report()
    workdir = Path(tempfile.mkdtemp(prefix="dsa-verify-"))
    log_path = workdir / "dsa-server.log"
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"

    print("=" * 60)
    print("端到端验证：MCP 服务器 → DSA → 多租户隔离")
    print(f"工作目录: {workdir}")
    print(f"服务地址: {base_url}")
    print("=" * 60)

    proc = None
    log_file = None
    try:
        report.section("⓪ 启动 DSA（多用户模式）")
        proc, log_file = start_dsa(workdir, port, log_path)
        healthy = wait_for_health(base_url, proc)
        if not report.check(
            "DSA 启动并健康",
            healthy,
            "启动超时或进程退出，见下方日志",
        ):
            print_log_tail(log_path)
            return report.summary()

        tokens = provision_users(base_url, report)
        asyncio.run(verify_chain(base_url, tokens, report))
        return report.summary()

    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if log_file is not None:
            log_file.close()
        if args.verbose and log_path.exists():
            print("\n--- DSA 服务端日志（尾部）---")
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            print("\n".join(lines[-40:]))
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
