# -*- coding: utf-8 -*-
"""共享行（公共数据）与线程池上下文传播。

这个文件针对的是同一类缺陷的两面：

1. **共享行被误当私有数据** —— 大盘复盘是公开市场数据，却因为盖上了
   某个用户的 ``tenant_id``，导致别的租户（如 CowAgent 服务账号）
   查不到。表现为「报告凭空消失」，且返回 404 而不是 403。
2. **后台任务丢上下文** —— ``ThreadPoolExecutor.submit`` 不复制
   ``contextvars``，于是后台线程里读不到当前用户，归属被收窄到系统属主。

两者都不报错，所以必须有测试钉住。

设计约束（写测试时请遵守）：

- 共享行的放宽**只针对声明了规则的行**。因此必须有一条「私有行仍然隔离」
  的反向断言，否则「顺手把整张表放宽」这种回归不会被发现。
- 删除权收口到管理员，所以要有「非管理员删不掉」+「管理员删得掉」两条。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from src.tenancy import service as tenancy_service
from src.tenancy.context import (
    ROLE_ADMIN,
    SHARED_TENANT_ID,
    SYSTEM_TENANT_ID,
    bind_user,
    bypass_tenant_scope,
    current_user_id,
)
from src.tenancy.schema import SHARED_ROW_RULES, ensure_tenancy_schema
from src.tenancy.scope import disarm_scope_guard
from src.utils.context_exec import submit_with_context

MARKET_REVIEW_TYPE = SHARED_ROW_RULES["analysis_history"][1]


class SharedRowTestBase(unittest.TestCase):
    """全新 SQLite + 多用户环境（与 tests/test_tenancy.py 同构）。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="dsa-shared-row-")
        self._db_path = Path(self._tmpdir) / "shared_row_test.db"
        self._saved_env = {
            key: os.environ.get(key)
            for key in (
                "DATABASE_PATH",
                "DSA_MULTIUSER_ENABLED",
                "DSA_SYSTEM_TENANT_ID",
                "DSA_SHARED_TENANT_ID",
                "DSA_TENANCY_STRICT_RAW_SQL",
            )
        }
        os.environ["DATABASE_PATH"] = str(self._db_path)
        os.environ["DSA_MULTIUSER_ENABLED"] = "true"
        os.environ.pop("DSA_TENANCY_STRICT_RAW_SQL", None)
        os.environ.pop("DSA_SHARED_TENANT_ID", None)

        from src.config import Config
        from src.storage import DatabaseManager
        from src.tenancy import tokens as tenancy_tokens
        from src.tenancy.install import reset_bootstrap_state
        from src.tenancy.scope import reset_raw_sql_warnings

        DatabaseManager.reset_instance()
        Config.reset_instance()
        reset_bootstrap_state()
        tenancy_tokens.reset_token_secret_cache()
        reset_raw_sql_warnings()

        self.db = DatabaseManager.get_instance()
        self.engine = self.db._engine  # noqa: SLF001 - 结构断言需要直连引擎

    def tearDown(self) -> None:
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

    # -- helpers ---------------------------------------------------------

    def _add_history(self, code: str, report_type: str = "detailed") -> int:
        from src.storage import AnalysisHistory

        with self.db.session_scope() as session:
            row = AnalysisHistory(
                code=code, name=f"name-{code}", report_type=report_type
            )
            session.add(row)
            session.flush()
            return row.id

    def _raw_tenant_id(self, row_id: int) -> int:
        from src.storage import AnalysisHistory

        with bypass_tenant_scope("assert raw tenant_id"):
            with self.db.session_scope() as session:
                return session.get(AnalysisHistory, row_id).tenant_id

    def _visible_codes(self, user_id: int, role: str = "user") -> set:
        from src.storage import AnalysisHistory

        with bind_user(user_id, role=role):
            with self.db.session_scope() as session:
                return {row.code for row in session.query(AnalysisHistory).all()}


# ---------------------------------------------------------------------------
# 1. 归属盖章
# ---------------------------------------------------------------------------

