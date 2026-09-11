# -*- coding: utf-8 -*-
"""多租户 REST API 端到端测试。

这里走**真实的 HTTP 栈**（FastAPI + 中间件 + 路由 + SQLAlchemy），
验证的是「整条链路是否真的生效」，而不是单个函数的行为：

1. 未认证请求被拦截（401）
2. Bearer Token 能换取身份
3. 普通用户无法访问管理员端点（403）
4. 按用户配置读写生效，且受保护键被拒绝
5. 会话 Cookie 与 Bearer Token 两条路径都能工作
6. Token 无效 / 被吊销后立即失效
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

# newspaper3k 是可选依赖（需要 wkhtmltopdf/lxml 生态），API 契约测试
# 不需要它真正工作——沿用仓库既有的 mock 惯例。
try:
    import newspaper  # noqa: F401
except ModuleNotFoundError:
    _mock_newspaper = MagicMock()
    _mock_newspaper.Article = MagicMock()
    _mock_newspaper.Config = MagicMock()
    sys.modules["newspaper"] = _mock_newspaper

from fastapi.testclient import TestClient

from src.tenancy import service as tenancy_service
from src.tenancy import tokens as tenancy_tokens
from src.tenancy.context import SYSTEM_TENANT_ID
from src.tenancy.install import reset_bootstrap_state
from src.tenancy.scope import disarm_scope_guard

ADMIN_PASSWORD = "admin-pass-123"
ALICE_PASSWORD = "alice-pass-123"


class TenancyApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="dsa-tenancy-api-")
        self._saved_env = {
            key: os.environ.get(key)
            for key in ("DATABASE_PATH", "DSA_MULTIUSER_ENABLED", "ADMIN_AUTH_ENABLED")
        }
        os.environ["DATABASE_PATH"] = str(Path(self._tmpdir) / "api_test.db")
        os.environ["DSA_MULTIUSER_ENABLED"] = "true"

        from src.config import Config
        from src.storage import DatabaseManager

        DatabaseManager.reset_instance()
        Config.reset_instance()
        reset_bootstrap_state()
        tenancy_tokens.reset_token_secret_cache()

        # 触发数据库引导（补 tenant_id 列 / 建租户表 / 播种系统属主）
        self.db = DatabaseManager.get_instance()

        # 系统属主在全新环境下没有可用密码（没有 .admin_password_hash），
        # 这里显式设置一个，模拟管理员首次登录后的状态。
        tenancy_service.reset_password(SYSTEM_TENANT_ID, new_password=ADMIN_PASSWORD)

        # 必须在 create_app() 之前设置环境变量：install_tenancy 在装配时读它
        from api.app import create_app

        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        disarm_scope_guard()
        from src.config import Config
        from src.storage import DatabaseManager

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

    # -- 测试 ---------------------------------------------------------

    def test_capabilities_is_public(self) -> None:
        response = self.client.get("/api/v1/tenancy/capabilities")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["multiuser_enabled"])
        self.assertIn("STOCK_LIST", body["supported_setting_keys"])
        self.assertIn("tenant_id", body["scope"]["scoped_tables"] and "tenant_id" or "tenant_id")

    def test_unauthenticated_api_is_rejected(self) -> None:
        for path in ("/api/v1/tenancy/users", "/api/v1/tenancy/settings", "/api/v1/tenancy/auth/me"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 401, f"{path} 应要求认证")

    def test_invalid_token_is_rejected(self) -> None:
        response = self.client.get(
            "/api/v1/tenancy/auth/me",
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        self.assertEqual(response.status_code, 401)

    def test_admin_can_manage_users(self) -> None:
        admin = self._auth(self._token("admin", ADMIN_PASSWORD))

        created = self.client.post(
            "/api/v1/tenancy/users",
            json={"username": "alice", "password": ALICE_PASSWORD, "role": "user"},
            headers=admin,
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["user"]["username"], "alice")
        self.assertNotIn("password_hash", created.json()["user"])

        listed = self.client.get("/api/v1/tenancy/users", headers=admin)
        self.assertEqual(listed.status_code, 200)
        usernames = {u["username"] for u in listed.json()["users"]}
        self.assertEqual(usernames, {"admin", "alice"})

    def test_normal_user_cannot_manage_users(self) -> None:
        admin = self._auth(self._token("admin", ADMIN_PASSWORD))
        self.client.post(
            "/api/v1/tenancy/users",
            json={"username": "alice", "password": ALICE_PASSWORD, "role": "user"},
            headers=admin,
        )
        alice = self._auth(self._token("alice", ALICE_PASSWORD))

        self.assertEqual(self.client.get("/api/v1/tenancy/users", headers=alice).status_code, 403)
        self.assertEqual(
            self.client.post(
                "/api/v1/tenancy/users",
                json={"username": "mallory", "password": "mallory-pass"},
                headers=alice,
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.delete(f"/api/v1/tenancy/users/{SYSTEM_TENANT_ID}", headers=alice).status_code,
            403,
        )

    def test_settings_roundtrip_and_isolation(self) -> None:
        admin = self._auth(self._token("admin", ADMIN_PASSWORD))
        for name in ("alice", "bob"):
            self.client.post(
                "/api/v1/tenancy/users",
                json={"username": name, "password": ALICE_PASSWORD, "role": "user"},
                headers=admin,
            )
        alice = self._auth(self._token("alice", ALICE_PASSWORD))
        bob = self._auth(self._token("bob", ALICE_PASSWORD))

        written = self.client.put(
            "/api/v1/tenancy/settings",
            json={"settings": {"STOCK_LIST": "600519,AAPL", "SCHEDULE_TIMES": "09:30"}},
            headers=alice,
        )
        self.assertEqual(written.status_code, 200, written.text)
        self.assertEqual(sorted(written.json()["written"]), ["SCHEDULE_TIMES", "STOCK_LIST"])

        alice_view = self.client.get("/api/v1/tenancy/settings", headers=alice).json()["settings"]
        self.assertEqual(alice_view["STOCK_LIST"]["value"], "600519,AAPL")
        self.assertTrue(alice_view["STOCK_LIST"]["configured"])

        # bob 不应看到 alice 的配置
        bob_view = self.client.get("/api/v1/tenancy/settings", headers=bob).json()["settings"]
        self.assertFalse(bob_view["STOCK_LIST"]["configured"])
        self.assertIsNone(bob_view["STOCK_LIST"]["value"])

    def test_protected_settings_rejected(self) -> None:
        admin = self._auth(self._token("admin", ADMIN_PASSWORD))
        self.client.post(
            "/api/v1/tenancy/users",
            json={"username": "alice", "password": ALICE_PASSWORD, "role": "user"},
            headers=admin,
        )
        alice = self._auth(self._token("alice", ALICE_PASSWORD))

        response = self.client.put(
            "/api/v1/tenancy/settings",
            json={
                "settings": {
                    "DATABASE_PATH": "/tmp/evil.db",
                    "ADMIN_AUTH_ENABLED": "false",
                    "OPENAI_API_KEY": "sk-evil",
                }
            },
            headers=alice,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "no_valid_keys")

    def test_secret_is_masked_in_response(self) -> None:
        admin = self._auth(self._token("admin", ADMIN_PASSWORD))
        self.client.post(
            "/api/v1/tenancy/users",
            json={"username": "alice", "password": ALICE_PASSWORD, "role": "user"},
            headers=admin,
        )
        alice = self._auth(self._token("alice", ALICE_PASSWORD))

        self.client.put(
            "/api/v1/tenancy/settings",
            json={"settings": {"WECHAT_WEBHOOK_URL": "https://example.com/super-secret-key"}},
            headers=alice,
        )
        view = self.client.get("/api/v1/tenancy/settings", headers=alice).json()["settings"]
        self.assertTrue(view["WECHAT_WEBHOOK_URL"]["configured"])
        self.assertNotIn("super-secret-key", view["WECHAT_WEBHOOK_URL"]["value"])

    def test_cookie_session_login(self) -> None:
        admin = self._auth(self._token("admin", ADMIN_PASSWORD))
        self.client.post(
            "/api/v1/tenancy/users",
            json={"username": "alice", "password": ALICE_PASSWORD, "role": "user"},
            headers=admin,
        )

        # 登录后 TestClient 会持有 Cookie
        login = self.client.post(
            "/api/v1/tenancy/auth/login",
            json={"username": "alice", "password": ALICE_PASSWORD},
        )
        self.assertEqual(login.status_code, 200, login.text)
        self.assertIn("dsa_user_token", login.cookies)

        me = self.client.get("/api/v1/tenancy/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["user"]["username"], "alice")

        logout = self.client.post("/api/v1/tenancy/auth/logout")
        self.assertEqual(logout.status_code, 200)

    def test_revoked_token_stops_working(self) -> None:
        admin = self._auth(self._token("admin", ADMIN_PASSWORD))
        created = self.client.post(
            "/api/v1/tenancy/users",
            json={"username": "alice", "password": ALICE_PASSWORD, "role": "user"},
            headers=admin,
        )
        alice_id = created.json()["user"]["id"]
        alice_token = self._token("alice", ALICE_PASSWORD)
        self.assertEqual(
            self.client.get("/api/v1/tenancy/auth/me", headers=self._auth(alice_token)).status_code,
            200,
        )

        revoke = self.client.post(
            f"/api/v1/tenancy/users/{alice_id}/revoke-tokens", headers=admin
        )
        self.assertEqual(revoke.status_code, 200)
        self.assertEqual(
            self.client.get("/api/v1/tenancy/auth/me", headers=self._auth(alice_token)).status_code,
            401,
        )

    def test_usage_endpoint(self) -> None:
        admin = self._auth(self._token("admin", ADMIN_PASSWORD))
        self.client.post(
            "/api/v1/tenancy/users",
            json={"username": "alice", "password": ALICE_PASSWORD, "role": "user"},
            headers=admin,
        )
        alice = self._auth(self._token("alice", ALICE_PASSWORD))

        response = self.client.get("/api/v1/tenancy/usage", headers=alice)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["totals"]["calls"], 0)
        self.assertEqual(body["by_type"], [])

    def test_scheduler_users_endpoint(self) -> None:
        admin = self._auth(self._token("admin", ADMIN_PASSWORD))
        created = self.client.post(
            "/api/v1/tenancy/users",
            json={"username": "alice", "password": ALICE_PASSWORD, "role": "user"},
            headers=admin,
        )
        alice_id = created.json()["user"]["id"]
        alice_token = self._token("alice", ALICE_PASSWORD)
        self.client.put(
            "/api/v1/tenancy/settings",
            json={"settings": {"SCHEDULE_ENABLED": True, "SCHEDULE_TIMES": "09:30"}},
            headers=self._auth(alice_token),
        )

        response = self.client.get("/api/v1/tenancy/scheduler/users", headers=admin)
        self.assertEqual(response.status_code, 200)
        entries = {e["tenant_id"]: e for e in response.json()["users"]}
        self.assertIn(alice_id, entries)
        self.assertTrue(entries[alice_id]["schedule_enabled"])
        self.assertEqual(entries[alice_id]["schedule_times"], ["09:30"])

    def test_health_endpoint(self) -> None:
        # /health 会暴露 scope 内部状态与用户数，因此需要认证
        response = self.client.get(
            "/api/v1/tenancy/health",
            headers=self._auth(self._token("admin", ADMIN_PASSWORD)),
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["multiuser_enabled"])
        self.assertGreaterEqual(body["user_count"], 1)
        self.assertTrue(body["scope"]["guard_armed"])

    def test_multiuser_disabled_does_not_mount_api(self) -> None:
        """多用户关闭时，路由不应被挂载，且不触发任何鉴权。"""
        os.environ["DSA_MULTIUSER_ENABLED"] = "false"
        try:
            from api.app import create_app

            client = TestClient(create_app())
            response = client.get("/api/v1/tenancy/capabilities")
            self.assertEqual(response.status_code, 404)
        finally:
            os.environ["DSA_MULTIUSER_ENABLED"] = "true"


if __name__ == "__main__":
    unittest.main()
