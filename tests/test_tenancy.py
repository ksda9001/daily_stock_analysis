# -*- coding: utf-8 -*-
"""多租户（multi-tenancy）单元测试。

覆盖范围
--------
1. 密码哈希与校验
2. Bearer Token 的签发、校验、吊销
3. 表结构引导（``tenant_id`` 列、租户表、系统属主播种）
4. **租户数据隔离**（最关键的一组）
5. 写入归属盖章
6. 按用户配置解析与 ``Config`` 副本叠加
7. 按用户调度注册
8. 受保护配置键拒绝写入

这些测试刻意使用真实的 SQLite 文件与真实的 SQLAlchemy 会话，
而不是 mock——租户隔离的正确性只能由真实 SQL 行为来证明。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# 让测试在没有安装 LLM 运行时依赖时也能跑
# 刻意的副作用导入：让后续模块能 import litellm（未安装时用 Mock 顶替）
try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from sqlalchemy import inspect

from src.tenancy import service as tenancy_service
from src.tenancy import tokens as tenancy_tokens
from src.tenancy.context import (
    ROLE_ADMIN,
    ROLE_USER,
    SYSTEM_TENANT_ID,
    bind_system,
    bind_user,
    bypass_tenant_scope,
    current_user_id,
    multiuser_enabled,
)
from src.tenancy.install import bootstrap_tenancy, reset_bootstrap_state
from src.tenancy.passwords import (
    hash_password,
    validate_password,
    validate_username,
    verify_password_hash,
)
from src.tenancy.schema import SCOPED_TABLES, ensure_tenancy_schema
from src.tenancy.scope import (
    TenantScopeError,
    disarm_scope_guard,
    effective_tenant_id,
    is_guard_armed,
    reset_raw_sql_warnings,
)


class TenancyTestBase(unittest.TestCase):
    """为每个测试准备一个全新的 SQLite 数据库与多用户环境。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="dsa-tenancy-")
        self._db_path = Path(self._tmpdir) / "tenancy_test.db"
        self._saved_env = {
            key: os.environ.get(key)
            for key in (
                "DATABASE_PATH",
                "DSA_MULTIUSER_ENABLED",
                "DSA_SYSTEM_TENANT_ID",
                "DSA_API_TOKEN_TTL_SECONDS",
                "DSA_TENANCY_STRICT_RAW_SQL",
            )
        }
        os.environ["DATABASE_PATH"] = str(self._db_path)
        os.environ["DSA_MULTIUSER_ENABLED"] = "true"
        os.environ.pop("DSA_TENANCY_STRICT_RAW_SQL", None)

        # 重置所有跨测试的模块级状态
        from src.config import Config
        from src.storage import DatabaseManager

        DatabaseManager.reset_instance()
        Config.reset_instance()
        reset_bootstrap_state()
        tenancy_tokens.reset_token_secret_cache()
        reset_raw_sql_warnings()

        self.db = DatabaseManager.get_instance()
        self.engine = self.db._engine  # noqa: SLF001 - 测试需要直连引擎做结构断言

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


# ---------------------------------------------------------------------------
# 密码
# ---------------------------------------------------------------------------

class PasswordTests(TenancyTestBase):
    def test_hash_and_verify_roundtrip(self) -> None:
        encoded = hash_password("s3cret-pass")
        self.assertTrue(verify_password_hash("s3cret-pass", encoded))
        self.assertFalse(verify_password_hash("wrong-pass", encoded))

    def test_hash_is_salted(self) -> None:
        first = hash_password("same-password")
        second = hash_password("same-password")
        self.assertNotEqual(first, second, "相同密码必须产生不同摘要（加盐）")

    def test_malformed_hash_never_verifies(self) -> None:
        for bad in ("", "not-a-hash", "abc:", ":def", "!!!:???"):
            self.assertFalse(verify_password_hash("whatever", bad))

    def test_validation_rules(self) -> None:
        self.assertIsNotNone(validate_username("ab"))
        self.assertIsNotNone(validate_username("has space"))
        self.assertIsNotNone(validate_username("bad$char"))
        self.assertIsNone(validate_username("good_user-1.0"))
        self.assertIsNotNone(validate_password("12345"))
        self.assertIsNone(validate_password("123456"))


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------