class SharedRowStampingTests(SharedRowTestBase):
    def test_market_review_is_stamped_shared(self) -> None:
        """大盘复盘不属于任何用户，必须盖 SHARED_TENANT_ID。"""
        alice = tenancy_service.create_user(username="alice", password="pass1234")
        with bind_user(alice.id, username="alice"):
            row_id = self._add_history("MARKET", report_type=MARKET_REVIEW_TYPE)

        self.assertEqual(self._raw_tenant_id(row_id), SHARED_TENANT_ID)

    def test_private_report_keeps_owner(self) -> None:
        """反向断言：普通研报仍归触发者，不能被顺手放宽。"""
        alice = tenancy_service.create_user(username="alice", password="pass1234")
        with bind_user(alice.id, username="alice"):
            row_id = self._add_history("600519", report_type="detailed")

        self.assertEqual(self._raw_tenant_id(row_id), alice.id)

    def test_explicit_tenant_id_is_not_overwritten(self) -> None:
        """显式传入的归属不应被盖章逻辑覆盖。"""
        from src.storage import AnalysisHistory

        alice = tenancy_service.create_user(username="alice", password="pass1234")
        with bind_user(alice.id, username="alice"):
            with self.db.session_scope() as session:
                row = AnalysisHistory(
                    code="MARKET",
                    report_type=MARKET_REVIEW_TYPE,
                    tenant_id=alice.id,
                )
                session.add(row)
                session.flush()
                row_id = row.id

        self.assertEqual(self._raw_tenant_id(row_id), alice.id)


# ---------------------------------------------------------------------------
# 2. 读可见性
# ---------------------------------------------------------------------------

class SharedRowVisibilityTests(SharedRowTestBase):
    def test_market_review_visible_to_every_tenant(self) -> None:
        alice = tenancy_service.create_user(username="alice", password="pass1234")
        bob = tenancy_service.create_user(username="bob", password="pass1234")

        with bind_user(alice.id, username="alice"):
            self._add_history("MARKET", report_type=MARKET_REVIEW_TYPE)

        self.assertIn("MARKET", self._visible_codes(bob.id), "共享行必须对其它租户可见")
        self.assertIn("MARKET", self._visible_codes(alice.id))

    def test_market_review_visible_without_context(self) -> None:
        """无上下文（收窄到系统属主）也要能看到公共数据。"""
        alice = tenancy_service.create_user(username="alice", password="pass1234")
        with bind_user(alice.id, username="alice"):
            self._add_history("MARKET", report_type=MARKET_REVIEW_TYPE)

        self.assertIn("MARKET", self._visible_codes(SYSTEM_TENANT_ID, role=ROLE_ADMIN))

    def test_private_reports_stay_isolated(self) -> None:
        """核心回归保护：共享规则不得把整张表变成公共表。"""
        alice = tenancy_service.create_user(username="alice", password="pass1234")
        bob = tenancy_service.create_user(username="bob", password="pass1234")

        with bind_user(alice.id, username="alice"):
            self._add_history("ALICE1")
            self._add_history("MARKET", report_type=MARKET_REVIEW_TYPE)
        with bind_user(bob.id, username="bob"):
            self._add_history("BOB1")

        self.assertEqual(self._visible_codes(bob.id), {"BOB1", "MARKET"})
        self.assertEqual(self._visible_codes(alice.id), {"ALICE1", "MARKET"})

    def test_null_report_type_rows_stay_isolated(self) -> None:
        """``report_type`` 为 NULL 的行不能被共享谓词放行。

        ``NULL = 'market_review'`` 求值为 NULL，若实现写成纯 OR 而没考虑
        三值逻辑，这些行会意外变成公共数据。
        """
        alice = tenancy_service.create_user(username="alice", password="pass1234")
        bob = tenancy_service.create_user(username="bob", password="pass1234")

        with bind_user(alice.id, username="alice"):
            with self.db.session_scope() as session:
                from src.storage import AnalysisHistory

                session.add(AnalysisHistory(code="NULLTYPE", name="n"))
                session.flush()

        self.assertEqual(self._visible_codes(bob.id), set())


# ---------------------------------------------------------------------------
# 3. Core 语句回退路径
# ---------------------------------------------------------------------------

