# -*- coding: utf-8 -*-
"""多租户能力的安装入口。

两个入口，职责分明：

:func:`bootstrap_tenancy`
    数据库侧引导。在 ``DatabaseManager.__init__`` 中调用，顺序是
    「建表/补列/回填 → 安装会话守卫 → 启用守卫」。**启用守卫必须在
    回填之后**，否则回填语句自身会被租户谓词限制。

:func:`install_tenancy`
    Web 侧装配。在 ``create_app`` 中调用，注册中间件与路由。

失败策略
--------
- 多用户模式**未启用**：完全跳过，不注册任何东西，也不碰数据库结构。
  这保证了「不开启就不改变上游行为」。
- 多用户模式**已启用**：引导失败直接抛出。多租户是安全边界，
  半吊子状态比不启动更危险。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_bootstrap_done = False
_bootstrap_report: Optional[Dict[str, Any]] = None


def bootstrap_tenancy(engine) -> Optional[Dict[str, Any]]:
    """引导数据库侧的多租户结构。返回引导报告（未启用时返回 ``None``）。"""
    global _bootstrap_done, _bootstrap_report

    from src.tenancy.context import multiuser_enabled

    if not multiuser_enabled():
        logger.debug("[tenancy] multiuser disabled; skipping schema bootstrap")
        return None

    if _bootstrap_done:
        return _bootstrap_report

    from src.tenancy.schema import ensure_tenancy_schema
    from src.tenancy.scope import arm_scope_guard, install_scope_guard, install_raw_sql_audit

    report = ensure_tenancy_schema(engine)
    if report.warnings:
        for warning in report.warnings:
            logger.warning("[tenancy] bootstrap warning: %s", warning)

    install_scope_guard()
    install_raw_sql_audit(engine)
    arm_scope_guard()

    _bootstrap_done = True
    _bootstrap_report = report.to_dict()
    if report.changed:
        logger.info("[tenancy] schema bootstrap applied: %s", _bootstrap_report)
    else:
        logger.info("[tenancy] schema already up to date")
    return _bootstrap_report


def install_tenancy(app) -> None:
    """把多租户中间件与路由装配到 FastAPI 应用。"""
    from src.tenancy.context import multiuser_enabled

    if not multiuser_enabled():
        logger.info("[tenancy] multiuser disabled; tenancy API not mounted")
        return

    from src.tenancy.api import router as tenancy_router
    from src.tenancy.middleware import add_tenancy_middleware
    from src.tenancy.scope import install_scope_guard

    # 保证守卫已安装（进程可能在未走 DatabaseManager 的场景下装配 app）
    install_scope_guard()

    app.include_router(tenancy_router, prefix="/api/v1/tenancy", tags=["Tenancy"])
    add_tenancy_middleware(app)
    logger.info("[tenancy] tenancy API mounted at /api/v1/tenancy")


def reset_bootstrap_state() -> None:
    """重置引导状态（测试使用）。"""
    global _bootstrap_done, _bootstrap_report
    _bootstrap_done = False
    _bootstrap_report = None


def bootstrap_report() -> Optional[Dict[str, Any]]:
    return _bootstrap_report
