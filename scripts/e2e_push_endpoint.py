#!/usr/bin/env python3
"""端到端验证：DSA 行情推送端点（三态语义）。

验证 ``POST /api/v1/tenancy/push/digest`` 的三种情形：

1. 无 token            -> 401
2. 有 token 且未到点   -> ``{"skip": true, "reason": "missed_window", ...}``
                          **不是** 500 / 报错
3. ``force=true``      -> 真实 digest，含 ``indices`` 与 ``watchlist`` 两段

用法（在 dsa-server 所在的宿主机上）::

    python3 e2e_push_endpoint.py

依赖：本机可访问 ``http://127.0.0.1:8000``，且能 ``docker exec dsa-server``。

设计说明
--------
- 端点要求 ``wechat_id`` 或 ``tenant_id`` **之一**，缺了会返回
  ``{"ok": false, "error": "missing_target"}``。本脚本用 ``tenant_id``。
- 铸 token 走应用自己的 ``src.tenancy.tokens.issue_token``（不改任何数据）。
- 输出里不打印 token 明文，只打印长度与前缀。

参见 ``docs/2026-09-14-push-feature-deploy.md`` §5.2。
"""
import json
import subprocess
import sys

BASE = "http://127.0.0.1:8000"

# 端点要求 wechat_id 或 tenant_id 之一。
# 用 cowagent 服务账号（dsa_users.id=2）作为目标租户。
TENANT_ID = 2


def sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180)
    return r.stdout.strip(), r.stderr.strip(), r.returncode


def main():
    out = {}

    # --- 1. 无 token ---
    stdout, _, _ = sh(
        f"curl -s -o /dev/null -w '%{{http_code}}' -X POST {BASE}/api/v1/tenancy/push/digest "
        f"-H 'Content-Type: application/json' -d '{{}}'"
    )
    out["no_token_http"] = stdout
    print(f"[1] 无 token -> HTTP {stdout}  (期望 401)")

    # --- 铸 token（在 dsa-server 容器内）---
    mint = (
        "docker exec dsa-server sh -c 'cd /app && python -c \""
        "import sys; sys.path.insert(0, \\\"/app\\\"); "
        "from src.tenancy.tokens import issue_token; "
        "print(issue_token(2, 1, ttl_seconds=300))\"'"
    )
    token, err, rc = sh(mint)
    if rc != 0 or not token.startswith("v1."):
        print("FAIL 铸 token 失败:", err[:400])
        return 1
    print(f"[*] token 已铸：长度 {len(token)}，前缀 {token[:12]}…")

    # --- 2. 有 token，未到点 ---
    body2, _, _ = sh(
        f"curl -s -X POST {BASE}/api/v1/tenancy/push/digest "
        f"-H 'Content-Type: application/json' "
        f"-H 'Authorization: Bearer {token}' -d '{{\"tenant_id\": {TENANT_ID}}}'"
    )
    print(f"[2] 带 token（未 force）-> {body2[:600]}")
    out["with_token"] = body2

    # --- 3. force=true ---
    body3, _, _ = sh(
        f"curl -s -X POST {BASE}/api/v1/tenancy/push/digest "
        f"-H 'Content-Type: application/json' "
        f"-H 'Authorization: Bearer {token}' -d '{{\"tenant_id\": {TENANT_ID}, \"force\": true}}'"
    )
    print(f"[3] force=true -> {body3[:2500]}")
    out["forced"] = body3

    # --- 判据 ---
    print("\n=== 判据 ===")
    ok = True

    def chk(cond, msg):
        nonlocal ok
        print(("  PASS  " if cond else "  FAIL  ") + msg)
        if not cond:
            ok = False

    chk(out["no_token_http"] == "401", f"无 token 返回 401（实际 {out['no_token_http']}）")

    try:
        b2 = json.loads(body2)
        chk(isinstance(b2, dict), "带 token 返回 JSON 对象")
        chk("skip" in b2 or b2.get("skip") is not None,
            f"未到点返回 skip 字段（实际 keys={list(b2.keys())}）")
        chk("error" not in b2 and "detail" not in b2,
            f"未到点不是错误响应（keys={list(b2.keys())}）")
    except Exception as e:
        chk(False, f"带 token 响应非 JSON：{e}；原文={body2[:200]}")

    try:
        b3 = json.loads(body3)
        keys3 = list(b3.keys())
        print(f"  [info] force 响应 keys = {keys3}")
        has_indices = "indices" in b3 or any("indices" in str(k) for k in keys3)
        has_watch = "watchlist" in b3 or any("watchlist" in str(k) for k in keys3)
        chk("error" not in b3, "force=true 未报错")
        chk(has_indices, "force 响应含 indices 段")
        chk(has_watch, "force 响应含 watchlist 段")
        if not has_watch:
            print("  [note] watchlist 缺失 —— 若该用户自选股为空则属正常")
    except Exception as e:
        chk(False, f"force 响应非 JSON：{e}；原文={body3[:300]}")

    print("\n结果：" + ("全部通过" if ok else "存在失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