class CoreStatementFallbackTests(SharedRowTestBase):
    """不带 ORM 实体的 Core 语句会走 WHERE 回退路径。

    这条分支曾被漏测，代价是 ``AttributeError: 'Table' object has no
    attribute 'tenant_id'`` —— ``Table`` 不代理属性访问，必须走
    ``table.c[...]``。因此这里显式钉住它。
    """

    def _seed(self):
        alice = tenancy_service.create_user(username="alice", password="pass1234")
        bob = tenancy_service.create_user(username="bob", password="pass1234")
        with bind_user(alice.id, username="alice"):
            self._add_history("ALICE1")
            self._add_history("MARKET", report_type=MARKET_REVIEW_TYPE)
        with bind_user(bob.id, username="bob"):
            self._add_history("BOB1")
        return alice, bob

    def _core_codes(self, user_id: int) -> set:
        from sqlalchemy import select

        from src.storage import Base

        table = Base.metadata.tables["analysis_history"]
        with bind_user(user_id, username="core-user"):
            with self.db.session_scope() as session:
                return {row[0] for row in session.execute(select(table.c.code))}

    def test_core_select_does_not_raise(self):
        alice, bob = self._seed()
        # 只要能跑完不抛异常，就说明列解析走对了分支
        self.assertIsInstance(self._core_codes(bob.id), set)

    def test_core_select_includes_shared_rows(self):
        alice, bob = self._seed()
        self.assertEqual(self._core_codes(bob.id), {"BOB1", "MARKET"})

    def test_core_select_keeps_private_rows_isolated(self):
        alice, bob = self._seed()
        self.assertNotIn("ALICE1", self._core_codes(bob.id))

    def test_core_update_cannot_touch_other_tenants_rows(self) -> None:
        """Core UPDATE 走 WHERE 回退路径，必须同样受租户约束。

        若 ``Update.get_final_froms()`` 取不到表，回退路径会静默失效 ——
        表现是「一条 UPDATE 就能改掉所有人的数据」。这里钉住它。
        """
        from sqlalchemy import select, update

        from src.storage import AnalysisHistory, Base

        table = Base.metadata.tables["analysis_history"]
        alice, bob = self._seed()

        with bind_user(bob.id, username="bob"):
            with self.db.session_scope() as session:
                affected = session.execute(
                    update(table).values(analysis_summary="by-bob")
                ).rowcount
        # 恰好 2 行：bob 自己的 1 行 + 共享的 1 行（UPDATE 对共享行刻意放宽）。
        # 若回退路径失效会是 3 行（连 alice 的一起改）。
        self.assertEqual(affected, 2, "Core UPDATE 必须被租户谓词限制")

        with bind_user(alice.id, username="alice"):
            with self.db.session_scope() as session:
                summary = session.execute(
                    select(AnalysisHistory.analysis_summary).where(
                        AnalysisHistory.code == "ALICE1"
                    )
                ).scalar()
        self.assertNotEqual(summary, "by-bob", "跨租户 UPDATE 必须被拦住")

    def test_core_delete_cannot_touch_other_tenants_rows(self) -> None:
        from sqlalchemy import delete, select

        from src.storage import AnalysisHistory, Base

        table = Base.metadata.tables["analysis_history"]
        alice, bob = self._seed()

        with bind_user(bob.id, username="bob"):
            with self.db.session_scope() as session:
                deleted = session.execute(delete(table)).rowcount
        # 恰好 1 行：只有 bob 自己的。共享行需管理员（DELETE 收口），
        # alice 的行不归 bob。若回退路径失效会是 3 行。
        self.assertEqual(deleted, 1, "Core DELETE 必须被租户谓词限制")

        with bypass_tenant_scope("verify alice row survived"):
            with self.db.session_scope() as session:
                survived = (
                    session.execute(
                        select(AnalysisHistory.id).where(AnalysisHistory.code == "ALICE1")
                    )
                    .scalars()
                    .all()
                )
        self.assertEqual(len(survived), 1, "跨租户 DELETE 必须被拦住")


# ---------------------------------------------------------------------------
# 4. 写权限：UPDATE 放开，DELETE 仅管理员
# ---------------------------------------------------------------------------

