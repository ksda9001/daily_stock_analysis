# -*- coding: utf-8 -*-
"""MCP 工具的业务实现。

**刻意不 import MCP SDK** —— 这里的函数是纯 Python，接收 :class:`DSAClient`
返回 ``dict``。这样做的好处：

* 单元测试可以直接调用，不需要起 MCP 会话；
* 换 Agent 框架（CowAgent / 其他）时业务逻辑不用重写。

约定
----
* 成功返回 ``dict``（含业务字段）；
* 失败**抛出** :class:`~mcp_server.client.DSAApiError`，由服务层统一转成
  结构化错误 —— 这样工具内部不用写一堆 ``try/except``。

⚠️ 自选股一律走 ``/api/v1/tenancy/watchlist``（按用户），
**不要**改用上游的 ``/api/v1/stocks/watchlist/*`` —— 那条路径写全局配置，会串号。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from mcp_server.client import DSAClient, truncate

TENANCY = "/api/v1/tenancy"


# ---------------------------------------------------------------------------
# 身份与配置
# ---------------------------------------------------------------------------

def whoami(client: DSAClient) -> Dict[str, Any]:
    """当前 Token 对应的用户身份与权限。"""
    return client.get(f"{TENANCY}/auth/me")


def get_my_settings(client: DSAClient) -> Dict[str, Any]:
    """读取当前用户的个人配置（敏感项已掩码）。"""
    return client.get(f"{TENANCY}/settings")


def update_my_settings(client: DSAClient, settings: Dict[str, Any]) -> Dict[str, Any]:
    """更新当前用户的个人配置。

    只接受业务偏好与推送目标（自选股、报告类型、推送 Webhook 等）；
    平台密钥、数据库路径等基础设施配置会被服务端拒绝。
    """
    return client.put(f"{TENANCY}/settings", json_body={"settings": settings})


# ---------------------------------------------------------------------------
# 1. 分析
# ---------------------------------------------------------------------------

def analyze_stocks(
    client: DSAClient,
    stock_codes: List[str],
    *,
    report_type: str = "detailed",
    force_refresh: bool = False,
    async_mode: bool = False,
) -> Dict[str, Any]:
    """触发 AI 分析。

    :param stock_codes: 股票代码列表；空列表表示分析当前自选股
    :param report_type: simple / detailed / full / brief
    :param async_mode: 为 True 时立即返回 task_id，稍后用 get_analysis_task 查
    """
    codes = [str(code).strip() for code in (stock_codes or []) if str(code).strip()]
    body: Dict[str, Any] = {
        "report_type": report_type,
        "force_refresh": bool(force_refresh),
        "async_mode": bool(async_mode),
    }
    if len(codes) == 1:
        body["stock_code"] = codes[0]
    elif codes:
        body["stock_codes"] = codes

    return client.post("/api/v1/analysis/analyze", json_body=body, timeout=900.0)


def get_analysis_task(client: DSAClient, task_id: str) -> Dict[str, Any]:
    """查询异步分析任务的进度与结果。"""
    return client.get(f"/api/v1/analysis/status/{task_id}")


def list_analysis_history(
    client: DSAClient,
    *,
    stock_code: Optional[str] = None,
    report_type: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    page: int = 1,
    limit: int = 20,
) -> Dict[str, Any]:
    """分页查询历史分析记录摘要（只返回当前用户自己的）。"""
    params: Dict[str, Any] = {"page": page, "limit": limit}
    if stock_code:
        params["stock_code"] = stock_code
    if report_type:
        params["report_type"] = report_type
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    return client.get("/api/v1/history", params=params)


def get_analysis_report(
    client: DSAClient, record_id: int, *, max_chars: int = 24000
) -> Dict[str, Any]:
    """按记录 ID 取完整分析报告。

    报告可能很长，默认截断到 24000 字符；需要更多可调大 ``max_chars``。
    """
    payload = client.get(f"/api/v1/history/{record_id}")
    return _limit_payload(payload, max_chars)


def get_report_markdown(
    client: DSAClient, record_id: int, *, max_chars: int = 24000
) -> Dict[str, Any]:
    """按记录 ID 取报告的 Markdown 原文。"""
    payload = client.get(f"/api/v1/history/{record_id}/markdown")
    return _limit_payload(payload, max_chars)


# ---------------------------------------------------------------------------
# 2. 自选股（按用户）
# ---------------------------------------------------------------------------

def get_watchlist(client: DSAClient) -> Dict[str, Any]:
    """读取当前用户的自选股。

    ``source=global`` 表示该用户还没有个人列表，当前看到的是全局配置。
    """
    return client.get(f"{TENANCY}/watchlist")


def add_to_watchlist(client: DSAClient, stock_code: str) -> Dict[str, Any]:
    """把股票加入当前用户的自选股。

    若该用户此前沿用全局列表，会先继承全局列表再追加 —— 不会把已有自选冲掉。
    """
    return client.post(f"{TENANCY}/watchlist", json_body={"stock_code": stock_code})


def remove_from_watchlist(client: DSAClient, stock_code: str) -> Dict[str, Any]:
    """从当前用户的自选股中移除。"""
    code = str(stock_code).strip()
    return client.delete(f"{TENANCY}/watchlist/{code}")


def replace_watchlist(client: DSAClient, stock_codes: List[str]) -> Dict[str, Any]:
    """整体替换当前用户的自选股。"""
    return client.put(
        f"{TENANCY}/watchlist",
        json_body={"stock_codes": [str(c).strip() for c in (stock_codes or [])]},
    )


# ---------------------------------------------------------------------------
# 3. 选股
# ---------------------------------------------------------------------------

def list_screening_strategies(client: DSAClient) -> Dict[str, Any]:
    """列出可用的选股策略（含各自适用的市场）。"""
    return client.get("/api/v1/screening/strategies")


def run_screening(
    client: DSAClient,
    *,
    strategy: str = "dual_low",
    market: str = "cn",
    max_results: int = 20,
) -> Dict[str, Any]:
    """执行选股。同步返回结果，可能耗时较久。"""
    return client.post(
        "/api/v1/screening/screen",
        json_body={
            "strategy": strategy,
            "market": market,
            "max_results": int(max_results),
        },
        timeout=900.0,
    )


def get_screening_history(
    client: DSAClient, *, limit: int = 20, strategy: str = "", market: str = ""
) -> Dict[str, Any]:
    """查询历史选股记录（只返回当前用户自己的）。"""
    params: Dict[str, Any] = {"limit": limit}
    if strategy:
        params["strategy"] = strategy
    if market:
        params["market"] = market
    return client.get("/api/v1/screening/history", params=params)


def get_market_hotspots(client: DSAClient, *, topic: str = "") -> Dict[str, Any]:
    """获取市场热点 / 题材。"""
    if topic:
        return client.get(f"/api/v1/screening/hotspots/{topic.strip('/')}")
    return client.get("/api/v1/screening/hotspots")


# ---------------------------------------------------------------------------
# 4. 问股（对话）
# ---------------------------------------------------------------------------

def ask_stock_question(
    client: DSAClient,
    message: str,
    *,
    session_id: Optional[str] = None,
    max_chars: int = 24000,
) -> Dict[str, Any]:
    """向 DSA 的 Agent 提问（问股 / 追问）。

    传 ``session_id`` 可延续上一轮对话；不传则新建会话。
    """
    body: Dict[str, Any] = {"message": message}
    if session_id:
        body["session_id"] = session_id
    payload = client.post("/api/v1/agent/chat", json_body=body, timeout=900.0)
    return _limit_payload(payload, max_chars)


def list_chat_sessions(client: DSAClient, *, limit: int = 20) -> Dict[str, Any]:
    """列出当前用户的问股会话。"""
    return client.get("/api/v1/agent/chat/sessions", params={"limit": limit})


def get_chat_messages(
    client: DSAClient, session_id: str, *, max_chars: int = 24000
) -> Dict[str, Any]:
    """读取某个会话的完整消息记录。"""
    payload = client.get(f"/api/v1/agent/chat/sessions/{session_id}")
    return _limit_payload(payload, max_chars)


# ---------------------------------------------------------------------------
# 5. AI 建议
# ---------------------------------------------------------------------------

def get_decision_signal(client: DSAClient, stock_code: str) -> Dict[str, Any]:
    """取某只股票最新的决策信号（AI 建议：买/卖/持有及理由）。"""
    return client.get(f"/api/v1/decision-signals/latest/{stock_code}")


def list_decision_signals(
    client: DSAClient,
    *,
    stock_code: Optional[str] = None,
    market: Optional[str] = None,
    action: Optional[str] = None,
    limit: int = 20,
) -> Dict[str, Any]:
    """查询决策信号列表（只返回当前用户自己的）。"""
    params: Dict[str, Any] = {"limit": limit}
    if stock_code:
        params["stock_code"] = stock_code
    if market:
        params["market"] = market
    if action:
        params["action"] = action
    return client.get("/api/v1/decision-signals", params=params)


def run_deep_research(
    client: DSAClient,
    question: str,
    *,
    stock_code: Optional[str] = None,
    max_chars: int = 24000,
) -> Dict[str, Any]:
    """跑一次深度研究（比普通问股更重，会检索外部资料）。"""
    body: Dict[str, Any] = {"question": question}
    if stock_code:
        body["stock_code"] = stock_code
    payload = client.post("/api/v1/agent/research", json_body=body, timeout=900.0)
    return _limit_payload(payload, max_chars)


# ---------------------------------------------------------------------------
# 6. 用量监控
# ---------------------------------------------------------------------------

def get_my_usage(client: DSAClient, *, limit_days: int = 30) -> Dict[str, Any]:
    """当前用户的 LLM 用量（按用户归属）。"""
    return client.get(f"{TENANCY}/usage", params={"limit_days": limit_days})


def get_usage_summary(client: DSAClient, *, period: str = "month") -> Dict[str, Any]:
    """LLM 用量汇总。period: today / month / all。"""
    return client.get("/api/v1/usage/summary", params={"period": period})


# ---------------------------------------------------------------------------
# 行情辅助
# ---------------------------------------------------------------------------

def get_stock_quote(client: DSAClient, stock_code: str) -> Dict[str, Any]:
    """取实时行情。"""
    return client.get(f"/api/v1/stocks/{stock_code}/quote")


def get_stock_profile(client: DSAClient, stock_code: str) -> Dict[str, Any]:
    """取股票基本面画像。"""
    return client.get(f"/api/v1/stocks/{stock_code}/profile")


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _limit_payload(payload: Any, max_chars: int) -> Any:
    """对可能超长的文本字段做截断，避免撑爆模型上下文。

    只处理常见的长文本字段名，其余结构原样返回。
    """
    if not isinstance(payload, dict) or max_chars <= 0:
        return payload
    limited = dict(payload)
    for key in ("content", "markdown", "report", "text", "answer", "reply", "message"):
        value = limited.get(key)
        if isinstance(value, str) and len(value) > max_chars:
            limited[key] = truncate(value, max_chars)
    return limited
