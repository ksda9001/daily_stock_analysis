# -*- coding: utf-8 -*-
"""微信用户股票咨询与自动加自选测试。"""

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
from src.tenancy.settings import extract_inquired_stocks, resolve_stock_list

TEST_WECHAT_ID = "wx_test_inquire_user_01"


class WatchlistInquireTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="dsa-inquire-test-")
        self._saved_env = {
            key: os.environ.get(key)
            for key in ("DATABASE_PATH", "DSA_MULTIUSER_ENABLED", "ADMIN_AUTH_ENABLED")
        }
        os.environ["DATABASE_PATH"] = str(Path(self._tmpdir) / "inquire_test.db")
        os.environ["DSA_MULTIUSER_ENABLED"] = "true"

        from src.config import Config
        from src.storage import DatabaseManager

        DatabaseManager.reset_instance()
        Config.reset_instance()
        reset_bootstrap_state()
        tenancy_tokens.reset_token_secret_cache()

        self.db = DatabaseManager.get_instance()
        tenancy_service.reset_password(SYSTEM_TENANT_ID, new_password="admin-password-test")

        # 创建一个已绑定微信的测试用户
        self.user = tenancy_service.create_user(
            username="wechat_trader",
            password="trader-password-123",
            actor=None,
        )
        tenancy_service.bind_wechat(
            username="wechat_trader",
            password="trader-password-123",
            wechat_id=TEST_WECHAT_ID,
            wechat_nickname="测试交易员",
        )

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

    def test_extract_inquired_stocks_positive(self) -> None:
        stocks = extract_inquired_stocks("帮我看看 600519")
        codes = [c for c, _ in stocks]
        self.assertIn("600519", codes)

        stocks_name = extract_inquired_stocks("贵州茅台走势怎么样")
        codes_name = [c for c, _ in stocks_name]
        self.assertIn("600519", codes_name)

    def test_extract_inquired_stocks_negative(self) -> None:
        self.assertEqual(extract_inquired_stocks("从自选删除 600519"), [])
        self.assertEqual(extract_inquired_stocks("查看我的自选股"), [])
        self.assertEqual(extract_inquired_stocks("清空自选股"), [])
        self.assertEqual(extract_inquired_stocks("解绑"), [])
        self.assertEqual(extract_inquired_stocks("登录 user pass"), [])
        self.assertEqual(extract_inquired_stocks("whoami"), [])
        self.assertEqual(extract_inquired_stocks("今天大盘走势怎么样"), [])

    def test_watchlist_inquire_unbound_user(self) -> None:
        resp = self.client.post(
            "/api/v1/tenancy/watchlist/inquire",
            json={"wechatId": "unknown_wechat_user", "query": "帮我看看 600519"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["reason"], "unbound_recipient")

    def test_watchlist_inquire_bound_user_add_stock(self) -> None:
        resp = self.client.post(
            "/api/v1/tenancy/watchlist/inquire",
            json={"wechatId": TEST_WECHAT_ID, "query": "帮我分析一下 600519 茅台"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["username"], "wechat_trader")
        self.assertTrue(len(data["stocks"]) > 0)
        self.assertIn("600519", [s["code"] for s in data["stocks"]])
        self.assertIn("已默认将", data["notice"])

        # 检查数据库中该用户的自选股
        user_stocks = resolve_stock_list(self.user.id)
        self.assertIn("600519", user_stocks["stocks"])

    def test_watchlist_inquire_negative_query(self) -> None:
        resp = self.client.post(
            "/api/v1/tenancy/watchlist/inquire",
            json={"wechatId": TEST_WECHAT_ID, "query": "从自选股删除 600519"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["stocks"], [])
        self.assertEqual(data.get("action"), "none")


if __name__ == "__main__":
    unittest.main()
