# -*- coding: utf-8 -*-
"""系统设置里的「用户级配置」权限测试。

背景（真实缺陷）：问股页的「上下文压缩」开关读写的是
``/api/v1/system/config``，而该端点要求管理员 —— 普通成员一按就弹
「普通成员无权访问或修改系统设置，请联系管理员」，开关永远存不上。

修复思路：把少数几项（:data:`USER_SCOPED_CONFIG_KEYS`）开放给普通成员，
**但写入的是该用户自己的 ``dsa_user_settings``，不是 ``.env``**。
因此这里要验证的不只是「能用」，还有两条安全边界：

1. 普通成员**看不到**任何非用户级配置（LLM 密钥、Webhook、DATABASE_PATH……）；
2. 普通成员**写不了**任何非用户级配置，且写入不触碰全局 ``.env``。
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
MEMBER_PASSWORD = "member-pass-123"

COMPRESSION_KEY = "AGENT_CONTEXT_COMPRESSION_ENABLED"
CONFIG_ENDPOINT = "/api/v1/system/config"

#: 这些键**绝不允许**出现在普通成员的响应里
NEVER_VISIBLE_KEYS = {
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "LLM_CHANNELS",
    "LITELLM_CONFIG",
    "WECHAT_WEBHOOK_URL",
    "DINGTALK_WEBHOOK_URL",
    "EMAIL_PASSWORD",
    "TELEGRAM_BOT_TOKEN",
    "DATABASE_PATH",
    "ADMIN_AUTH_ENABLED",
    "DSA_MULTIUSER_ENABLED",
    "SCHEDULE_TIMES",
    "STOCK_LIST",
}


class SystemConfigUserScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="dsa-syscfg-scope-")
        self._saved_env = {
            key: os.environ.get(key)
            for key in ("DATABASE_PATH", "DSA_MULTIUSER_ENABLED", "ADMIN_AUTH_ENABLED")
        }
        os.environ["DATABASE_PATH"] = str(Path(self._tmpdir) / "syscfg_test.db")
        os.environ["DSA_MULTIUSER_ENABLED"] = "true"

        from src.config import Config
        from src.storage import DatabaseManager

        DatabaseManager.reset_instance()
        Config.reset_instance()
        reset_bootstrap_state()
        tenancy_tokens.reset_token_secret_cache()

        self.db = DatabaseManager.get_instance()
        tenancy_service.reset_password(SYSTEM_TENANT_ID, new_password=ADMIN_PASSWORD)

        # 必须在 create_app() 之前设置环境变量：install_tenancy 装配时读它
        from api.app import create_app

        self.client = TestClient(create_app())

        self.alice_id = self._create_member("alice")
        self.bob_id = self._create_member("bob")
        self.alice = self._auth(self._token("alice", MEMBER_PASSWORD))
        self.bob = self._auth(self._token("bob", MEMBER_PASSWORD))
        self.admin = self._auth(self._token("admin", ADMIN_PASSWORD))

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

    def _create_member(self, username: str) -> int:
        created = self.client.post(
            "/api/v1/tenancy/users",
            json={"username": username, "password": MEMBER_PASSWORD, "role": "user"},
            headers=self._auth(self._token("admin", ADMIN_PASSWORD)),
        )
        self.assertEqual(created.status_code, 201, created.text)
        return int(created.json()["user"]["id"])

    def _get_config(self, headers: dict) -> dict:
        response = self.client.get(CONFIG_ENDPOINT, params={"include_schema": "false"}, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _put_config(self, headers: dict, items: list) -> object:
        payload = self._get_config(headers)
        return self.client.put(
            CONFIG_ENDPOINT,
            json={
                "config_version": payload["config_version"],
                "mask_token": payload.get("mask_token", "******"),
                "reload_now": True,
                "items": items,
            },
            headers=headers,
        )

    @staticmethod
    def _item_value(payload: dict, key: str):
        for item in payload["items"]:
            if item["key"] == key:
                return item["value"]
        return None

    def _effective(self, key: str, user_id: int):
        from src.tenancy.settings import effective_config_value

        return effective_config_value(key, user_id)

    @staticmethod
    def _error_detail(response, expected_status: int) -> dict:
        """断言状态码并取出错误体；失败时把原始响应体贴进错误信息。

        本应用的 ``HTTPException`` 处理器在 ``detail`` 已是
        ``{error, message}`` 形态时会**直接把它当响应体返回**（不再套
        ``{"detail": ...}``），所以这里两种形态都要认。
        """
        if response.status_code != expected_status:
            raise AssertionError(
                f"期望 {expected_status}，实际 {response.status_code}：{response.text}"
            )
        body = response.json()
        if not isinstance(body, dict):
            raise AssertionError(f"响应体不是对象：{response.text}")
        detail = body.get("detail", body)
        if not isinstance(detail, dict):
            raise AssertionError(f"错误体不是对象：{response.text}")
        return detail

    # -- 读取权限 -----------------------------------------------------

    def test_member_sees_only_user_scoped_keys(self) -> None:
        from src.tenancy.settings import USER_SCOPED_CONFIG_KEYS

        payload = self._get_config(self.alice)
        keys = {item["key"] for item in payload["items"]}
        self.assertEqual(keys, set(USER_SCOPED_CONFIG_KEYS), "普通成员只应看到用户级配置项")

    def test_member_never_sees_sensitive_or_global_keys(self) -> None:
        payload = self._get_config(self.alice)
        keys = {item["key"] for item in payload["items"]}
        leaked = keys & NEVER_VISIBLE_KEYS
        self.assertEqual(leaked, set(), f"响应里泄露了不该可见的键: {sorted(leaked)}")
        # 也不该顺带泄露模型清单
        self.assertEqual(payload.get("llm_model_providers"), [])

    def test_admin_still_sees_full_config(self) -> None:
        payload = self._get_config(self.admin)
        keys = {item["key"] for item in payload["items"]}
        self.assertGreater(len(keys), 20, "管理员应拿到完整配置项")
        self.assertTrue(keys & NEVER_VISIBLE_KEYS, "管理员应能看到敏感键（受掩码保护）")
        self.assertNotIn("user-scope:", payload["config_version"])

    # -- 写入权限 -----------------------------------------------------

    def test_member_can_toggle_context_compression(self) -> None:
        response = self._put_config(
            self.alice, [{"key": COMPRESSION_KEY, "value": "true"}]
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["updated_keys"], [COMPRESSION_KEY])
        self.assertFalse(body["reload_triggered"], "用户级写入不应触发全局重载")

        # 自己的设置里能看到
        view = self.client.get("/api/v1/tenancy/settings", headers=self.alice).json()["settings"]
        self.assertTrue(view[COMPRESSION_KEY]["configured"])
        self.assertEqual(view[COMPRESSION_KEY]["source"], "user")
        self.assertEqual(view[COMPRESSION_KEY]["effective_value"], "true")

        # 回读系统配置接口也应是 true
        self.assertEqual(self._item_value(self._get_config(self.alice), COMPRESSION_KEY), "true")

    def test_toggle_does_not_touch_global_env(self) -> None:
        from src.config import get_config
        from src.services.system_config_service import SystemConfigService

        manager = SystemConfigService()._manager
        version_before = manager.get_config_version()
        runtime_before = getattr(get_config(), "agent_context_compression_enabled", None)

        response = self._put_config(self.alice, [{"key": COMPRESSION_KEY, "value": "true"}])
        self.assertEqual(response.status_code, 200, response.text)

        self.assertEqual(
            manager.get_config_version(), version_before, "用户级写入不得改动 .env"
        )
        self.assertEqual(
            getattr(get_config(), "agent_context_compression_enabled", None),
            runtime_before,
            "用户级写入不得改动全局运行时配置",
        )

    def test_toggle_is_isolated_per_user(self) -> None:
        before_bob, _source = self._effective(COMPRESSION_KEY, self.bob_id)

        response = self._put_config(self.alice, [{"key": COMPRESSION_KEY, "value": "true"}])
        self.assertEqual(response.status_code, 200, response.text)

        alice_value, alice_source = self._effective(COMPRESSION_KEY, self.alice_id)
        bob_value, bob_source = self._effective(COMPRESSION_KEY, self.bob_id)

        self.assertTrue(alice_value)
        self.assertEqual(alice_source, "user")
        self.assertEqual(bob_value, before_bob, "bob 不应被 alice 的开关影响")
        self.assertEqual(bob_source, "global")

    def test_member_cannot_write_non_user_scoped_key(self) -> None:
        response = self._put_config(
            self.alice,
            [
                {"key": COMPRESSION_KEY, "value": "true"},
                {"key": "STOCK_LIST", "value": "600519"},
            ],
        )
        detail = self._error_detail(response, 403)
        self.assertEqual(detail["error"], "forbidden")
        self.assertEqual(detail["message"], "普通成员无权访问或修改系统设置，请联系管理员")
        self.assertEqual(detail["forbidden_keys"], ["STOCK_LIST"])

        # 整批拒绝：合法的那一项也不能落库
        value, source = self._effective(COMPRESSION_KEY, self.alice_id)
        self.assertEqual(source, "global", "混合请求必须整批拒绝，不能部分生效")
        self.assertFalse(bool(value))

    def test_member_cannot_write_secret_key(self) -> None:
        response = self._put_config(
            self.alice, [{"key": "OPENAI_API_KEY", "value": "sk-evil"}]
        )
        detail = self._error_detail(response, 403)
        self.assertEqual(detail["forbidden_keys"], ["OPENAI_API_KEY"])

    def test_member_bool_value_is_validated(self) -> None:
        response = self._put_config(
            self.alice, [{"key": COMPRESSION_KEY, "value": "maybe"}]
        )
        detail = self._error_detail(response, 400)
        self.assertEqual(detail["error"], "validation_failed")

    # -- 生效链路 -----------------------------------------------------

    def test_effective_value_falls_back_to_global(self) -> None:
        from src.config import get_config

        base = get_config()
        base.agent_context_compression_enabled = True

        value, source = self._effective(COMPRESSION_KEY, self.bob_id)
        self.assertTrue(value)
        self.assertEqual(source, "global", "未设置个人偏好时应回落到全局值")

        payload = self._get_config(self.bob)
        self.assertEqual(
            self._item_value(payload, COMPRESSION_KEY),
            "true",
            "接口返回的必须是生效值，而不是 None",
        )

    def test_tenant_config_applies_user_override(self) -> None:
        """Agent 问股链路真正消费的 Config 必须带上用户偏好。"""
        from src.config import get_config
        from src.tenancy.context import bind_user
        from src.tenancy.settings import save_user_settings, tenant_config

        base = get_config()
        base.agent_context_compression_enabled = False
        save_user_settings(self.alice_id, {COMPRESSION_KEY: "true"})

        with bind_user(self.alice_id, username="alice"):
            effective = tenant_config(base)

        self.assertIsNot(effective, base, "必须返回副本，不能污染全局 Config")
        self.assertTrue(effective.agent_context_compression_enabled)
        self.assertFalse(base.agent_context_compression_enabled, "原对象不得被改动")

    def test_tenant_config_is_noop_without_principal(self) -> None:
        from src.config import get_config
        from src.tenancy.settings import tenant_config

        base = get_config()
        self.assertIs(tenant_config(base), base, "没有用户上下文时必须原样返回")

    # -- 其余系统设置端点仍然只对管理员开放 ---------------------------

    def test_secret_consuming_endpoints_require_admin(self) -> None:
        """``use_saved_secret`` 会动用已保存的密钥，普通成员必须被拦住。"""
        endpoints = (
            ("/api/v1/system/config/llm/test-channel", {}),
            ("/api/v1/system/config/llm/discover-models", {}),
            ("/api/v1/system/config/notification/test-channel", {"channel": "wechat"}),
            ("/api/v1/system/config/generation-backends/smoke-test", {}),
        )
        for path, body in endpoints:
            with self.subTest(path=path):
                response = self.client.post(path, json=body, headers=self.alice)
                self.assertEqual(response.status_code, 403, f"{path} 应拒绝普通成员: {response.text}")

    def test_single_user_mode_is_unaffected(self) -> None:
        """关闭多用户后，系统设置回到「本机用户全权」的旧行为。"""
        os.environ["DSA_MULTIUSER_ENABLED"] = "false"
        try:
            from src.config import Config
            from src.storage import DatabaseManager

            DatabaseManager.reset_instance()
            Config.reset_instance()
            reset_bootstrap_state()

            from api.app import create_app

            client = TestClient(create_app())
            response = client.get(CONFIG_ENDPOINT, params={"include_schema": "false"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertGreater(len(response.json()["items"]), 20)
        finally:
            os.environ["DSA_MULTIUSER_ENABLED"] = "true"


if __name__ == "__main__":
    unittest.main()