class TokenTests(TenancyTestBase):
    def test_issue_and_verify(self) -> None:
        token = tenancy_tokens.issue_token(user_id=7, token_version=3)
        self.assertTrue(token)
        claims = tenancy_tokens.verify_token(token)
        self.assertIsNotNone(claims)
        self.assertEqual(claims.user_id, 7)
        self.assertEqual(claims.token_version, 3)
        self.assertFalse(claims.is_expired)

    def test_tampered_token_rejected(self) -> None:
        token = tenancy_tokens.issue_token(user_id=7, token_version=1)
        payload, _, signature = token.rpartition(".")
        forged = f"{payload}.{'0' * len(signature)}"
        self.assertIsNone(tenancy_tokens.verify_token(forged))

        # 篡改 user_id 也会破坏签名
        parts = token.split(".")
        parts[1] = "99"
        self.assertIsNone(tenancy_tokens.verify_token(".".join(parts)))

    def test_expired_token_rejected(self) -> None:
        token = tenancy_tokens.issue_token(user_id=1, token_version=1, ttl_seconds=0)
        claims = tenancy_tokens.verify_token(token)
        # ttl_seconds=0 时立即过期
        self.assertIsNone(claims)

    def test_garbage_rejected(self) -> None:
        for bad in ("", "abc", "v1.1.1.1.1", "v2.1.1.9999999999.nonce.sig"):
            self.assertIsNone(tenancy_tokens.verify_token(bad))

    def test_bearer_header_parsing(self) -> None:
        extract = tenancy_tokens.extract_bearer_token
        self.assertEqual(extract("Bearer abc123"), "abc123")
        self.assertEqual(extract("bearer abc123"), "abc123")
        self.assertIsNone(extract("Basic abc123"))
        self.assertIsNone(extract(None))
        self.assertIsNone(extract("Bearer "))

    def test_token_version_bump_revokes(self) -> None:
        user = tenancy_service.create_user(username="alice", password="pass1234")
        token = tenancy_service.issue_api_token(user.id)
        self.assertIsNotNone(tenancy_service.resolve_principal_from_token(token))

        tenancy_service.revoke_api_tokens(user.id)
        self.assertIsNone(
            tenancy_service.resolve_principal_from_token(token),
            "递增 token_version 后旧 Token 必须失效",
        )

    def test_disabled_user_token_rejected(self) -> None:
        user = tenancy_service.create_user(username="bob", password="pass1234")
        token = tenancy_service.issue_api_token(user.id)
        tenancy_service.update_user(user.id, status="disabled")
        self.assertIsNone(tenancy_service.resolve_principal_from_token(token))


# ---------------------------------------------------------------------------
# 表结构引导
# ---------------------------------------------------------------------------