class SharedRowWriteTests(SharedRowTestBase):
    def _market_review_id(self) -> int:
        alice = tenancy_service.create_user(username="alice", password="pass1234")
        with bind_user(alice.id, username="alice"):
            return self._add_history("MARKET", report_type=MARKET_REVIEW_TYPE)

    def test_shared_row_is_updatable_by_any_tenant(self) -> None:
        """后台任务（可能以任意身份运行）需要能维护公共报告。"""
        row_id = self._market_review_id()
        bob = tenancy_service.create_user(username="bob", password="pass1234")

        from src.storage import AnalysisHistory

        with bind_user(bob.id, username="bob"):
            with self.db.session_scope() as session:
                updated = (
                    session.query(AnalysisHistory)
                    .filter(AnalysisHistory.id == row_id)
                    .update({"analysis_summary": "由 bob 的任务回填"}, synchronize_session=False)
                )
        self.assertEqual(updated, 1)

    def test_non_admin_cannot_delete_shared_row(self) -> None:
        row_id = self._market_review_id()
        bob = tenancy_service.create_user(username="bob", password="pass1234")

        from src.storage import AnalysisHistory

        with bind_user(bob.id, username="bob", role="user"):
            with self.db.session_scope() as session:
                deleted = (
                    session.query(AnalysisHistory)
                    .filter(AnalysisHistory.id == row_id)
                    .delete(synchronize_session=False)
                )
        self.assertEqual(deleted, 0, "普通用户不得删除公共报告")

        with bypass_tenant_scope("verify survived"):
            with self.db.session_scope() as session:
                self.assertIsNotNone(session.get(AnalysisHistory, row_id))

    def test_admin_can_delete_shared_row(self) -> None:
        row_id = self._market_review_id()

        from src.storage import AnalysisHistory

        with bind_user(SYSTEM_TENANT_ID, username="admin", role=ROLE_ADMIN):
            with self.db.session_scope() as session:
                deleted = (
                    session.query(AnalysisHistory)
                    .filter(AnalysisHistory.id == row_id)
                    .delete(synchronize_session=False)
                )
        self.assertEqual(deleted, 1, "管理员应能清理公共报告")

    def test_private_row_still_not_deletable_cross_tenant(self) -> None:
        """共享规则不得削弱原有隔离。"""
        alice = tenancy_service.create_user(username="alice", password="pass1234")
        bob = tenancy_service.create_user(username="bob", password="pass1234")
        with bind_user(alice.id, username="alice"):
            row_id = self._add_history("ALICE1")

        from src.storage import AnalysisHistory

        with bind_user(bob.id, username="bob"):
            with self.db.session_scope() as session:
                deleted = (
                    session.query(AnalysisHistory)
                    .filter(AnalysisHistory.id == row_id)
                    .delete(synchronize_session=False)
                )
        self.assertEqual(deleted, 0)


# ---------------------------------------------------------------------------
# 5. 迁移：把历史遗留的共享行归位
# ---------------------------------------------------------------------------

class SharedRowMigrationTests(SharedRowTestBase):
    def test_migration_moves_existing_market_review_rows(self) -> None:
        """修复前写下的 market_review 行（tenant_id=系统属主）必须被搬到共享归属。"""
        from src.storage import AnalysisHistory

        with bypass_tenant_scope("simulate pre-fix row"):
            with self.db.session_scope() as session:
                row = AnalysisHistory(
                    code="MARKET",
                    report_type=MARKET_REVIEW_TYPE,
                    tenant_id=SYSTEM_TENANT_ID,
                )
                session.add(row)
                session.flush()
                row_id = row.id

        report = ensure_tenancy_schema(self.engine)

        self.assertEqual(self._raw_tenant_id(row_id), SHARED_TENANT_ID)
        self.assertEqual(report.rows_shared.get("analysis_history"), 1)

    def test_migration_leaves_private_rows_alone(self) -> None:
        from src.storage import AnalysisHistory

        with bypass_tenant_scope("simulate private row"):
            with self.db.session_scope() as session:
                row = AnalysisHistory(code="600519", report_type="detailed")
                session.add(row)
                session.flush()
                row_id = row.id

        ensure_tenancy_schema(self.engine)

        self.assertEqual(self._raw_tenant_id(row_id), SYSTEM_TENANT_ID)

    def test_migration_is_idempotent(self) -> None:
        from src.storage import AnalysisHistory

        with bypass_tenant_scope("simulate pre-fix row"):
            with self.db.session_scope() as session:
                session.add(
                    AnalysisHistory(code="MARKET", report_type=MARKET_REVIEW_TYPE)
                )
                session.flush()

        first = ensure_tenancy_schema(self.engine)
        second = ensure_tenancy_schema(self.engine)

        self.assertEqual(first.rows_shared.get("analysis_history"), 1)
        self.assertIsNone(second.rows_shared.get("analysis_history"), "二次迁移不应再改动")


