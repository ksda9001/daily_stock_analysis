# -*- coding: utf-8 -*-
"""DSA MCP 服务器入口。

把 :mod:`mcp_server.tools` 的业务函数注册成 MCP 工具，通过 **stdio** 暴露
给 CowAgent 等 Agent 框架。

SDK 版本兼容
------------
``mcp`` 2.x 把 ``FastMCP`` 改名为 ``MCPServer``（``mcp.server.mcpserver``）。
两者的 ``tool()`` 装饰器与 ``run(transport="stdio")`` 用法一致，
因此这里做一层兼容导入，1.x / 2.x 都能跑。

⚠️ stdio 传输下 **stdout 是 JSON-RPC 通道**。任何 ``print`` 或输出到 stdout
的日志都会破坏协议、导致客户端报「解析失败」。所以本模块的日志一律走 stderr。

用法
----
::

    export DSA_BASE_URL=http://127.0.0.1:8000
    export DSA_API_TOKEN=<用 /api/v1/tenancy/auth/token 签发>

    python -m mcp_server            # 以 stdio 启动 MCP 服务器
    python -m mcp_server --check    # 自检：连通性 + 鉴权 + 身份
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Callable, Dict, List, Optional

# 允许 `python mcp_server/server.py` 直接运行，而不仅是 `python -m mcp_server`
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _ServerBase
except ImportError:  # pragma: no cover - mcp 2.x 把 FastMCP 改名成了 MCPServer
    from mcp.server.mcpserver import MCPServer as _ServerBase

from mcp_server import tools
from mcp_server.client import DSAApiError, DSAClient, to_text

logger = logging.getLogger("dsa_mcp")

SERVER_NAME = "daily-stock-analysis"

_client: Optional[DSAClient] = None


def get_client() -> DSAClient:
    """惰性构造全局客户端（进程内复用同一个连接池）。"""
    global _client
    if _client is None:
        _client = DSAClient()
        logger.info("[mcp] base_url=%s credentials=%s", _client.base_url, _client.has_credentials)
    return _client


def _call(fn: Callable[..., Any], **kwargs: Any) -> str:
    """执行工具函数并把结果 / 异常统一转成 JSON 文本。

    失败时**返回**结构化错误而不是抛异常 —— 让模型能读到原因并自行纠正
    （例如换个参数重试），比直接给一个协议级错误更有用。
    """
    try:
        return to_text(fn(get_client(), **kwargs))
    except DSAApiError as exc:
        logger.warning("[mcp] %s 调用失败: %s", getattr(fn, "__name__", fn), exc.message)
        return to_text(exc.to_dict())
    except TypeError as exc:
        return to_text(
            {
                "ok": False,
                "error": "bad_arguments",
                "message": f"参数不正确：{exc}",
            }
        )
    except Exception as exc:  # noqa: BLE001 —— 兜底，绝不让工具把服务器打挂
        logger.exception("[mcp] %s 未预期异常", getattr(fn, "__name__", fn))
        return to_text({"ok": False, "error": "internal_error", "message": str(exc)})


server = _ServerBase(
    name=SERVER_NAME,
    instructions=(
        "daily_stock_analysis 的 MCP 工具集。可以触发 AI 分析、管理自选股、"
        "执行选股、问股、读取决策信号与 LLM 用量。\n\n"
        "所有数据都归属于当前 Token 对应的账号，不会看到其他用户的数据。\n"
        "自选股请使用 add_to_watchlist / remove_from_watchlist（按用户存储），"
        "不要使用任何写全局 STOCK_LIST 的接口。"
    ),
)


# ---------------------------------------------------------------------------
# 身份与配置
# ---------------------------------------------------------------------------

@server.tool()
def whoami() -> str:
    """查看当前身份：用户名、角色、租户 ID。

    调用其它工具前可先用它确认 Token 对应的是哪个账号。
    """
    return _call(tools.whoami)


@server.tool()
def get_my_settings() -> str:
    """读取当前账号的个人配置（自选股、报告类型、推送渠道等；敏感项已掩码）。"""
    return _call(tools.get_my_settings)


@server.tool()
def update_my_settings(settings: Dict[str, Any]) -> str:
    """更新当前账号的个人配置。

    :param settings: 键值对，例如 {"REPORT_TYPE": "brief", "SCHEDULE_TIMES": "18:00"}。
        只接受业务偏好与推送目标；平台密钥、数据库路径等会被服务端拒绝。
    """
    return _call(tools.update_my_settings, settings=settings)


# ---------------------------------------------------------------------------
# 1. 分析
# ---------------------------------------------------------------------------

@server.tool()
def analyze_stocks(
    stock_codes: List[str],
    report_type: str = "detailed",
    force_refresh: bool = False,
    async_mode: bool = False,
) -> str:
    """触发 AI 股票分析。

    :param stock_codes: 股票代码列表，如 ["600519", "000858"]；留空表示分析当前自选股
    :param report_type: simple（精简）/ detailed（完整）/ full / brief（简洁）
    :param force_refresh: 忽略缓存强制重跑
    :param async_mode: True 时立即返回 task_id，稍后用 get_analysis_task 查结果
    """
    return _call(
        tools.analyze_stocks,
        stock_codes=stock_codes,
        report_type=report_type,
        force_refresh=force_refresh,
        async_mode=async_mode,
    )


@server.tool()
def get_analysis_task(task_id: str) -> str:
    """查询异步分析任务的进度与结果。

    :param task_id: analyze_stocks(async_mode=True) 返回的任务 ID
    """
    return _call(tools.get_analysis_task, task_id=task_id)


@server.tool()
def list_analysis_history(
    stock_code: Optional[str] = None,
    report_type: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    page: int = 1,
    limit: int = 20,
) -> str:
    """分页查询历史分析记录（只含当前账号自己的）。

    :param stock_code: 按股票代码筛选
    :param report_type: 按报告类型筛选，如 market_review
    :param start_date: 起始日期 YYYY-MM-DD
    :param end_date: 结束日期 YYYY-MM-DD
    :param page: 页码，从 1 开始
    :param limit: 每页条数，1-100
    """
    return _call(
        tools.list_analysis_history,
        stock_code=stock_code,
        report_type=report_type,
        start_date=start_date,
        end_date=end_date,
        page=page,
        limit=limit,
    )


@server.tool()
def get_analysis_report(record_id: int, max_chars: int = 24000) -> str:
    """按记录 ID 读取完整分析报告。

    :param record_id: list_analysis_history 返回的记录 ID
    :param max_chars: 内容截断上限，默认 24000 字符
    """
    return _call(tools.get_analysis_report, record_id=record_id, max_chars=max_chars)


@server.tool()
def get_report_markdown(record_id: int, max_chars: int = 24000) -> str:
    """按记录 ID 读取报告的 Markdown 原文（适合转发 / 存档）。"""
    return _call(tools.get_report_markdown, record_id=record_id, max_chars=max_chars)


# ---------------------------------------------------------------------------
# 2. 自选股
# ---------------------------------------------------------------------------

@server.tool()
def get_watchlist() -> str:
    """读取当前账号的自选股。

    source=global 表示该账号还没有个人列表，当前看到的是全局配置。
    """
    return _call(tools.get_watchlist)


@server.tool()
def add_to_watchlist(stock_code: str) -> str:
    """把股票加入当前账号的自选股。

    若此前沿用全局列表，会先继承再追加，不会冲掉已有自选。

    :param stock_code: 股票代码，如 600519
    """
    return _call(tools.add_to_watchlist, stock_code=stock_code)


@server.tool()
def remove_from_watchlist(stock_code: str) -> str:
    """从当前账号的自选股中移除某只股票。"""
    return _call(tools.remove_from_watchlist, stock_code=stock_code)


@server.tool()
def replace_watchlist(stock_codes: List[str]) -> str:
    """用给定列表整体替换当前账号的自选股（会覆盖原有内容）。"""
    return _call(tools.replace_watchlist, stock_codes=stock_codes)


# ---------------------------------------------------------------------------
# 3. 选股
# ---------------------------------------------------------------------------

@server.tool()
def list_screening_strategies() -> str:
    """列出可用的选股策略及其适用市场。调用 run_screening 前先看这个。"""
    return _call(tools.list_screening_strategies)


@server.tool()
def run_screening(
    strategy: str = "dual_low", market: str = "cn", max_results: int = 20
) -> str:
    """执行选股（同步，可能耗时较久）。

    :param strategy: 策略 ID，见 list_screening_strategies
    :param market: 市场，cn / hk / us / jp / kr / tw
    :param max_results: 返回条数上限，1-100
    """
    return _call(
        tools.run_screening, strategy=strategy, market=market, max_results=max_results
    )


@server.tool()
def get_screening_history(limit: int = 20, strategy: str = "", market: str = "") -> str:
    """查询历史选股记录（只含当前账号自己的）。"""
    return _call(
        tools.get_screening_history, limit=limit, strategy=strategy, market=market
    )


@server.tool()
def get_market_hotspots(topic: str = "") -> str:
    """获取市场热点 / 题材列表；传 topic 取该题材详情。"""
    return _call(tools.get_market_hotspots, topic=topic)


# ---------------------------------------------------------------------------
# 4. 问股
# ---------------------------------------------------------------------------

@server.tool()
def ask_stock_question(
    message: str, session_id: Optional[str] = None, max_chars: int = 24000
) -> str:
    """向 DSA 的 Agent 提问（问股 / 追问）。

    :param message: 问题内容
    :param session_id: 传上次的会话 ID 可延续上下文；不传则新建会话
    """
    return _call(
        tools.ask_stock_question,
        message=message,
        session_id=session_id,
        max_chars=max_chars,
    )


@server.tool()
def list_chat_sessions(limit: int = 20) -> str:
    """列出当前账号的历史问股会话。"""
    return _call(tools.list_chat_sessions, limit=limit)


@server.tool()
def get_chat_messages(session_id: str, max_chars: int = 24000) -> str:
    """读取某个问股会话的完整消息记录。"""
    return _call(tools.get_chat_messages, session_id=session_id, max_chars=max_chars)


# ---------------------------------------------------------------------------
# 5. AI 建议
# ---------------------------------------------------------------------------

@server.tool()
def get_decision_signal(stock_code: str) -> str:
    """取某只股票最新的决策信号（AI 建议：动作、理由、有效期）。"""
    return _call(tools.get_decision_signal, stock_code=stock_code)


@server.tool()
def list_decision_signals(
    stock_code: Optional[str] = None,
    market: Optional[str] = None,
    action: Optional[str] = None,
    limit: int = 20,
) -> str:
    """查询决策信号列表（只含当前账号自己的）。"""
    return _call(
        tools.list_decision_signals,
        stock_code=stock_code,
        market=market,
        action=action,
        limit=limit,
    )


@server.tool()
def run_deep_research(
    question: str, stock_code: Optional[str] = None, max_chars: int = 24000
) -> str:
    """跑一次深度研究（会检索外部资料，比普通问股更慢更重）。"""
    return _call(
        tools.run_deep_research,
        question=question,
        stock_code=stock_code,
        max_chars=max_chars,
    )


# ---------------------------------------------------------------------------
# 6. 用量监控
# ---------------------------------------------------------------------------

@server.tool()
def get_my_usage(limit_days: int = 30) -> str:
    """当前账号的 LLM 用量统计（按账号归属）。"""
    return _call(tools.get_my_usage, limit_days=limit_days)


@server.tool()
def get_usage_summary(period: str = "month") -> str:
    """LLM 用量汇总。period: today / month / all。"""
    return _call(tools.get_usage_summary, period=period)


# ---------------------------------------------------------------------------
# 行情辅助
# ---------------------------------------------------------------------------

@server.tool()
def get_stock_quote(stock_code: str) -> str:
    """取实时行情（价格、涨跌幅等）。"""
    return _call(tools.get_stock_quote, stock_code=stock_code)


@server.tool()
def get_stock_profile(stock_code: str) -> str:
    """取股票基本面画像。"""
    return _call(tools.get_stock_profile, stock_code=stock_code)


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def _configure_logging() -> None:
    """日志一律写 stderr —— stdout 留给 JSON-RPC。"""
    level = os.environ.get("DSA_MCP_LOG_LEVEL", "WARNING").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.WARNING),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _check(client: Optional[DSAClient] = None) -> int:
    """自检：能否连上 DSA、Token 是否有效、对应哪个账号。"""
    client = client or DSAClient()
    print(f"[*] 目标: {client.base_url}", file=sys.stderr)
    if not client.has_credentials:
        print(
            "[x] 未提供凭据。请设置 DSA_API_TOKEN，或 DSA_USERNAME + DSA_PASSWORD。",
            file=sys.stderr,
        )
        return 2
    try:
        payload = client.get("/api/v1/tenancy/auth/me")
    except DSAApiError as exc:
        print(f"[x] 失败: {exc.message}", file=sys.stderr)
        if exc.code:
            print(f"    错误码: {exc.code}", file=sys.stderr)
        return 1

    user = (payload or {}).get("user") or {}
    print("[✓] 连接与鉴权正常", file=sys.stderr)
    print(
        f"    账号: {user.get('username')} "
        f"(id={user.get('id')}, role={user.get('role')})",
        file=sys.stderr,
    )
    try:
        watchlist = client.get("/api/v1/tenancy/watchlist")
        codes = (watchlist or {}).get("stock_codes") or []
        print(
            f"    自选股: {len(codes)} 只 (来源={(watchlist or {}).get('source')})",
            file=sys.stderr,
        )
    except DSAApiError as exc:
        print(f"    [!] 自选股读取失败: {exc.message}", file=sys.stderr)
    return 0


def main() -> None:
    _configure_logging()
    argv = sys.argv[1:]
    if "--check" in argv:
        raise SystemExit(_check())
    logger.info("[mcp] 启动 %s（stdio）", SERVER_NAME)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