class SchemaBootstrapTests(TenancyTestBase):
    def test_tenant_tables_created(self) -> None:
        inspector = inspect(self.engine)
        for table in ("dsa_users", "dsa_user_settings", "dsa_tenancy_audit"):
            self.assertTrue(inspector.has_table(table), f"缺少租户表 {table}")

    def test_tenant_column_added_to_all_scoped_tables(self) -> None:
        inspector = inspect(self.engine)
        missing = []
        for table_name in sorted(SCOPED_TABLES):
            if not inspector.has_table(table_name):
                missing.append(f"{table_name} (表不存在)")
                continue
            columns = {col["name"] for col in inspector.get_columns(table_name)}
            if "tenant_id" not in columns:
                missing.append(table_name)
        self.assertEqual(missing, [], f"以下表缺少 tenant_id 列: {missing}")

    def test_global_tables_do_not_get_tenant_column(self) -> None:
        inspector = inspect(self.engine)
        for table_name in ("stock_daily", "news_intel"):
            if not inspector.has_table(table_name):
                continue
            columns = {col["name"] for col in inspector.get_columns(table_name)}
            self.assertNotIn(
                "tenant_id",
                columns,
                f"{table_name} 是全局共享表，不应被加上 tenant_id",
            )

    def test_system_owner_seeded_and_protected(self) -> None:
        owner = tenancy_service.get_user(SYSTEM_TENANT_ID)
        self.assertIsNotNone(owner, "系统属主必须被播种")
        self.assertTrue(owner.is_system)
        self.assertEqual(owner.role, ROLE_ADMIN)

        with self.assertRaises(tenancy_service.TenancyError):
            tenancy_service.delete_user(SYSTEM_TENANT_ID)
        with self.assertRaises(tenancy_service.TenancyError):
            tenancy_service.update_user(SYSTEM_TENANT_ID, status="disabled")
        with self.assertRaises(tenancy_service.TenancyError):
            tenancy_service.update_user(SYSTEM_TENANT_ID, role=ROLE_USER)

    def test_bootstrap_is_idempotent(self) -> None:
        first = ensure_tenancy_schema(self.engine)
        self.assertEqual(first.columns_added, [], "第二次引导不应再补列")
        self.assertFalse(first.system_owner_created, "系统属主不应被重复创建")

    def test_bootstrap_returns_none_when_multiuser_disabled(self) -> None:
        """多用户关闭时必须完全跳过数据库结构引导。"""
        os.environ["DSA_MULTIUSER_ENABLED"] = "false"
        try:
            self.assertIsNone(bootstrap_tenancy(self.engine))
        finally:
            os.environ["DSA_MULTIUSER_ENABLED"] = "true"

    def test_bind_system_binds_system_owner(self) -> None:
        """后台任务通过 bind_system() 以系统属主身份运行。"""
        with bind_system():
            self.assertEqual(current_user_id(), SYSTEM_TENANT_ID)
            self.assertEqual(effective_tenant_id(), SYSTEM_TENANT_ID)
        self.assertIsNone(current_user_id())

    def test_portfolio_owner_id_is_untouched(self) -> None:
        """上游 portfolio_accounts.owner_id（String）必须保持原样。"""
        columns = {col["name"]: col for col in inspect(self.engine).get_columns("portfolio_accounts")}
        self.assertIn("owner_id", columns)
        self.assertIn("tenant_id", columns)
        self.assertNotEqual(columns["owner_id"]["type"].__class__.__name__, "Integer")


# ---------------------------------------------------------------------------
# 租户隔离（核心）
# ---------------------------------------------------------------------------

