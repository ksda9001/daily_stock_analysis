# -*- coding: utf-8 -*-
"""租户隔离执行点（SQLAlchemy 会话级）。

为什么在会话层做，而不是逐条查询手工加条件
------------------------------------------
上游 ``src/storage.py`` 有 4300+ 行、上百个查询方法。逐条添加
``WHERE tenant_id = ?`` 需要改动上百处，且**新增查询时极易遗漏**——
遗漏的表现是「悄悄看到别人的数据」，而不是报错。

因此这里采用「单一执行点」策略：在 SQLAlchemy 的 ``do_orm_execute``
事件上统一施加租户谓词。任何 ORM 语句（SELECT / UPDATE / DELETE）
只要涉及 :data:`src.tenancy.schema.SCOPED_TABLES` 中的表，就自动被
限制在当前用户范围内。

失败策略：fail-closed
---------------------
- 多用户模式关闭 → 完全不干预，行为与上游一致。
- 多用户模式开启 → 归属恒为 ``current_user_id() or SYSTEM_TENANT_ID``。

也就是说「上下文缺失」只会收窄到系统属主，**绝不会放宽到全部数据**。
请求路径上，中间件保证一定会绑定身份；后台任务（调度器 / CLI）通过
:func:`src.tenancy.context.bind_user` 显式绑定，或退化为系统属主。

已知边界
--------
``do_orm_execute`` 只覆盖 ORM 语句。绕过 ORM 的裸 SQL
（``connection.execute(text(...))``）不会被改写——上游仅在
``DatabaseManager.__init__`` 的迁移阶段使用裸 SQL，且该阶段发生在
:func:`arm_scope_guard` 之前。为防回归，:func:`_install_raw_sql_audit`
会对「守卫启用后仍触碰租户表的裸 SQL」发出告警，严格模式下直接报错。
"""

from __future__ import annotations

import logging
import os
import re
import threading
from contextlib import contextmanager
from typing import Iterator, Optional, Set

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, with_loader_criteria
from sqlalchemy.sql.schema import Table

from src.tenancy.context import (
    SYSTEM_TENANT_ID,
    current_user_id,
    multiuser_enabled,
    scope_bypassed,
)
from src.tenancy.schema import SCOPED_TABLES

logger = logging.getLogger(__name__)

_TENANCY_MARKER = "_dsa_tenancy_scoped"
_STRICT_RAW_SQL_ENV = "DSA_TENANCY_STRICT_RAW_SQL"
_TRUTHY = {"1", "true", "yes", "on"}

_install_lock = threading.RLock()
_installed = False
_guard_armed = False
_raw_sql_warned: Set[str] = set()

#: 裸 SQL 中出现的租户表名（用于审计，不做改写）
_SCOPED_TABLE_RE = re.compile(
    r"\b(" + "|".join(sorted(re.escape(name) for name in SCOPED_TABLES)) + r")\b",
    re.IGNORECASE,
)


class TenantScopeError(RuntimeError):
    """严格模式下检测到未受租户约束的裸 SQL。"""


# ---------------------------------------------------------------------------
# 归属解析
# ---------------------------------------------------------------------------

def effective_tenant_id() -> int:
    """返回当前应当生效的数据归属用户 ID。

    这是整个租户隔离的**唯一**归属判定函数。多用户模式下
    ``None`` 上下文会收窄到系统属主，而不是放开。
    """
    user_id = current_user_id()
    if user_id is None:
        return SYSTEM_TENANT_ID
    return int(user_id)


def should_scope() -> bool:
    """当前语句是否应当施加租户谓词。"""
    if not multiuser_enabled():
        return False
    if scope_bypassed():
        return False
    if not _guard_armed:
        # 迁移阶段（建表 / 补列 / 回填）必须先于守卫启用，
        # 否则回填语句自身会被租户谓词限制而漏改历史数据。
        return False
    return True


def arm_scope_guard() -> None:
    """启用租户守卫。必须在数据库结构引导完成之后调用。"""
    global _guard_armed
    _guard_armed = True
    logger.info("[tenancy] tenant scope guard armed")


