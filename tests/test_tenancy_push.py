# -*- coding: utf-8 -*-
"""行情推送（大盘 + 自选股）的到点判定、渲染与接口契约。

分两层：

* :class:`PushSlotLogicTests` / :class:`BuildDigestTests` —— 纯逻辑。到点判定
  必须**幂等**（调用方是轮询的，同一个时段会被问到多次），这是本模块最容易
  写错的地方，所以单独覆盖。
* :class:`PushApiTests` —— 走真实 HTTP 栈。重点验证**权限边界**：
  ``/push/digest`` 能按入参读别人的数据，因此普通成员必须被挡住。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

try:
    import newspaper  # noqa: F401
except ModuleNotFoundError:
    _mock_newspaper = MagicMock()
    _mock_newspaper.Article = MagicMock()
    _mock_newspaper.Config = MagicMock()
    sys.modules["newspaper"] = _mock_newspaper

from src.tenancy import push

from fastapi.testclient import TestClient

ADMIN_PASSWORD = "admin-pass-123"
ALICE_PASSWORD = "alice-pass-123"
SERVICE_PASSWORD = "svc-pass-123"


def _patch_internal_store(sent: dict):
    """把去重标记的读写换成内存字典，避免纯逻辑测试也要起数据库。"""
    return [
        patch.object(
            push,
            "read_internal_setting",
            side_effect=lambda tenant_id, key: sent.get((tenant_id, key)),
        ),
        patch.object(
            push,
            "write_internal_setting",
            side_effect=lambda tenant_id, key, value: sent.__setitem__((tenant_id, key), value),
        ),
    ]


class PushSlotLogicTests(unittest.TestCase):
    """到点判定。"""

    def setUp(self) -> None:
        self.sent: dict = {}
        self._patches = _patch_internal_store(self.sent)
        for item in self._patches:
            item.start()

    def tearDown(self) -> None:
        for item in self._patches:
            item.stop()

    def test_not_due_before_first_slot(self) -> None:
        slot, reason = push.due_slot(1, ["09:35", "15:30"], datetime(2026, 9, 14, 8, 0))
        self.assertIsNone(slot)
        self.assertEqual(reason, "not_due")

    def test_due_within_window(self) -> None:
        slot, reason = push.due_slot(1, ["09:35", "15:30"], datetime(2026, 9, 14, 9, 40))
        self.assertEqual(slot, "09:35")
        self.assertEqual(reason, "")

    def test_due_exactly_at_slot(self) -> None:
        slot, reason = push.due_slot(1, ["09:35"], datetime(2026, 9, 14, 9, 35, 0))
        self.assertEqual(slot, "09:35")
        self.assertEqual(reason, "")

    def test_second_slot_still_due_after_first_sent(self) -> None:
        now = datetime(2026, 9, 14, 15, 32)
        push.mark_sent(1, "09:35", now)
        slot, _ = push.due_slot(1, ["09:35", "15:30"], now)
        self.assertEqual(slot, "15:30")

    def test_already_sent_is_not_due(self) -> None:
        now = datetime(2026, 9, 14, 9, 40)
        push.mark_sent(1, "09:35", now)
        slot, reason = push.due_slot(1, ["09:35"], now)
        self.assertIsNone(slot)
        self.assertEqual(reason, "already_sent")

    def test_all_slots_done_reports_already_sent_not_missed(self) -> None:
        """两个时段都推完后，再问一次应当是「今天做完了」，不是「错过」。"""
        now = datetime(2026, 9, 14, 16, 0)
        push.mark_sent(1, "09:35", now)
        push.mark_sent(1, "15:30", now)
        slot, reason = push.due_slot(1, ["09:35", "15:30"], now)
        self.assertIsNone(slot)
        self.assertEqual(reason, "already_sent")

    def test_next_day_rearms(self) -> None:
        push.mark_sent(1, "09:35", datetime(2026, 9, 14, 9, 40))
        slot, _ = push.due_slot(1, ["09:35"], datetime(2026, 9, 15, 9, 40))
        self.assertEqual(slot, "09:35")

    def test_other_user_marker_does_not_leak(self) -> None:
        now = datetime(2026, 9, 14, 9, 40)
        push.mark_sent(1, "09:35", now)
        slot, _ = push.due_slot(2, ["09:35"], now)
        self.assertEqual(slot, "09:35")

    def test_missed_window(self) -> None:
        slot, reason = push.due_slot(1, ["09:35"], datetime(2026, 9, 14, 11, 0))
        self.assertIsNone(slot)
        self.assertEqual(reason, "missed_window")

    def test_no_valid_times(self) -> None:
        slot, reason = push.due_slot(1, ["25:99"], datetime(2026, 9, 14, 9, 40))
        self.assertIsNone(slot)
        self.assertEqual(reason, "no_valid_times")

    def test_slot_key_is_internal(self) -> None:
        self.assertTrue(push._slot_key("09:35").startswith("__"))
        self.assertEqual(push._slot_key("09:35"), "__PUSH_LAST_SENT_0935")


class RenderTests(unittest.TestCase):
    """推送正文渲染。"""

    NOW = datetime(2026, 9, 14, 9, 35, 12)

    def test_renders_indices_and_watchlist(self) -> None:
        text = push.render_digest(
            slot="09:35",
            indices=[{"name": "上证指数", "current": 3412.56, "change_pct": 0.42}],
            watchlist=[
                {"code": "600206", "name": "有研新材", "price": 18.3, "change_pct": -1.2, "ok": True}
            ],
            watchlist_source="user",
            now=self.NOW,
        )
        self.assertIn("09:35", text)
        self.assertIn("上证指数", text)
        self.assertIn("3,412.56", text)
        self.assertIn("+0.42%", text)
        self.assertIn("有研新材", text)
        self.assertIn("600206", text)
        self.assertIn("-1.20%", text)

    def test_renders_missing_data_gracefully(self) -> None:
        text = push.render_digest(
            slot="",
            indices=[],
            watchlist=[],
            watchlist_source="user",
            now=self.NOW,
        )
        self.assertIn("指数行情暂不可用", text)
        self.assertIn("还没有自选股", text)
        self.assertNotIn("None", text)

    def test_renders_failed_quote_code(self) -> None:
        text = push.render_digest(
            slot="15:30",
            indices=[{"name": "上证指数", "current": 3400.0, "change_pct": -0.1}],
            watchlist=[{"code": "301526", "ok": False}],
            watchlist_source="user",
            now=self.NOW,
        )
        self.assertIn("301526 行情暂不可用", text)

    def test_renders_placeholder_for_none_values(self) -> None:
        text = push.render_digest(
            slot="09:35",
            indices=[{"name": "上证指数", "current": None, "change_pct": None}],
            watchlist=[{"code": "600206", "name": "有研新材", "price": None, "change_pct": None, "ok": True}],
            watchlist_source="user",
            now=self.NOW,
        )
        self.assertNotIn("None", text)
        self.assertIn("--", text)


class BuildDigestTests(unittest.TestCase):
    """build_user_digest 的判定分支。"""

    NOW = datetime(2026, 9, 14, 9, 40)

    def setUp(self) -> None:
        self.sent: dict = {}
        self._patches = _patch_internal_store(self.sent)
        for item in self._patches:
            item.start()

    def tearDown(self) -> None:
        for item in self._patches:
            item.stop()

    def test_disabled_skips(self) -> None:
        with patch.object(push, "effective_push_enabled", return_value=False):
            out = push.build_user_digest(3, now=self.NOW)
        self.assertTrue(out["skip"])
        self.assertEqual(out["reason"], "disabled")

    def test_non_trading_day_skips(self) -> None:
        with patch.object(push, "effective_push_enabled", return_value=True), patch.object(
            push, "effective_push_times", return_value=["09:35", "15:30"]
        ), patch.object(push, "is_trading_day", return_value=False):
            out = push.build_user_digest(3, now=self.NOW)
        self.assertTrue(out["skip"])
        self.assertEqual(out["reason"], "non_trading_day")

    def test_not_due_does_not_touch_data_sources(self) -> None:
        with patch.object(push, "effective_push_enabled", return_value=True), patch.object(
            push, "effective_push_times", return_value=["09:35", "15:30"]
        ), patch.object(push, "is_trading_day", return_value=True), patch.object(
            push, "fetch_indices"
        ) as fetch_indices:
            out = push.build_user_digest(3, now=datetime(2026, 9, 14, 8, 0))
        self.assertTrue(out["skip"])
        self.assertEqual(out["reason"], "not_due")
        fetch_indices.assert_not_called()

    def test_due_slot_produces_text_and_marks_sent(self) -> None:
        with patch.object(push, "effective_push_enabled", return_value=True), patch.object(
            push, "effective_push_times", return_value=["09:35", "15:30"]
        ), patch.object(push, "is_trading_day", return_value=True), patch.object(
            push, "fetch_indices", return_value=[{"name": "上证指数", "current": 3412.56, "change_pct": 0.42}]
        ), patch.object(
            push, "resolve_stock_list", return_value={"stock_codes": ["600206"], "source": "user"}
        ), patch.object(
            push, "fetch_watchlist_quotes",
            return_value=[{"code": "600206", "name": "有研新材", "price": 18.3, "change_pct": 1.0, "ok": True}],
        ):
            out = push.build_user_digest(3, now=self.NOW)

        self.assertFalse(out["skip"])
        self.assertEqual(out["slot"], "09:35")
        self.assertIn("上证指数", out["text"])
        self.assertEqual(self.sent.get((3, "__PUSH_LAST_SENT_0935")), "2026-09-14")

    def test_second_poll_in_same_window_is_idempotent(self) -> None:
        """轮询的核心保证：同一个时段问两次，第二次不再推。"""
        common = [
            patch.object(push, "effective_push_enabled", return_value=True),
            patch.object(push, "effective_push_times", return_value=["09:35"]),
            patch.object(push, "is_trading_day", return_value=True),
            patch.object(push, "fetch_indices", return_value=[{"name": "上证指数", "current": 3400.0, "change_pct": 0.1}]),
            patch.object(push, "resolve_stock_list", return_value={"stock_codes": [], "source": "user"}),
            patch.object(push, "fetch_watchlist_quotes", return_value=[]),
        ]
        for item in common:
            item.start()
            self.addCleanup(item.stop)

        first = push.build_user_digest(3, now=datetime(2026, 9, 14, 9, 36))
        second = push.build_user_digest(3, now=datetime(2026, 9, 14, 9, 41))

        self.assertFalse(first["skip"])
        self.assertTrue(second["skip"])
        self.assertEqual(second["reason"], "already_sent")

    def test_no_data_skips_without_marking(self) -> None:
        """数据源全挂时不该写去重标记 —— 否则窗口内再也重试不了。"""
        with patch.object(push, "effective_push_enabled", return_value=True), patch.object(
            push, "effective_push_times", return_value=["09:35"]
        ), patch.object(push, "is_trading_day", return_value=True), patch.object(
            push, "fetch_indices", return_value=[]
        ), patch.object(
            push, "resolve_stock_list", return_value={"stock_codes": ["600206"], "source": "user"}
        ), patch.object(
            push, "fetch_watchlist_quotes", return_value=[{"code": "600206", "ok": False}]
        ):
            out = push.build_user_digest(3, now=self.NOW)

        self.assertTrue(out["skip"])
        self.assertEqual(out["reason"], "no_data")
        self.assertEqual(self.sent, {})

    def test_force_bypasses_schedule_but_keeps_disabled(self) -> None:
        with patch.object(push, "effective_push_enabled", return_value=True), patch.object(
            push, "effective_push_times", return_value=["09:35", "15:30"]
        ), patch.object(push, "is_trading_day", return_value=False), patch.object(
            push, "fetch_indices", return_value=[{"name": "上证指数", "current": 3400.0, "change_pct": 0.0}]
        ), patch.object(
            push, "resolve_stock_list", return_value={"stock_codes": [], "source": "user"}
        ), patch.object(push, "fetch_watchlist_quotes", return_value=[]):
            forced = push.build_user_digest(3, now=datetime(2026, 9, 12, 3, 0), force=True)

        self.assertFalse(forced["skip"])
        # force 不写去重标记：它不该影响真实时间表的节奏
        self.assertEqual(self.sent, {})

    def test_market_now_respects_explicit_argument(self) -> None:
        marker = datetime(2026, 9, 14, 9, 40)
        self.assertIs(push.market_now(marker), marker)


class InternalKeyGuardTests(unittest.TestCase):
    """内部状态键的写入闸门。"""

    def test_rejects_user_writable_key(self) -> None:
        from src.tenancy.settings import write_internal_setting

        with self.assertRaises(ValueError):
            write_internal_setting(1, "PUSH_ENABLED", "true")

    def test_rejects_empty_key(self) -> None:
        from src.tenancy.settings import write_internal_setting

        with self.assertRaises(ValueError):
            write_internal_setting(1, "", "x")


class PushApiTests(unittest.TestCase):
    """走真实 HTTP 栈的权限边界与端到端行为。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="dsa-push-api-")
        self._saved_env = {
            key: os.environ.get(key)
            for key in ("DATABASE_PATH", "DSA_MULTIUSER_ENABLED", "ADMIN_AUTH_ENABLED")
        }
        os.environ["DATABASE_PATH"] = str(Path(self._tmpdir) / "push_test.db")
        os.environ["DSA_MULTIUSER_ENABLED"] = "true"

        from src.config import Config
        from src.storage import DatabaseManager
        from src.tenancy import service as tenancy_service
        from src.tenancy import tokens as tenancy_tokens
        from src.tenancy.context import SYSTEM_TENANT_ID
        from src.tenancy.install import reset_bootstrap_state
        from src.tenancy.scope import disarm_scope_guard

        DatabaseManager.reset_instance()
        Config.reset_instance()
        reset_bootstrap_state()
        tenancy_tokens.reset_token_secret_cache()

        self.db = DatabaseManager.get_instance()
        tenancy_service.reset_password(SYSTEM_TENANT_ID, new_password=ADMIN_PASSWORD)

        from api.app import create_app

        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        from src.tenancy.scope import disarm_scope_guard

        disarm_scope_guard()
        from src.config import Config
        from src.storage import DatabaseManager
        from src.tenancy.install import reset_bootstrap_state

        DatabaseManager.reset_instance()
        Config.reset_instance()
        reset_bootstrap_state()
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    # -- 工具 ---------------------------------------------------------

    def _token(self, username: str, password: str) -> str:
        response = self.client.post(
            "/api/v1/tenancy/auth/token",
            json={"username": username, "password": password},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"]

    @staticmethod
    def _auth(token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def _create_user(self, username: str, password: str, *, system: bool = False) -> int:
        from src.tenancy import service as tenancy_service

        record = tenancy_service.create_user(username=username, password=password, role="user")
        if system:
            from src.storage import get_db
            from src.tenancy.models import TenantUser

            with get_db().session_scope() as session:
                row = (
                    session.query(TenantUser)
                    .filter(TenantUser.username == username)
                    .one()
                )
                row.is_system = True
        return record.id

    def _admin(self) -> dict:
        return self._auth(self._token("admin", ADMIN_PASSWORD))

    # -- 测试 ---------------------------------------------------------

    def test_requires_authentication(self) -> None:
        response = self.client.post("/api/v1/tenancy/push/digest", json={"force": True})
        self.assertEqual(response.status_code, 401)

    def test_normal_user_is_forbidden(self) -> None:
        """普通成员不能按入参读别人的数据 —— 这是本接口的核心边界。"""
        self._create_user("alice", ALICE_PASSWORD)
        headers = self._auth(self._token("alice", ALICE_PASSWORD))
        response = self.client.post(
            "/api/v1/tenancy/push/digest",
            json={"tenantId": 1, "force": True},
            headers=headers,
        )
        self.assertEqual(response.status_code, 403, response.text)

    def test_system_account_is_allowed(self) -> None:
        user_id = self._create_user("svc", SERVICE_PASSWORD, system=True)
        headers = self._auth(self._token("svc", SERVICE_PASSWORD))
        with patch.object(push, "effective_push_enabled", return_value=False):
            response = self.client.post(
                "/api/v1/tenancy/push/digest",
                json={"tenantId": user_id},
                headers=headers,
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["skip"])
        self.assertEqual(response.json()["reason"], "disabled")

    def test_missing_target_is_rejected(self) -> None:
        response = self.client.post(
            "/api/v1/tenancy/push/digest", json={"force": True}, headers=self._admin()
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "missing_target")

    def test_unbound_wechat_skips(self) -> None:
        response = self.client.post(
            "/api/v1/tenancy/push/digest",
            json={"wechatId": "wx-not-bound"},
            headers=self._admin(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["skip"])
        self.assertEqual(response.json()["reason"], "unbound_recipient")

    def test_admin_force_returns_digest(self) -> None:
        alice_id = self._create_user("alice", ALICE_PASSWORD)
        with patch.object(push, "effective_push_enabled", return_value=True), patch.object(
            push, "effective_push_times", return_value=["09:35", "15:30"]
        ), patch.object(
            push, "fetch_indices",
            return_value=[{"name": "上证指数", "current": 3412.56, "change_pct": 0.42}],
        ), patch.object(
            push, "resolve_stock_list", return_value={"stock_codes": ["600206"], "source": "user"}
        ), patch.object(
            push, "fetch_watchlist_quotes",
            return_value=[{"code": "600206", "name": "有研新材", "price": 18.3, "change_pct": 1.0, "ok": True}],
        ):
            response = self.client.post(
                "/api/v1/tenancy/push/digest",
                json={"tenantId": alice_id, "force": True},
                headers=self._admin(),
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["skip"])
        self.assertIn("上证指数", body["text"])
        self.assertIn("有研新材", body["text"])

    def test_user_can_disable_push_via_settings(self) -> None:
        """「取消推送」必须走用户自己的设置，不需要管理员介入。"""
        self._create_user("alice", ALICE_PASSWORD)
        headers = self._auth(self._token("alice", ALICE_PASSWORD))

        response = self.client.put(
            "/api/v1/tenancy/settings",
            json={"settings": {"PUSH_ENABLED": False, "PUSH_TIMES": "10:00,16:00"}},
            headers=headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        written = set(response.json()["written"])
        self.assertEqual(written, {"PUSH_ENABLED", "PUSH_TIMES"})

        view = self.client.get("/api/v1/tenancy/settings", headers=headers).json()["settings"]
        # effective_value 是「编码后的字符串」形态（见 public_settings_view），
        # 不是原始类型 —— 布尔是 "false"，时间列表是逗号串。
        self.assertEqual(view["PUSH_ENABLED"]["effective_value"], "false")
        self.assertEqual(view["PUSH_ENABLED"]["source"], "user")
        self.assertEqual(view["PUSH_TIMES"]["effective_value"], "10:00,16:00")
        self.assertEqual(view["PUSH_TIMES"]["source"], "user")

    def test_analysis_schedule_keys_stay_forbidden(self) -> None:
        """放开推送时间**不能**连带放开分析调度 —— 两者是不同的东西。"""
        self._create_user("alice", ALICE_PASSWORD)
        headers = self._auth(self._token("alice", ALICE_PASSWORD))

        response = self.client.put(
            "/api/v1/tenancy/settings",
            json={"settings": {"SCHEDULE_TIMES": "10:00"}},
            headers=headers,
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"], "no_valid_keys")

    def test_capabilities_expose_push_keys(self) -> None:
        body = self.client.get("/api/v1/tenancy/capabilities").json()
        self.assertIn("PUSH_ENABLED", body["supported_setting_keys"])
        self.assertIn("PUSH_TIMES", body["supported_setting_keys"])


if __name__ == "__main__":
    unittest.main()