class TenantIsolationTests(TenancyTestBase):
    def _add_history(self, code: str) -> int:
        from src.storage import AnalysisHistory

        with self.db.session_scope() as session:
            row = AnalysisHistory(code=code, name=f"name-{code}")
            session.add(row)
            session.flush()
            return row.id

    def test_guard_is_armed(self) -> None:
        self.assertTrue(is_guard_armed(), "多用户模式下租户守卫必须已启用")

    def test_write_stamps_tenant_id(self) -> None:
        from src.storage import AnalysisHistory

        alice = tenancy_service.create_user(username="alice", password="pass1234")
        with bind_user(alice.id, username="alice"):
            row_id = self._add_history("600519")

        with bypass_tenant_scope("assert raw row"):
            with self.db.session_scope() as session:
                row = session.get(AnalysisHistory, row_id)
                self.assertEqual(row.tenant_id, alice.id)

    def test_users_cannot_see_each_other(self) -> None:
        alice = tenancy_service.create_user(username="alice", password="pass1234")
        bob = tenancy_service.create_user(username="bob", password="pass1234")

        with bind_user(alice.id, username="alice"):
            self._add_history("ALICE1")
        with bind_user(bob.id, username="bob"):
            self._add_history("BOB1")

        from src.storage import AnalysisHistory

        with bind_user(alice.id, username="alice"):
            with self.db.session_scope() as session:
                codes = {row.code for row in session.query(AnalysisHistory).all()}
            self.assertEqual(codes, {"ALICE1"})

        with bind_user(bob.id, username="bob"):
            with self.db.session_scope() as session:
                codes = {row.code for row in session.query(AnalysisHistory).all()}
            self.assertEqual(codes, {"BOB1"})

    def test_missing_context_falls_back_to_system_owner(self) -> None:
        """没有用户上下文时必须收窄到系统属主，而不是看到全部数据。"""
        alice = tenancy_service.create_user(username="alice", password="pass1234")
        with bind_user(alice.id, username="alice"):
            self._add_history("ALICE1")

        from src.storage import AnalysisHistory

        # 不绑定任何用户 → 归属应为系统属主
        self.assertEqual(effective_tenant_id(), SYSTEM_TENANT_ID)
        with self.db.session_scope() as session:
            codes = {row.code for row in session.query(AnalysisHistory).all()}
        self.assertEqual(codes, set(), "无上下文时必须看不到其它用户的数据")

    def test_bypass_scope_sees_everything(self) -> None:
        alice = tenancy_service.create_user(username="alice", password="pass1234")
        bob = tenancy_service.create_user(username="bob", password="pass1234")
        with bind_user(alice.id):
            self._add_history("ALICE1")
        with bind_user(bob.id):
            self._add_history("BOB1")

        from src.storage import AnalysisHistory

        with bypass_tenant_scope("cross-tenant report"):
            with self.db.session_scope() as session:
                codes = {row.code for row in session.query(AnalysisHistory).all()}
        self.assertEqual(codes, {"ALICE1", "BOB1"})

    def test_delete_is_scoped(self) -> None:
        alice = tenancy_service.create_user(username="alice", password="pass1234")
        bob = tenancy_service.create_user(username="bob", password="pass1234")
        with bind_user(alice.id):
            alice_row = self._add_history("ALICE1")
        with bind_user(bob.id):
            bob_row = self._add_history("BOB1")

        from src.storage import AnalysisHistory

        # alice 尝试删除 bob 的记录：应当删不到任何行
        with bind_user(alice.id):
            with self.db.session_scope() as session:
                deleted = (
                    session.query(AnalysisHistory)
                    .filter(AnalysisHistory.id == bob_row)
                    .delete(synchronize_session=False)
                )
            self.assertEqual(deleted, 0, "跨租户删除必须被隔离")

        with bypass_tenant_scope("verify bob row survived"):
            with self.db.session_scope() as session:
                self.assertIsNotNone(session.get(AnalysisHistory, bob_row))
                self.assertIsNotNone(session.get(AnalysisHistory, alice_row))

    def test_isolation_covers_all_scoped_tables(self) -> None:
        """对每张租户表做一次通用读隔离检查（空表也应被加上谓词）。"""
        alice = tenancy_service.create_user(username="alice", password="pass1234")
        from src.storage import Base

        with bind_user(alice.id):
            with self.db.session_scope() as session:
                for table_name in sorted(SCOPED_TABLES):
                    table = Base.metadata.tables.get(table_name)
                    if table is None or "tenant_id" not in table.c:
                        continue
                    rows = session.execute(table.select()).fetchall()
                    self.assertEqual(rows, [], f"{table_name} 在空库下不应有行")

    def test_strict_raw_sql_mode_raises(self) -> None:
        from sqlalchemy import text

        os.environ["DSA_TENANCY_STRICT_RAW_SQL"] = "true"
        try:
            with self.assertRaises(TenantScopeError):
                with self.db.session_scope() as session:
                    session.execute(text("SELECT * FROM analysis_history")).fetchall()
        finally:
            os.environ.pop("DSA_TENANCY_STRICT_RAW_SQL", None)

    def test_single_user_mode_does_not_scope(self) -> None:
        os.environ["DSA_MULTIUSER_ENABLED"] = "false"
        self.assertFalse(multiuser_enabled())
        self.assertIsNone(current_user_id())
        # 单用户模式：归属解析返回 None 语义（不受限）
        from src.tenancy.scope import should_scope

        self.assertFalse(should_scope())


# ---------------------------------------------------------------------------
# 用户体系
# ---------------------------------------------------------------------------