def disarm_scope_guard() -> None:
    """关闭租户守卫（测试与迁移脚本使用）。"""
    global _guard_armed
    _guard_armed = False


def is_guard_armed() -> bool:
    return _guard_armed


# ---------------------------------------------------------------------------
# 语句改写
# ---------------------------------------------------------------------------

def _walk_froms(fromclause, out: Set[Table]) -> None:
    """把 FROM 子句中出现的物理表收集到 ``out``。"""
    if fromclause is None:
        return
    if isinstance(fromclause, Table):
        out.add(fromclause)
        return
    left = getattr(fromclause, "left", None)
    right = getattr(fromclause, "right", None)
    if left is not None or right is not None:
        _walk_froms(left, out)
        _walk_froms(right, out)
        return
    element = getattr(fromclause, "element", None)
    if element is not None:
        _walk_froms(element, out)
        return
    final_froms = getattr(fromclause, "get_final_froms", None)
    if callable(final_froms):
        for child in final_froms():
            _walk_froms(child, out)


def _scoped_tables_in(stmt) -> Set[Table]:
    """返回语句直接引用的租户表。"""
    found: Set[Table] = set()
    try:
        froms = stmt.get_final_froms()
    except Exception:  # noqa: BLE001 - 部分语句类型没有 FROM
        return found
    for item in froms:
        _walk_froms(item, found)
    return {table for table in found if table.name in SCOPED_TABLES}


def _apply_loader_criteria(stmt, tenant_id: int, mappers=()):
    """为语句中涉及的每个租户实体加上 ``tenant_id`` 谓词。

    ``mappers`` 来自 ``orm_execute_state.all_mappers``——注意它挂在
    **执行状态**上而不是语句对象上（``Select`` 没有 ``all_mappers``）。
    """
    applied = False
    for mapper in mappers:
        cls = mapper.class_
        table_name = getattr(getattr(mapper, "local_table", None), "name", "")
        if table_name not in SCOPED_TABLES:
            continue
        if not hasattr(cls, "tenant_id"):
            logger.error(
                "[tenancy] mapped class %s (table %s) has no tenant_id attribute; "
                "tenant isolation is NOT applied to it",
                cls.__name__,
                table_name,
            )
            continue
        stmt = stmt.options(
            with_loader_criteria(
                cls,
                # SQLAlchemy 会把实体类作为参数传入 lambda，并把闭包变量
                # ``tenant_id`` 提取为**绑定参数**，在语句执行时求值。
                # 每次执行都新建 lambda，因此不会串味到上一个请求的归属。
                lambda cls: cls.tenant_id == tenant_id,  # noqa: B023
                include_aliases=True,
            )
        )
        applied = True

    if applied:
        return stmt

    # 回退路径：Core 语句（未携带 ORM 实体）直接引用租户表时，
    # 用显式 WHERE 施加谓词。
    for table in _scoped_tables_in(stmt):
        if "tenant_id" not in table.c:
            continue
        stmt = stmt.where(table.c.tenant_id == tenant_id)
        applied = True

    return stmt


# ---------------------------------------------------------------------------
# 事件安装
# ---------------------------------------------------------------------------

def install_scope_guard() -> None:
    """安装会话级租户守卫。幂等，可重复调用。"""
    global _installed
    with _install_lock:
        if _installed:
            return
        Session.dispatch.do_orm_execute  # noqa: B018 - 触发事件集合初始化
        event.listen(Session, "do_orm_execute", _on_do_orm_execute)
        event.listen(Session, "before_flush", _on_before_flush)
        _installed = True
        logger.info("[tenancy] scope guard installed")


def uninstall_scope_guard() -> None:
    """卸载守卫（测试使用）。"""
    global _installed
    with _install_lock:
        if not _installed:
            return
        event.remove(Session, "do_orm_execute", _on_do_orm_execute)
        event.remove(Session, "before_flush", _on_before_flush)
        _installed = False


