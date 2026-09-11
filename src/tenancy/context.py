# -*- coding: utf-8 -*-
"""当前用户上下文（request / background scoped principal）。

为什么用 ``contextvars`` 而不是 ``threading.local``
--------------------------------------------------
FastAPI 的请求可能在同一个线程内被多个协程交替执行（``async def`` 端点、
``run_in_threadpool`` 等）。``threading.local`` 会在协程切换时串味，
``contextvars`` 跟随 asyncio 任务上下文，是唯一正确的选择。

上下文来源（``Principal.source``）
---------------------------------
- ``"session"``：浏览器会话 Cookie（管理员或普通用户登录）
- ``"bearer"`` ：服务端到服务端调用（CowAgent 等），携带 API Token
- ``"system"`` ：后台任务（调度器、CLI）显式绑定

只有 ``None`` 表示「未知来源」，这在多用户模式下属于**异常状态**：
它会被 :func:`src.tenancy.scope.resolve_scope_owner` 按 fail-closed
策略处理。
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

#: 系统属主 ID。上游单用户数据在迁移时全部归属到该用户，
#: 后台任务（调度器 / CLI）也以该身份运行。
SYSTEM_TENANT_ID: int = int(os.environ.get("DSA_SYSTEM_TENANT_ID", "1"))

#: 角色常量
ROLE_ADMIN = "admin"
ROLE_USER = "user"

#: 上下文来源常量
SOURCE_SESSION = "session"
SOURCE_BEARER = "bearer"
SOURCE_SYSTEM = "system"

_ENV_MULTIUSER = "DSA_MULTIUSER_ENABLED"
_TRUTHY = {"1", "true", "yes", "on"}

_current_principal: ContextVar[Optional["Principal"]] = ContextVar(
    "dsa_tenancy_principal", default=None
)
#: 显式绕过租户过滤的深度计数（见 :func:`bypass_tenant_scope`）。
_scope_bypass_depth: ContextVar[int] = ContextVar("dsa_tenancy_bypass_depth", default=0)


@dataclass(frozen=True)
class Principal:
    """一次请求 / 一次后台任务所代表的身份。"""

    user_id: int
    username: str = ""
    role: str = ROLE_USER
    source: str = SOURCE_SYSTEM

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def is_system(self) -> bool:
        return self.source == SOURCE_SYSTEM

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "role": self.role,
            "source": self.source,
        }


def system_principal(username: str = "system") -> Principal:
    """构造系统属主身份，供后台任务使用。"""
    return Principal(
        user_id=SYSTEM_TENANT_ID,
        username=username,
        role=ROLE_ADMIN,
        source=SOURCE_SYSTEM,
    )


def multiuser_enabled() -> bool:
    """多用户模式是否启用。

    每次调用都重新读取环境变量，这样 WebUI 修改配置后无需重启即可生效
    （与上游 ``is_auth_enabled`` 的语义不同：这里不做缓存，因为它是
    安全开关，宁可多读一次环境变量）。
    """
    return (os.environ.get(_ENV_MULTIUSER, "") or "").strip().lower() in _TRUTHY


def set_current_principal(principal: Optional[Principal]) -> Token:
    """设置当前身份，返回可用于回滚的 token。"""
    return _current_principal.set(principal)


def reset_current_principal(token: Token) -> None:
    """回滚到 :func:`set_current_principal` 之前的状态。"""
    try:
        _current_principal.reset(token)
    except ValueError:  # pragma: no cover - token 来自其它上下文
        _current_principal.set(None)


def current_principal() -> Optional[Principal]:
    """返回当前身份；``None`` 表示处于「未知来源」状态。"""
    return _current_principal.get()


def current_user_id() -> Optional[int]:
    """返回当前用户 ID；``None`` 表示未绑定。"""
    principal = current_principal()
    return principal.user_id if principal is not None else None


def current_username() -> str:
    principal = current_principal()
    return principal.username if principal is not None else ""


def current_role() -> str:
    principal = current_principal()
    return principal.role if principal is not None else ""


def is_admin_context() -> bool:
    """当前身份是否具备管理员权限。"""
    principal = current_principal()
    return principal is not None and principal.is_admin


@contextmanager
def bind_user(
    user_id: int,
    *,
    username: str = "",
    role: str = ROLE_USER,
    source: str = SOURCE_SYSTEM,
) -> Iterator[Principal]:
    """在 ``with`` 块内把当前身份绑定到指定用户。

    后台任务（尤其是调度器的子进程）必须使用本上下文管理器显式绑定身份，
    否则租户过滤无法确定归属。

    Example:
        >>> with bind_user(7, username="alice"):
        ...     db.get_analysis_history()
    """
    principal = Principal(user_id=user_id, username=username, role=role, source=source)
    token = set_current_principal(principal)
    try:
        yield principal
    finally:
        reset_current_principal(token)


@contextmanager
def bind_system() -> Iterator[Principal]:
    """在 ``with`` 块内以系统属主身份运行。"""
    principal = system_principal()
    token = set_current_principal(principal)
    try:
        yield principal
    finally:
        reset_current_principal(token)


@contextmanager
def bypass_tenant_scope(reason: str = "") -> Iterator[None]:
    """显式绕过租户过滤（迁移、跨用户统计、运维脚本）。

    这是一个**逃生舱**，调用点必须说明理由。嵌套调用是安全的。
    """
    if reason:
        logger.warning("[tenancy] tenant scope bypassed: %s", reason)
    depth = _scope_bypass_depth.get()
    token = _scope_bypass_depth.set(depth + 1)
    try:
        yield
    finally:
        _scope_bypass_depth.reset(token)


def scope_bypassed() -> bool:
    """当前是否处于绕过租户过滤的上下文内。"""
    return _scope_bypass_depth.get() > 0