# ---------------------------------------------------------------------------
# 6. 线程池上下文传播
# ---------------------------------------------------------------------------

class ContextPropagationTests(SharedRowTestBase):
    def test_bare_submit_drops_context(self) -> None:
        """记录缺陷本身：裸 ``executor.submit`` 会丢 contextvars。

        这条断言是「缺陷复现」而不是「期望行为」—— 它存在的意义是：
        如果哪天 Python 改了 ``submit`` 的语义，这里会红，提醒我们
        重新审视 :func:`submit_with_context` 是否还必要。
        """
        alice = tenancy_service.create_user(username="alice", password="pass1234")
        with ThreadPoolExecutor(max_workers=1) as executor:
            with bind_user(alice.id, username="alice"):
                seen = executor.submit(current_user_id).result()
        self.assertIsNone(seen, "裸 submit 本应丢上下文；若不再是 None 说明前提变了")

    def test_submit_with_context_preserves_context(self) -> None:
        alice = tenancy_service.create_user(username="alice", password="pass1234")
        with ThreadPoolExecutor(max_workers=1) as executor:
            with bind_user(alice.id, username="alice"):
                seen = submit_with_context(executor, current_user_id).result()
        self.assertEqual(seen, alice.id)

    def test_context_is_snapshot_at_submit_time(self) -> None:
        """提交后退出 with 块，任务里仍应看到提交时刻的身份。"""
        alice = tenancy_service.create_user(username="alice", password="pass1234")
        with ThreadPoolExecutor(max_workers=1) as executor:
            with bind_user(alice.id, username="alice"):
                future = submit_with_context(executor, current_user_id)
            # 上下文已退出，但任务还没跑
            self.assertIsNone(current_user_id())
            self.assertEqual(future.result(), alice.id)

    def test_exceptions_still_propagate(self) -> None:
        """包装层不得吞掉异常或改变 future 语义。"""

        def boom() -> None:
            raise ValueError("boom")

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = submit_with_context(executor, boom)
            with self.assertRaises(ValueError):
                future.result()

    def test_kwargs_are_forwarded(self) -> None:
        def add(a: int, *, b: int) -> int:
            return a + b

        with ThreadPoolExecutor(max_workers=1) as executor:
            self.assertEqual(submit_with_context(executor, add, 1, b=2).result(), 3)


# ---------------------------------------------------------------------------
# 7. 端点层：可见性与删除权不一致时的行为
# ---------------------------------------------------------------------------