def _on_do_orm_execute(orm_execute_state) -> None:
    """ORM 语句执行前的租户谓词注入。"""
    if not should_scope():
        return
    # 列加载 / 关系加载会继承父语句的条件，重复注入只会产生冗余 SQL。
    if orm_execute_state.is_column_load or orm_execute_state.is_relationship_load:
        return
    if not (
        orm_execute_state.is_select
        or orm_execute_state.is_update
        or orm_execute_state.is_delete
    ):
        return

    tenant_id = effective_tenant_id()
    try:
        rewritten = _apply_loader_criteria(
            orm_execute_state.statement,
            tenant_id,
            getattr(orm_execute_state, "all_mappers", None) or (),
        )
    except Exception as exc:  # noqa: BLE001 - 绝不因守卫异常放行
        logger.exception("[tenancy] failed to apply tenant criteria: %s", exc)
        raise

    if rewritten is not orm_execute_state.statement:
        orm_execute_state.statement = rewritten.execution_options(
            **{_TENANCY_MARKER: True}
        )


def _on_before_flush(session: Session, flush_context, instances) -> None:
    """为新写入的对象盖上归属戳。"""
    if not should_scope():
        return
    tenant_id = effective_tenant_id()
    for obj in session.new:
        table_name = getattr(getattr(obj, "__table__", None), "name", "")
        if table_name not in SCOPED_TABLES:
            continue
        if not hasattr(obj, "tenant_id"):
            continue
        if getattr(obj, "tenant_id", None) is None:
            setattr(obj, "tenant_id", tenant_id)


def install_raw_sql_audit(engine: Engine) -> None:
    """审计绕过 ORM 的裸 SQL。

    ORM 语句会在 :func:`_on_do_orm_execute` 中被打上标记，因此这里只关心
    「未打标记且引用了租户表」的语句。默认仅告警一次；设置
    ``DSA_TENANCY_STRICT_RAW_SQL=true`` 后直接报错。
    """

    def _before_execute(conn, clauseelement, multiparams, params, execution_options):
        if not should_scope():
            return
        if execution_options.get(_TENANCY_MARKER):
            return

        sql = str(clauseelement)
        match = _SCOPED_TABLE_RE.search(sql)
        if match is None:
            return

        table_name = match.group(1).lower()
        signature = f"{type(clauseelement).__name__}:{table_name}"
        if (os.getenv(_STRICT_RAW_SQL_ENV, "") or "").strip().lower() in _TRUTHY:
            raise TenantScopeError(
                f"raw SQL touches tenant table {table_name!r} without tenant "
                f"isolation; wrap it in bypass_tenant_scope() or use the ORM path. "
                f"SQL: {sql[:200]}"
            )
        if signature not in _raw_sql_warned:
            _raw_sql_warned.add(signature)
            logger.warning(
                "[tenancy] raw SQL touches tenant table %r without tenant "
                "isolation and is NOT filtered. SQL: %s",
                table_name,
                sql[:200],
            )

    event.listen(engine, "before_execute", _before_execute, retval=False)


@contextmanager
def engine_guard(engine: Engine) -> Iterator[None]:
    """为指定引擎安装守卫与裸 SQL 审计。"""
    install_scope_guard()
    install_raw_sql_audit(engine)
    try:
        yield
    finally:
        pass


def reset_raw_sql_warnings() -> None:
    """清空裸 SQL 告警去重集合（测试使用）。"""
    _raw_sql_warned.clear()


def scope_status() -> dict:
    """返回守卫运行状态，供 ``/capabilities`` 与诊断使用。"""
    return {
        "multiuser_enabled": multiuser_enabled(),
        "guard_installed": _installed,
        "guard_armed": _guard_armed,
        "strict_raw_sql": (os.getenv(_STRICT_RAW_SQL_ENV, "") or "").strip().lower()
        in _TRUTHY,
        "scoped_tables": sorted(SCOPED_TABLES),
        "effective_tenant_id": effective_tenant_id() if multiuser_enabled() else None,
    }


def get_optional_tenant_id() -> Optional[int]:
    """返回当前上下文归属（未启用多用户时返回 ``None``）。"""
    if not multiuser_enabled():
        return None
    return effective_tenant_id()