class UserServiceTests(TenancyTestBase):
    def test_create_and_authenticate(self) -> None:
        user = tenancy_service.create_user(username="carol", password="pass1234", role=ROLE_ADMIN)
        self.assertEqual(user.role, ROLE_ADMIN)
        self.assertTrue(user.is_active)
        self.assertIsNotNone(tenancy_service.authenticate("carol", "pass1234"))
        self.assertIsNone(tenancy_service.authenticate("carol", "wrong"))
        self.assertIsNone(tenancy_service.authenticate("nobody", "pass1234"))

    def test_duplicate_username_rejected(self) -> None:
        tenancy_service.create_user(username="dave", password="pass1234")
        with self.assertRaises(tenancy_service.TenancyError) as ctx:
            tenancy_service.create_user(username="dave", password="pass1234")
        self.assertEqual(ctx.exception.status_code, 409)

    def test_weak_password_rejected(self) -> None:
        with self.assertRaises(tenancy_service.TenancyError):
            tenancy_service.create_user(username="weak", password="123")

    def test_public_dict_never_exposes_hash(self) -> None:
        user = tenancy_service.create_user(username="erin", password="pass1234")
        payload = user.to_public_dict()
        self.assertNotIn("password_hash", payload)
        self.assertEqual(payload["username"], "erin")

    def test_change_password_revokes_tokens(self) -> None:
        user = tenancy_service.create_user(username="frank", password="pass1234")
        token = tenancy_service.issue_api_token(user.id)
        tenancy_service.change_password(
            user.id, current_password="pass1234", new_password="newpass123"
        )
        self.assertIsNone(tenancy_service.resolve_principal_from_token(token))
        self.assertIsNotNone(tenancy_service.authenticate("frank", "newpass123"))

    def test_change_password_wrong_current(self) -> None:
        user = tenancy_service.create_user(username="grace", password="pass1234")
        with self.assertRaises(tenancy_service.TenancyError):
            tenancy_service.change_password(
                user.id, current_password="nope", new_password="newpass123"
            )

    def test_delete_user_clears_settings(self) -> None:
        from src.tenancy.settings import load_user_settings, save_user_settings

        user = tenancy_service.create_user(username="heidi", password="pass1234")
        save_user_settings(user.id, {"STOCK_LIST": "600519,AAPL"})
        self.assertTrue(load_user_settings(user.id))

        tenancy_service.delete_user(user.id)
        self.assertIsNone(tenancy_service.get_user(user.id))
        self.assertEqual(load_user_settings(user.id), {})

    def test_audit_log_written(self) -> None:
        user = tenancy_service.create_user(username="ivan", password="pass1234")
        entries = tenancy_service.list_audit_log(user.id)
        self.assertTrue(any(e["action"] == "user.create" for e in entries))


# ---------------------------------------------------------------------------
# 按用户配置
# ---------------------------------------------------------------------------

