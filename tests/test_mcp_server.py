# -*- coding: utf-8 -*-
"""MCP 服务器测试。

用 ``httpx.MockTransport`` 拦掉真实网络，验证的是**契约**：

1. Bearer Token 正确注入；无 Token 时能用用户名密码换
2. 各类错误体（FastAPI ``detail`` / 多租户 ``error+message``）都能映射成可读信息
3. 每个工具打到正确的 **方法 + 路径 + 载荷**
4. ⚠️ 自选股走 ``/api/v1/tenancy/watchlist``（按用户），**不能**碰全局接口
5. 长文本被截断，不会撑爆模型上下文
6. 工具失败时返回结构化 JSON，而不是把异常抛给协议层

不依赖真实 DSA 实例，也不需要数据库。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

import httpx

from mcp_server import tools as dsa_tools
from mcp_server.client import DSAApiError, DSAClient, truncate

try:
    from mcp_server import server as dsa_server

    _MCP_AVAILABLE = True
    _MCP_IMPORT_ERROR = ""
except Exception as exc:  # noqa: BLE001 —— 未安装 mcp 时跳过协议层测试
    dsa_server = None
    _MCP_AVAILABLE = False
    _MCP_IMPORT_ERROR = str(exc)


BASE_URL = "http://dsa.test"


class Recorder:
    """记录请求，并按 (method, path) 返回预设响应。"""

    def __init__(self, routes=None):
        self.calls = []
        self.routes = dict(routes or {})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = None
        raw = request.content
        if raw:
            try:
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                body = raw.decode("utf-8", "replace")
        self.calls.append(
            {
                "method": request.method,
                "path": request.url.path,
                "query": dict(request.url.params),
                "body": body,
                "authorization": request.headers.get("authorization"),
            }
        )
        key = (request.method, request.url.path)
        if key in self.routes:
            return self.routes[key]
        return httpx.Response(200, json={"ok": True})

    @property
    def last(self):
        return self.calls[-1]

    @property
    def paths(self):
        return [call["path"] for call in self.calls]


def build_client(recorder, *, token="tok-abc", **kwargs) -> DSAClient:
    return DSAClient(
        base_url=BASE_URL,
        token=token,
        transport=httpx.MockTransport(recorder),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 传输层
# ---------------------------------------------------------------------------

class ClientTests(unittest.TestCase):
    def test_bearer_token_is_injected(self):
        rec = Recorder()
        build_client(rec).get("/api/v1/tenancy/watchlist")
        self.assertEqual(rec.last["authorization"], "Bearer tok-abc")

    def test_trailing_slash_is_stripped_from_base_url(self):
        client = DSAClient(base_url="http://dsa.test/", token="t", transport=httpx.MockTransport(Recorder()))
        self.assertEqual(client.base_url, "http://dsa.test")

    def test_login_flow_exchanges_credentials_for_token(self):
        rec = Recorder(
            {
                ("POST", "/api/v1/tenancy/auth/token"): httpx.Response(
                    200, json={"token": "fresh-token", "user": {"id": 7}}
                )
            }
        )
        client = DSAClient(
            base_url=BASE_URL,
            transport=httpx.MockTransport(rec),
            username="alice",
            password="pw-123",
        )
        client.get("/api/v1/tenancy/watchlist")

        self.assertEqual(rec.calls[0]["path"], "/api/v1/tenancy/auth/token")
        self.assertEqual(rec.calls[0]["body"], {"username": "alice", "password": "pw-123"})
        # 第二次请求才带业务路径，且用的是换来的 token
        self.assertEqual(rec.calls[1]["path"], "/api/v1/tenancy/watchlist")
        self.assertEqual(rec.calls[1]["authorization"], "Bearer fresh-token")

    def test_missing_credentials_raises_readable_error(self):
        with patch.dict(
            os.environ,
            {"DSA_API_TOKEN": "", "DSA_USERNAME": "", "DSA_PASSWORD": ""},
        ):
            client = DSAClient(base_url=BASE_URL, transport=httpx.MockTransport(Recorder()))
            with self.assertRaises(DSAApiError) as ctx:
                client.get("/api/v1/tenancy/watchlist")
        self.assertEqual(ctx.exception.code, "missing_credentials")
        self.assertIn("DSA_API_TOKEN", ctx.exception.message)

    def test_error_detail_string_is_mapped(self):
        rec = Recorder({("GET", "/x"): httpx.Response(400, json={"detail": "参数不合法"})})
        with self.assertRaises(DSAApiError) as ctx:
            build_client(rec).get("/x")
        self.assertEqual(ctx.exception.message, "参数不合法")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_tenancy_error_shape_is_mapped(self):
        rec = Recorder(
            {
                ("GET", "/x"): httpx.Response(
                    401, json={"error": "invalid_credentials", "message": "用户名或密码错误"}
                )
            }
        )
        with self.assertRaises(DSAApiError) as ctx:
            build_client(rec).get("/x")
        self.assertEqual(ctx.exception.message, "用户名或密码错误")
        self.assertEqual(ctx.exception.code, "invalid_credentials")
        self.assertEqual(ctx.exception.to_dict()["error"], "invalid_credentials")

    def test_error_detail_dict_is_mapped(self):
        rec = Recorder(
            {("GET", "/x"): httpx.Response(500, json={"detail": {"error": "boom", "message": "炸了"}})}
        )
        with self.assertRaises(DSAApiError) as ctx:
            build_client(rec).get("/x")
        self.assertEqual(ctx.exception.message, "炸了")
        self.assertEqual(ctx.exception.code, "boom")

    def test_connection_failure_is_wrapped(self):
        def boom(request):
            raise httpx.ConnectError("connection refused", request=request)

        client = DSAClient(base_url=BASE_URL, token="t", transport=httpx.MockTransport(boom))
        with self.assertRaises(DSAApiError) as ctx:
            client.get("/x")
        self.assertEqual(ctx.exception.code, "connection_error")
        self.assertIn(BASE_URL, ctx.exception.message)

    def test_non_json_response_falls_back_to_raw(self):
        rec = Recorder({("GET", "/x"): httpx.Response(200, text="plain text body")})
        self.assertEqual(build_client(rec).get("/x"), {"raw": "plain text body"})

    def test_truncate_keeps_short_text_intact(self):
        self.assertEqual(truncate("hello", 100), "hello")

    def test_truncate_marks_long_text(self):
        out = truncate("x" * 500, 100)
        self.assertTrue(out.startswith("x" * 100))
        self.assertIn("已截断", out)
        self.assertIn("500", out)


# ---------------------------------------------------------------------------
# 工具契约
# ---------------------------------------------------------------------------

class ToolContractTests(unittest.TestCase):
    def setUp(self):
        self.rec = Recorder()
        self.client = build_client(self.rec)

    # -- 自选股（最关键：必须走按用户的端点） ----------------------------

    def test_add_to_watchlist_uses_per_user_endpoint(self):
        dsa_tools.add_to_watchlist(self.client, "600519")
        self.assertEqual(self.rec.last["method"], "POST")
        self.assertEqual(self.rec.last["path"], "/api/v1/tenancy/watchlist")
        self.assertEqual(self.rec.last["body"], {"stock_code": "600519"})
        # 绝不能打到写全局 STOCK_LIST 的上游接口
        self.assertNotIn("/api/v1/stocks/watchlist", self.rec.last["path"])

    def test_remove_from_watchlist_uses_path_param(self):
        dsa_tools.remove_from_watchlist(self.client, "600519")
        self.assertEqual(self.rec.last["method"], "DELETE")
        self.assertEqual(self.rec.last["path"], "/api/v1/tenancy/watchlist/600519")

    def test_get_watchlist_uses_per_user_endpoint(self):
        dsa_tools.get_watchlist(self.client)
        self.assertEqual(self.rec.last["path"], "/api/v1/tenancy/watchlist")

    def test_replace_watchlist_sends_full_list(self):
        dsa_tools.replace_watchlist(self.client, ["600519", "000858"])
        self.assertEqual(self.rec.last["method"], "PUT")
        self.assertEqual(self.rec.last["body"], {"stock_codes": ["600519", "000858"]})

    # -- 分析 -------------------------------------------------------------

    def test_analyze_single_code_uses_stock_code_field(self):
        dsa_tools.analyze_stocks(self.client, ["600519"])
        body = self.rec.last["body"]
        self.assertEqual(body["stock_code"], "600519")
        self.assertNotIn("stock_codes", body)
        self.assertEqual(self.rec.last["path"], "/api/v1/analysis/analyze")

    def test_analyze_multiple_codes_uses_stock_codes_field(self):
        dsa_tools.analyze_stocks(self.client, ["600519", "000858"])
        body = self.rec.last["body"]
        self.assertEqual(body["stock_codes"], ["600519", "000858"])
        self.assertNotIn("stock_code", body)

    def test_analyze_without_codes_omits_both_fields(self):
        dsa_tools.analyze_stocks(self.client, [])
        body = self.rec.last["body"]
        self.assertNotIn("stock_code", body)
        self.assertNotIn("stock_codes", body)

    def test_analyze_passes_flags(self):
        dsa_tools.analyze_stocks(
            self.client, ["600519"], report_type="brief", force_refresh=True, async_mode=True
        )
        body = self.rec.last["body"]
        self.assertEqual(body["report_type"], "brief")
        self.assertTrue(body["force_refresh"])
        self.assertTrue(body["async_mode"])

    def test_analysis_history_passes_filters(self):
        dsa_tools.list_analysis_history(
            self.client, stock_code="600519", start_date="2026-09-01", page=2, limit=50
        )
        query = self.rec.last["query"]
        self.assertEqual(query["stock_code"], "600519")
        self.assertEqual(query["start_date"], "2026-09-01")
        self.assertEqual(query["page"], "2")
        self.assertEqual(query["limit"], "50")
        self.assertNotIn("report_type", query)

    def test_analysis_task_uses_status_path(self):
        dsa_tools.get_analysis_task(self.client, "task-1")
        self.assertEqual(self.rec.last["path"], "/api/v1/analysis/status/task-1")

    def test_report_content_is_truncated(self):
        rec = Recorder(
            {
                ("GET", "/api/v1/history/9"): httpx.Response(
                    200, json={"id": 9, "content": "y" * 5000}
                )
            }
        )
        out = dsa_tools.get_analysis_report(build_client(rec), 9, max_chars=500)
        self.assertEqual(len(out["content"][:500]), 500)
        self.assertIn("已截断", out["content"])

    # -- 大盘复盘（公共数据） ---------------------------------------------

    def test_trigger_market_review_path_and_body(self):
        dsa_tools.trigger_market_review(self.client, region="cn", send_notification=True)
        self.assertEqual(self.rec.last["method"], "POST")
        self.assertEqual(self.rec.last["path"], "/api/v1/analysis/market-review")
        self.assertEqual(self.rec.last["body"], {"region": "cn", "send_notification": True})

    def test_trigger_market_review_omits_blank_region(self):
        dsa_tools.trigger_market_review(self.client)
        body = self.rec.last["body"]
        self.assertNotIn("region", body, "留空应让服务端用默认区域")
        self.assertEqual(body["send_notification"], False)

    def test_list_market_reviews_filters_by_report_type(self):
        dsa_tools.list_market_reviews(self.client, limit=5)
        query = self.rec.last["query"]
        self.assertEqual(self.rec.last["path"], "/api/v1/history")
        self.assertEqual(query["report_type"], "market_review")
        self.assertEqual(query["limit"], "5")

    def test_latest_market_review_fetches_markdown(self):
        rec = Recorder(
            {
                ("GET", "/api/v1/history"): httpx.Response(
                    200,
                    json={
                        "total": 1,
                        "items": [
                            {"id": 42, "report_type": "market_review", "created_at": "T1"}
                        ],
                    },
                ),
                ("GET", "/api/v1/history/42/markdown"): httpx.Response(
                    200, json={"content": "# 大盘复盘\n今日..."}
                ),
            }
        )
        out = dsa_tools.get_latest_market_review(build_client(rec))
        self.assertTrue(out["found"])
        self.assertEqual(out["record_id"], 42)
        self.assertIn("大盘复盘", out["content"])
        self.assertEqual(
            rec.paths, ["/api/v1/history", "/api/v1/history/42/markdown"]
        )

    def test_latest_market_review_returns_found_false_when_empty(self):
        """「还没复盘过」是正常状态，不该抛异常。"""
        rec = Recorder(
            {("GET", "/api/v1/history"): httpx.Response(200, json={"total": 0, "items": []})}
        )
        out = dsa_tools.get_latest_market_review(build_client(rec))
        self.assertFalse(out["found"])
        self.assertIn("trigger_market_review", out["message"])
        self.assertEqual(rec.paths, ["/api/v1/history"], "无记录时不应再请求正文")

    # -- 选股 -------------------------------------------------------------

    def test_screening_strategies_path(self):
        dsa_tools.list_screening_strategies(self.client)
        self.assertEqual(self.rec.last["path"], "/api/v1/screening/strategies")

    def test_run_screening_body(self):
        dsa_tools.run_screening(self.client, strategy="dual_low", market="hk", max_results=5)
        self.assertEqual(self.rec.last["path"], "/api/v1/screening/screen")
        self.assertEqual(
            self.rec.last["body"],
            {"strategy": "dual_low", "market": "hk", "max_results": 5},
        )

    def test_screening_history_omits_empty_filters(self):
        dsa_tools.get_screening_history(self.client, limit=10)
        self.assertEqual(self.rec.last["query"], {"limit": "10"})

    def test_hotspots_default_and_topic(self):
        dsa_tools.get_market_hotspots(self.client)
        self.assertEqual(self.rec.last["path"], "/api/v1/screening/hotspots")
        dsa_tools.get_market_hotspots(self.client, topic="ai")
        self.assertEqual(self.rec.last["path"], "/api/v1/screening/hotspots/ai")

    # -- 问股 / 研究 ------------------------------------------------------

    def test_ask_without_session_omits_session_id(self):
        dsa_tools.ask_stock_question(self.client, "贵州茅台怎么样")
        self.assertEqual(self.rec.last["path"], "/api/v1/agent/chat")
        self.assertEqual(self.rec.last["body"], {"message": "贵州茅台怎么样"})

    def test_ask_with_session_continues_conversation(self):
        dsa_tools.ask_stock_question(self.client, "继续", session_id="s-1")
        self.assertEqual(self.rec.last["body"]["session_id"], "s-1")

    def test_chat_sessions_and_messages_paths(self):
        dsa_tools.list_chat_sessions(self.client, limit=5)
        self.assertEqual(self.rec.last["path"], "/api/v1/agent/chat/sessions")
        dsa_tools.get_chat_messages(self.client, "s-1")
        self.assertEqual(self.rec.last["path"], "/api/v1/agent/chat/sessions/s-1")

    def test_deep_research_body(self):
        dsa_tools.run_deep_research(self.client, "新能源车产业链", stock_code="000858")
        self.assertEqual(self.rec.last["path"], "/api/v1/agent/research")
        self.assertEqual(
            self.rec.last["body"], {"question": "新能源车产业链", "stock_code": "000858"}
        )

    # -- AI 建议 ----------------------------------------------------------

    def test_decision_signal_paths(self):
        dsa_tools.get_decision_signal(self.client, "600519")
        self.assertEqual(self.rec.last["path"], "/api/v1/decision-signals/latest/600519")
        dsa_tools.list_decision_signals(self.client, action="buy", limit=3)
        self.assertEqual(self.rec.last["path"], "/api/v1/decision-signals")
        self.assertEqual(self.rec.last["query"]["action"], "buy")
        self.assertEqual(self.rec.last["query"]["limit"], "3")

    # -- 用量 -------------------------------------------------------------

    def test_usage_paths(self):
        dsa_tools.get_my_usage(self.client, limit_days=7)
        self.assertEqual(self.rec.last["path"], "/api/v1/tenancy/usage")
        self.assertEqual(self.rec.last["query"], {"limit_days": "7"})
        dsa_tools.get_usage_summary(self.client, period="today")
        self.assertEqual(self.rec.last["path"], "/api/v1/usage/summary")
        self.assertEqual(self.rec.last["query"], {"period": "today"})

    # -- 身份 / 行情 ------------------------------------------------------

    def test_identity_paths(self):
        dsa_tools.whoami(self.client)
        self.assertEqual(self.rec.last["path"], "/api/v1/tenancy/auth/me")
        dsa_tools.get_my_settings(self.client)
        self.assertEqual(self.rec.last["path"], "/api/v1/tenancy/settings")
        dsa_tools.update_my_settings(self.client, {"REPORT_TYPE": "brief"})
        self.assertEqual(self.rec.last["method"], "PUT")
        self.assertEqual(self.rec.last["body"], {"settings": {"REPORT_TYPE": "brief"}})

    def test_quote_and_profile_paths(self):
        dsa_tools.get_stock_quote(self.client, "600519")
        self.assertEqual(self.rec.last["path"], "/api/v1/stocks/600519/quote")
        dsa_tools.get_stock_profile(self.client, "600519")
        self.assertEqual(self.rec.last["path"], "/api/v1/stocks/600519/profile")


# ---------------------------------------------------------------------------
# 协议层
# ---------------------------------------------------------------------------

@unittest.skipUnless(_MCP_AVAILABLE, f"未安装 mcp SDK: {_MCP_IMPORT_ERROR}")
class ServerTests(unittest.TestCase):
    def test_expected_tools_are_registered(self):
        import asyncio

        names = {tool.name for tool in asyncio.run(dsa_server.server.list_tools())}
        required = {
            "whoami",
            "analyze_stocks",
            "get_analysis_task",
            "list_analysis_history",
            "get_analysis_report",
            "get_watchlist",
            "add_to_watchlist",
            "remove_from_watchlist",
            "replace_watchlist",
            "list_screening_strategies",
            "run_screening",
            "ask_stock_question",
            "get_decision_signal",
            "get_my_usage",
            "get_usage_summary",
            "get_stock_quote",
            "get_latest_market_review",
            "list_market_reviews",
            "trigger_market_review",
        }
        self.assertTrue(required.issubset(names), f"缺少工具: {required - names}")
        self.assertEqual(len(names), 29)

    def test_tool_failure_returns_structured_json_not_exception(self):
        rec = Recorder(
            {("GET", "/api/v1/tenancy/auth/me"): httpx.Response(401, json={"detail": "未授权"})}
        )
        with patch.object(dsa_server, "get_client", return_value=build_client(rec)):
            out = dsa_server.whoami()
        data = json.loads(out)
        self.assertFalse(data["ok"])
        self.assertEqual(data["message"], "未授权")

    def test_tool_success_is_json_text(self):
        rec = Recorder(
            {("GET", "/api/v1/tenancy/auth/me"): httpx.Response(200, json={"user": {"id": 3}})}
        )
        with patch.object(dsa_server, "get_client", return_value=build_client(rec)):
            data = json.loads(dsa_server.whoami())
        self.assertEqual(data["user"]["id"], 3)

    def test_unexpected_exception_is_contained(self):
        class Boom:
            def get(self, *a, **kw):
                raise RuntimeError("unexpected")

        with patch.object(dsa_server, "get_client", return_value=Boom()):
            data = json.loads(dsa_server.whoami())
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "internal_error")
        self.assertIn("unexpected", data["message"])

    def test_check_reports_identity_on_success(self):
        rec = Recorder(
            {
                ("GET", "/api/v1/tenancy/auth/me"): httpx.Response(
                    200, json={"user": {"id": 3, "username": "alice", "role": "user"}}
                ),
                ("GET", "/api/v1/tenancy/watchlist"): httpx.Response(
                    200, json={"stock_codes": ["600519"], "source": "user"}
                ),
            }
        )
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            code = dsa_server._check(build_client(rec))
        output = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("alice", output)
        self.assertIn("1 只", output)

    def test_check_fails_without_credentials(self):
        with patch.dict(
            os.environ,
            {"DSA_API_TOKEN": "", "DSA_USERNAME": "", "DSA_PASSWORD": ""},
        ):
            client = DSAClient(base_url=BASE_URL, transport=httpx.MockTransport(Recorder()))
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                code = dsa_server._check(client)
        self.assertEqual(code, 2)
        self.assertIn("未提供凭据", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