class SharedRowEndpointTests(SharedRowTestBase):
    """``delete_history_by_code`` 的循环以「查得到就删得掉」为前提。

    共享行打破了这个前提：它对所有租户可读，但删除权收口到管理员。
    前端确实会以 ``MARKET`` 调用该接口（``HomePage.tsx`` 的
    ``historyApi.deleteByCode``），因此这条路径必须能优雅降级，
    而不是把「权限边界」当成「删除失败」抛 500。
    """

    def _delete_by_code(self, code: str):
        try:
            from api.v1.endpoints.history import delete_history_by_code
        except Exception:  # pragma: no cover - 依赖缺失时跳过
            self.skipTest("fastapi is not installed in this test environment")
        return delete_history_by_code(code, db_manager=self.db)

    def test_delete_by_code_does_not_fail_on_shared_rows(self) -> None:
        """普通用户清理 MARKET 历史：不报错，也不真的删掉公共报告。"""
        alice = tenancy_service.create_user(username="alice", password="pass1234")
        with bind_user(alice.id, username="alice"):
            row_id = self._add_history("MARKET", report_type=MARKET_REVIEW_TYPE)

        with bind_user(alice.id, username="alice"):
            response = self._delete_by_code("MARKET")

        self.assertEqual(response.deleted, 0, "普通用户不得删除公共报告")
        self.assertEqual(self._raw_tenant_id(row_id), SHARED_TENANT_ID)

    def test_delete_by_code_still_removes_private_rows(self) -> None:
        """反向断言：修复不能把「按代码删除」整体废掉。"""
        from src.storage import AnalysisHistory

        alice = tenancy_service.create_user(username="alice", password="pass1234")
        with bind_user(alice.id, username="alice"):
            row_id = self._add_history("600519", report_type="detailed")

        with bind_user(alice.id, username="alice"):
            response = self._delete_by_code("600519")

        self.assertEqual(response.deleted, 1)
        with bypass_tenant_scope("verify private row deleted"):
            with self.db.session_scope() as session:
                self.assertIsNone(session.get(AnalysisHistory, row_id))

    def test_delete_by_code_removes_private_and_keeps_shared(self) -> None:
        """混合批次：同一 code 下既有私有行又有共享行。

        这是最容易写出死循环的形状 —— 私有行删掉后循环继续，再次取到
        的仍是那条删不掉的共享行。必须证明它能收敛。
        """
        from src.storage import AnalysisHistory

        alice = tenancy_service.create_user(username="alice", password="pass1234")
        with bind_user(alice.id, username="alice"):
            private_id = self._add_history("MARKET", report_type="detailed")
            shared_id = self._add_history("MARKET", report_type=MARKET_REVIEW_TYPE)

        with bind_user(alice.id, username="alice"):
            response = self._delete_by_code("MARKET")

        self.assertEqual(response.deleted, 1, "私有行应被删除")
        with bypass_tenant_scope("verify mixed batch outcome"):
            with self.db.session_scope() as session:
                self.assertIsNone(
                    session.get(AnalysisHistory, private_id), "私有行应已删除"
                )
                self.assertIsNotNone(
                    session.get(AnalysisHistory, shared_id), "共享行应留存"
                )

    def test_admin_can_delete_shared_rows_by_code(self) -> None:
        """管理员保留清理公共报告的权力。"""
        from src.storage import AnalysisHistory

        alice = tenancy_service.create_user(username="alice", password="pass1234")
        with bind_user(alice.id, username="alice"):
            row_id = self._add_history("MARKET", report_type=MARKET_REVIEW_TYPE)

        with bind_user(SYSTEM_TENANT_ID, username="admin", role=ROLE_ADMIN):
            response = self._delete_by_code("MARKET")

        self.assertEqual(response.deleted, 1)
        with bypass_tenant_scope("verify shared row deleted by admin"):
            with self.db.session_scope() as session:
                self.assertIsNone(session.get(AnalysisHistory, row_id))


# ---------------------------------------------------------------------------
# 8. 裸 SQL 审计：不得误伤 ORM 写入
# ---------------------------------------------------------------------------

class RawSqlAuditTests(SharedRowTestBase):
    """ORM 的 INSERT 不经过 ``do_orm_execute``（flush 是会话内部操作），
    因此拿不到租户标记。审计若只看「有没有标记」，就会把 ORM 写入当成
    裸 SQL —— 严格模式下表现为「所有写入都失败」，且报错信息还会建议
    「改用 ORM 路径」，而调用方本来就在用 ORM。

    这里同时钉住两个方向：ORM 写入必须放行，真正的裸写必须仍被拦住。
    """

    def test_orm_insert_passes_strict_audit(self) -> None:
        os.environ["DSA_TENANCY_STRICT_RAW_SQL"] = "true"
        alice = tenancy_service.create_user(username="alice", password="pass1234")

        with bind_user(alice.id, username="alice"):
            row_id = self._add_history("600519")

        self.assertEqual(self._raw_tenant_id(row_id), alice.id)

    def test_raw_sql_insert_still_blocked_in_strict_mode(self) -> None:
        """反向断言：修复不得把审计整体关掉。"""
        from sqlalchemy import text

        from src.tenancy.scope import TenantScopeError

        os.environ["DSA_TENANCY_STRICT_RAW_SQL"] = "true"
        alice = tenancy_service.create_user(username="alice", password="pass1234")

        with bind_user(alice.id, username="alice"):
            with self.assertRaises(TenantScopeError):
                with self.engine.begin() as conn:
                    conn.execute(
                        text(
                            "INSERT INTO analysis_history (code, name, report_type) "
                            "VALUES ('RAW1', 'raw', 'detailed')"
                        )
                    )

    def test_orm_insert_does_not_raise_raw_sql_warning(self) -> None:
        """默认模式下也不该留下误导性告警。"""
        from src.tenancy import scope as scope_module

        alice = tenancy_service.create_user(username="alice", password="pass1234")
        with bind_user(alice.id, username="alice"):
            self._add_history("600519")

        self.assertNotIn(
            "Insert:analysis_history",
            scope_module._raw_sql_warned,
            "ORM 写入不应被记为裸 SQL",
        )


if __name__ == "__main__":
    unittest.main()