class UserSettingsTests(TenancyTestBase):
    def test_save_load_roundtrip(self) -> None:
        from src.tenancy.settings import load_user_settings, save_user_settings

        user = tenancy_service.create_user(username="judy", password="pass1234")
        written = save_user_settings(
            user.id,
            {
                "STOCK_LIST": "600519, aapl ,600519",
                "SCHEDULE_ENABLED": True,
                "SCHEDULE_TIMES": ["09:30", "15:05"],
                "WECHAT_WEBHOOK_URL": "https://example.com/hook",
            },
        )
        self.assertIn("STOCK_LIST", written)

        raw = load_user_settings(user.id)
        self.assertEqual(raw["STOCK_LIST"], "600519,AAPL", "代码应大写并去重")
        self.assertEqual(raw["SCHEDULE_ENABLED"], "true")
        self.assertEqual(raw["SCHEDULE_TIMES"], "09:30,15:05")

    def test_forbidden_keys_rejected(self) -> None:
        from src.tenancy.settings import load_user_settings, save_user_settings

        user = tenancy_service.create_user(username="karl", password="pass1234")
        written = save_user_settings(
            user.id,
            {
                "DATABASE_PATH": "/tmp/evil.db",
                "ADMIN_AUTH_ENABLED": "false",
                "DSA_MULTIUSER_ENABLED": "false",
                "OPENAI_API_KEY": "sk-evil",
                "STOCK_LIST": "600519",
            },
        )
        self.assertEqual(sorted(written), ["STOCK_LIST"])
        raw = load_user_settings(user.id)
        self.assertNotIn("DATABASE_PATH", raw)
        self.assertNotIn("ADMIN_AUTH_ENABLED", raw)
        self.assertNotIn("OPENAI_API_KEY", raw)

    def test_unknown_keys_ignored(self) -> None:
        from src.tenancy.settings import save_user_settings

        user = tenancy_service.create_user(username="laura", password="pass1234")
        self.assertEqual(save_user_settings(user.id, {"NOT_A_REAL_KEY": "x"}), {})

    def test_resolve_setting_falls_back_to_env(self) -> None:
        from src.tenancy.settings import resolve_setting

        os.environ["REPORT_TYPE"] = "detailed"
        try:
            user = tenancy_service.create_user(username="mike", password="pass1234")
            self.assertEqual(resolve_setting("REPORT_TYPE", user.id), "detailed")
            from src.tenancy.settings import save_user_settings

            save_user_settings(user.id, {"REPORT_TYPE": "simple"})
            self.assertEqual(resolve_setting("REPORT_TYPE", user.id), "simple")
        finally:
            os.environ.pop("REPORT_TYPE", None)

    def test_apply_user_overrides_returns_copy(self) -> None:
        from src.config import get_config
        from src.tenancy.settings import apply_user_overrides, save_user_settings

        user = tenancy_service.create_user(username="nina", password="pass1234")
        save_user_settings(
            user.id,
            {
                "STOCK_LIST": "600519,AAPL",
                "WECHAT_WEBHOOK_URL": "https://user.example/hook",
                "SCHEDULE_TIMES": "09:30",
            },
        )

        base = get_config()
        base.stock_list = ["GLOBAL1"]
        original_webhook = getattr(base, "wechat_webhook_url", None)

        effective = apply_user_overrides(base, user.id)
        self.assertIsNot(effective, base, "必须返回副本，不能污染全局 Config")
        self.assertEqual(effective.stock_list, ["600519", "AAPL"])
        self.assertEqual(effective.wechat_webhook_url, "https://user.example/hook")
        self.assertEqual(effective.schedule_times, ["09:30"])

        # 原对象保持不变
        self.assertEqual(base.stock_list, ["GLOBAL1"])
        self.assertEqual(getattr(base, "wechat_webhook_url", None), original_webhook)

    def test_effective_stock_list(self) -> None:
        from src.tenancy.settings import effective_stock_list, save_user_settings

        user = tenancy_service.create_user(username="olga", password="pass1234")
        self.assertIsNone(effective_stock_list(user.id), "未配置时应返回 None（沿用全局）")
        save_user_settings(user.id, {"STOCK_LIST": "600519"})
        self.assertEqual(effective_stock_list(user.id), ["600519"])

    def test_secret_masking(self) -> None:
        from src.tenancy.settings import public_settings_view, save_user_settings

        user = tenancy_service.create_user(username="pete", password="pass1234")
        save_user_settings(user.id, {"WECHAT_WEBHOOK_URL": "https://example.com/very-long-hook-key"})
        view = public_settings_view(user.id)
        self.assertTrue(view["WECHAT_WEBHOOK_URL"]["configured"])
        self.assertIn("*", view["WECHAT_WEBHOOK_URL"]["value"])
        self.assertNotIn("very-long-hook-key", view["WECHAT_WEBHOOK_URL"]["value"])

    def test_delete_user_settings_reverts_to_global(self) -> None:
        from src.tenancy.settings import (
            delete_user_settings,
            load_user_settings,
            save_user_settings,
        )

        user = tenancy_service.create_user(username="quinn", password="pass1234")
        save_user_settings(user.id, {"STOCK_LIST": "600519"})
        self.assertEqual(delete_user_settings(user.id, ["STOCK_LIST"]), 1)
        self.assertNotIn("STOCK_LIST", load_user_settings(user.id))


# ---------------------------------------------------------------------------
# 用量归属
# ---------------------------------------------------------------------------

class UsageAttributionTests(TenancyTestBase):
    def _record(self, call_type: str = "analysis", total: int = 100) -> None:
        self.db.record_llm_usage(
            call_type=call_type,
            model="test-model",
            prompt_tokens=total - 10,
            completion_tokens=10,
            total_tokens=total,
        )

    def test_usage_is_attributed_to_current_user(self) -> None:
        from src.storage import LLMUsage

        alice = tenancy_service.create_user(username="alice", password="pass1234")
        bob = tenancy_service.create_user(username="bob", password="pass1234")

        with bind_user(alice.id, username="alice"):
            self._record(total=100)
        with bind_user(bob.id, username="bob"):
            self._record(total=250)

        with bypass_tenant_scope("assert raw attribution"):
            with self.db.session_scope() as session:
                rows = session.query(LLMUsage).all()
                attribution = {row.tenant_id: row.total_tokens for row in rows}
        self.assertEqual(attribution.get(alice.id), 100)
        self.assertEqual(attribution.get(bob.id), 250)

    def test_usage_summary_is_per_user(self) -> None:
        alice = tenancy_service.create_user(username="alice", password="pass1234")
        bob = tenancy_service.create_user(username="bob", password="pass1234")

        with bind_user(alice.id):
            self._record(total=100)
            self._record(total=50)
        with bind_user(bob.id):
            self._record(total=999)

        alice_summary = tenancy_service.usage_summary(alice.id)
        self.assertEqual(alice_summary["totals"]["calls"], 2)
        self.assertEqual(alice_summary["totals"]["total_tokens"], 150)

        bob_summary = tenancy_service.usage_summary(bob.id)
        self.assertEqual(bob_summary["totals"]["calls"], 1)
        self.assertEqual(bob_summary["totals"]["total_tokens"], 999)

    def test_usage_without_context_goes_to_system_owner(self) -> None:
        from src.storage import LLMUsage

        self._record(total=42)
        with bypass_tenant_scope("assert system attribution"):
            with self.db.session_scope() as session:
                row = session.query(LLMUsage).one()
                tenant_id = row.tenant_id
        self.assertEqual(tenant_id, SYSTEM_TENANT_ID)


