# -*- coding: utf-8 -*-
"""多租户表结构引导（含 ``tenant_id`` 列的增量迁移）。

表分类
------
``SCOPED_TABLES``（26 张）
    按用户隔离。查询/写入必须带上 ``tenant_id``。

``GLOBAL_TABLES``（6 张）
    全局共享的**公开市场数据**，不按用户隔离。

    这是一个**刻意的设计决定**，不是遗漏：

    - ``stock_daily`` / ``fundamental_snapshot``：行情与基本面快照，
      对所有用户完全相同，共享可以避免 N 倍冗余与 N 倍数据源请求。
    - ``news_intel`` / ``intelligence_items`` / ``intelligence_sources``：
      公开新闻与情报的抓取缓存，内容本身不含用户私有信息。
    - ``schema_migrations``：框架级元数据。

    共享的代价是「用户 A 抓到的新闻用户 B 也能看到」——对公开市场数据
    而言这是可接受的，而且能显著降低被数据源限流的概率。

迁移策略
--------
沿用上游 ``src/storage.py`` 中既有的 ``_ensure_*`` 增量迁移风格
（``inspect(engine)`` + 原生 ``ALTER TABLE``），不引入 Alembic：

1. 建三张租户表（``Base.metadata.create_all`` 的定向子集）；
2. 为每张 ``SCOPED_TABLES`` 补 ``tenant_id`` 列（若物理表存在且缺列）；
3. 建 ``ix_<table>_tenant_id`` 索引；
4. 把历史数据回填为系统属主；
5. 播种系统属主用户行。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from src.tenancy.context import ROLE_ADMIN, SYSTEM_TENANT_ID
from src.tenancy.models import TenantUser, TenantUserSetting, TenantAuditLog

logger = logging.getLogger(__name__)

#: 系统属主的默认用户名
SYSTEM_OWNER_USERNAME = "admin"

#: 按用户隔离的表
SCOPED_TABLES: frozenset = frozenset({
    # 分析
    "analysis_history",
    "backtest_results",
    "backtest_summaries",
    "screening_runs",
    # 对话 / Agent
    "conversation_messages",
    "conversation_session_states",
    "conversation_summaries",
    "agent_provider_turns",
    # 用量
    "llm_usage",
    # 告警
    "alert_rules",
    "alert_triggers",
    "alert_notifications",
    "alert_cooldowns",
    # 决策信号
    "decision_signals",
    "decision_signal_outcomes",
    "decision_signal_feedback",
    # 技能观点
    "skill_opinion_samples",
    "skill_opinion_outcomes",
    # 持仓
    "portfolio_accounts",
    "portfolio_trades",
    "portfolio_cash_ledger",
    "portfolio_corporate_actions",
    "portfolio_positions",
    "portfolio_position_lots",
    "portfolio_daily_snapshots",
    "portfolio_fx_rates",
})

#: 全局共享（公开市场数据）
GLOBAL_TABLES: frozenset = frozenset({
    "stock_daily",
    "news_intel",
    "intelligence_sources",
    "intelligence_items",
    "fundamental_snapshot",
    "schema_migrations",
})

TENANT_COLUMN = "tenant_id"
_TENANT_TABLES = (TenantUser, TenantUserSetting, TenantAuditLog)


@dataclass
class SchemaReport:
    """引导结果，便于启动日志与测试断言。"""

    tables_created: List[str] = field(default_factory=list)
    columns_added: List[str] = field(default_factory=list)
    indexes_added: List[str] = field(default_factory=list)
    rows_backfilled: Dict[str, int] = field(default_factory=dict)
    system_owner_created: bool = False
    warnings: List[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(
            self.tables_created
            or self.columns_added
            or self.indexes_added
            or self.system_owner_created
            or any(self.rows_backfilled.values())
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tables_created": self.tables_created,
            "columns_added": self.columns_added,
            "indexes_added": self.indexes_added,
            "rows_backfilled": self.rows_backfilled,
            "system_owner_created": self.system_owner_created,
            "warnings": self.warnings,
            "changed": self.changed,
        }


def ensure_tenancy_schema(
    engine: Engine,
    *,
    backfill_tenant_id: int = SYSTEM_TENANT_ID,
    create_system_owner: bool = True,
) -> SchemaReport:
    """幂等地把数据库升级到多租户结构。

    本函数必须在任何业务查询之前执行（``DatabaseManager.__init__`` 中调用）。
    """
    report = SchemaReport()
    inspector = inspect(engine)

    # ---- 1. 建租户表 -------------------------------------------------
    for model in _TENANT_TABLES:
        name = model.__tablename__
        if not inspector.has_table(name):
            model.__table__.create(engine, checkfirst=True)
            report.tables_created.append(name)

    # ---- 2. 补 tenant_id 列 -------------------------------------------
    inspector = inspect(engine)
    for table_name in sorted(SCOPED_TABLES):
        if not inspector.has_table(table_name):
            report.warnings.append(f"scoped table missing, skipped: {table_name}")
            continue
        existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
        if TENANT_COLUMN in existing_columns:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(f'ALTER TABLE "{table_name}" ADD COLUMN tenant_id INTEGER')
                )
            report.columns_added.append(table_name)
        except Exception as exc:  # noqa: BLE001 - 单表失败不应阻断其余表
            report.warnings.append(f"failed to add tenant_id to {table_name}: {exc}")
            logger.error("[tenancy] failed to add tenant_id to %s: %s", table_name, exc)

    # ---- 3. 建索引 ---------------------------------------------------
    inspector = inspect(engine)
    for table_name in sorted(SCOPED_TABLES):
        if not inspector.has_table(table_name):
            continue
        index_name = f"ix_{table_name}_tenant_id"
        existing_indexes = {idx["name"] for idx in inspector.get_indexes(table_name)}
        if index_name in existing_indexes:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        f'CREATE INDEX IF NOT EXISTS "{index_name}" '
                        f'ON "{table_name}" (tenant_id)'
                    )
                )
            report.indexes_added.append(index_name)
        except Exception as exc:  # noqa: BLE001
            report.warnings.append(f"failed to create {index_name}: {exc}")
            logger.error("[tenancy] failed to create index %s: %s", index_name, exc)

    # ---- 4. 回填历史数据 ---------------------------------------------
    for table_name in sorted(SCOPED_TABLES):
        if not inspector.has_table(table_name):
            continue
        try:
            with engine.begin() as conn:
                result = conn.execute(
                    text(
                        f'UPDATE "{table_name}" SET tenant_id = :tenant '
                        f"WHERE tenant_id IS NULL"
                    ),
                    {"tenant": int(backfill_tenant_id)},
                )
            count = int(result.rowcount or 0)
            if count:
                report.rows_backfilled[table_name] = count
        except Exception as exc:  # noqa: BLE001
            report.warnings.append(f"failed to backfill {table_name}: {exc}")
            logger.error("[tenancy] failed to backfill %s: %s", table_name, exc)

    # ---- 5. 播种系统属主 ---------------------------------------------
    if create_system_owner:
        report.system_owner_created = _seed_system_owner(engine)

    return report


def _seed_system_owner(engine: Engine) -> bool:
    """确保系统属主用户行存在。返回是否新建。"""
    from sqlalchemy.orm import Session

    with Session(engine) as session:
        existing = session.get(TenantUser, SYSTEM_TENANT_ID)
        if existing is not None:
            # 系统属主必须始终是启用状态的管理员，纠正历史误改。
            if existing.role != ROLE_ADMIN or not existing.is_system:
                existing.role = ROLE_ADMIN
                existing.is_system = True
                existing.status = "active"
                session.commit()
            return False

        # 若上游管理员已设置密码，直接复用其摘要，避免出现「两个密码」。
        password_hash = _read_legacy_admin_hash() or "!"
        session.add(
            TenantUser(
                id=SYSTEM_TENANT_ID,
                username=SYSTEM_OWNER_USERNAME,
                display_name="系统属主",
                password_hash=password_hash,
                role=ROLE_ADMIN,
                status="active",
                is_system=True,
                token_version=1,
            )
        )
        session.commit()
        logger.info("[tenancy] seeded system owner user id=%s", SYSTEM_TENANT_ID)
        return True


def _read_legacy_admin_hash() -> Optional[str]:
    """读取上游 ``.admin_password_hash``（若存在且格式有效）。"""
    from src.auth import _get_credential_path  # noqa: PLC2701 - 复用上游路径解析
    from src.tenancy.passwords import parse_password_hash

    try:
        path = _get_credential_path()
        if not path.exists():
            return None
        raw = path.read_text().strip()
    except OSError as exc:  # pragma: no cover - 防御性分支
        logger.warning("[tenancy] cannot read legacy admin credential: %s", exc)
        return None
    return raw if parse_password_hash(raw) else None


def sync_system_owner_password() -> bool:
    """把上游管理员密码摘要同步到系统属主行。

    在管理员通过 ``/api/v1/auth/login`` 登录成功后调用，这样系统属主
    也能用于签发 Bearer Token，而不必再单独设置一次密码。
    """
    from sqlalchemy.orm import Session

    from src.storage import get_db

    encoded = _read_legacy_admin_hash()
    if not encoded:
        return False

    try:
        engine = get_db()._engine  # noqa: SLF001 - 内部单例访问
    except Exception as exc:  # pragma: no cover - 数据库未初始化
        logger.warning("[tenancy] cannot sync system owner password: %s", exc)
        return False

    with Session(engine) as session:
        owner = session.get(TenantUser, SYSTEM_TENANT_ID)
        if owner is None:
            return False
        if owner.password_hash == encoded:
            return False
        owner.password_hash = encoded
        session.commit()
        logger.info("[tenancy] synced admin password into system owner user")
        return True


def scoped_table_names() -> List[str]:
    """返回已排序的按用户隔离表名，供测试与文档使用。"""
    return sorted(SCOPED_TABLES)