# ---------------------------------------------------------------------------
# 按用户调度
# ---------------------------------------------------------------------------

class PerUserScheduleTests(TenancyTestBase):
    def test_schedulable_users_lists_active_only(self) -> None:
        from src.tenancy.settings import save_user_settings

        active = tenancy_service.create_user(username="rita", password="pass1234")
        disabled = tenancy_service.create_user(username="sam", password="pass1234")
        tenancy_service.update_user(disabled.id, status="disabled")
        save_user_settings(active.id, {"SCHEDULE_ENABLED": True, "SCHEDULE_TIMES": "09:30"})

        entries = tenancy_service.list_schedulable_users()
        ids = {e["tenant_id"] for e in entries}
        self.assertIn(active.id, ids)
        self.assertNotIn(disabled.id, ids)

        entry = next(e for e in entries if e["tenant_id"] == active.id)
        self.assertTrue(entry["schedule_enabled"])
        self.assertEqual(entry["schedule_times"], ["09:30"])

    def test_scheduler_registers_named_daily_tasks(self) -> None:
        from src.scheduler import Scheduler
        from src.services.runtime_scheduler import RuntimeSchedulerService
        from src.tenancy.settings import save_user_settings

        user = tenancy_service.create_user(username="tina", password="pass1234")
        save_user_settings(user.id, {"SCHEDULE_ENABLED": True, "SCHEDULE_TIMES": "09:30,15:05"})

        scheduler = Scheduler(register_signals=False)
        svc = RuntimeSchedulerService(owns_schedule=True)
        registered = svc._register_tenant_schedules(scheduler, generation=0)  # noqa: SLF001

        self.assertEqual(registered, [f"tenant-{user.id}"])
        self.assertEqual(scheduler.named_daily_task_names(), [f"tenant-{user.id}"])
        # 两个时间点 → 两个 job
        self.assertEqual(len(scheduler._named_daily_jobs[f"tenant-{user.id}"]), 2)  # noqa: SLF001

        scheduler.stop()
        self.assertEqual(scheduler.named_daily_task_names(), [])

    def test_fanout_targets_exclude_personal_schedules(self) -> None:
        from src.services.runtime_scheduler import RuntimeSchedulerService
        from src.tenancy.settings import save_user_settings

        follower = tenancy_service.create_user(username="uma", password="pass1234")
        personal = tenancy_service.create_user(username="vic", password="pass1234")
        opted_out = tenancy_service.create_user(username="wes", password="pass1234")
        save_user_settings(personal.id, {"SCHEDULE_ENABLED": True, "SCHEDULE_TIMES": "09:30"})
        save_user_settings(opted_out.id, {"SCHEDULE_ENABLED": False})

        svc = RuntimeSchedulerService(owns_schedule=True)
        targets = svc._tenant_fanout_targets()  # noqa: SLF001

        self.assertIn(follower.id, targets)
        self.assertIn(SYSTEM_TENANT_ID, targets)
        self.assertNotIn(personal.id, targets, "有个人时间表的用户不应被全局扇出重复执行")
        self.assertNotIn(opted_out.id, targets, "显式关闭调度的用户不应被执行")

    def test_single_user_mode_has_no_tenant_schedules(self) -> None:
        from src.services.runtime_scheduler import RuntimeSchedulerService

        os.environ["DSA_MULTIUSER_ENABLED"] = "false"
        svc = RuntimeSchedulerService(owns_schedule=True)
        self.assertEqual(svc._tenant_schedule_entries(), [])  # noqa: SLF001
        self.assertEqual(svc._tenant_fanout_targets(), [])  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
