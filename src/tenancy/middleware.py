# -*- coding: utf-8 -*-
"""租户上下文中间件。

职责
----
1. **解析身份**：从 ``Authorization: Bearer <token>`` 或用户会话 Cookie
   中解析出 :class:`Principal`，并绑定到 contextvar。
2. **强制认证**：多用户模式下，未认证的 ``/api/v1/*`` 请求返回 401。

与上游 ``AuthMiddleware`` 的关系
-------------------------------
上游中间件负责「单管理员模式」。本中间件在多用户模式启用时**接管**
``/api/v1/*`` 的鉴权，此时上游中间件会主动让行（见
``api/middlewares/auth.py`` 中的提前返回），避免两套规则互相打架。

管理员兼容
----------
若 ``ADMIN_AUTH_ENABLED=true`` 且请求携带有效的管理员会话 Cookie，
则把身份绑定为**系统属主**（``user_id=1``，管理员角色）。
这样上游的管理员登录流程零改动即可继续使用。
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from src.tenancy.context import (
    Principal,
    SOURCE_SESSION,
    multiuser_enabled,
    reset_current_principal,
    set_current_principal,
    system_principal,
)
from src.tenancy.tokens import extract_bearer_token

logger = logging.getLogger(__name__)

#: 用户会话 Cookie 名。刻意与上游 ``dsa_session`` 区分，避免语义混淆。
USER_COOKIE_NAME = "dsa_user_token"

#: 无需认证即可访问的路径
EXEMPT_PATHS = frozenset({
    "/api/health",
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/api/v1/health",
    "/api/v1/tenancy/capabilities",
    "/api/v1/tenancy/auth/login",
    "/api/v1/tenancy/auth/token",
    "/api/v1/tenancy/auth/wechat-bind",
    "/api/v1/tenancy/auth/wechat-status",
    "/api/v1/tenancy/watchlist/inquire",
    # 上游管理员登录流程必须可达，否则管理员无法进入系统属主身份
    "/api/v1/auth/login",
    "/api/v1/auth/status",
})

#: 即使未认证也放行的路径前缀（前端静态资源 / 公开只读）
EXEMPT_PREFIXES = (
    "/assets/",
    "/static/",
)


def _is_exempt(path: str) -> bool:
    normalized = path.rstrip("/") or "/"
    if normalized in EXEMPT_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in EXEMPT_PREFIXES)


def resolve_request_principal(request: Request) -> Optional[Principal]:
    """从请求中解析身份。解析不出返回 ``None``。

    优先级：Bearer Token > 用户会话 Cookie > 管理员会话 Cookie。
    Bearer 优先是因为它显式表达了「以某个用户身份调用」的意图。
    """
    from src.tenancy import service

    bearer = extract_bearer_token(request.headers.get("Authorization"))
    if bearer:
        principal = service.resolve_principal_from_token(bearer)
        if principal is not None:
            return principal
        # Token 无效时不再回退到 Cookie：携带了 Token 就说明调用方
        # 明确要求以 Token 身份访问，静默降级会掩盖配置错误。
        return None

    cookie_token = request.cookies.get(USER_COOKIE_NAME)
    if cookie_token:
        principal = service.resolve_principal_from_token(cookie_token)
        if principal is not None:
            return Principal(
                user_id=principal.user_id,
                username=principal.username,
                role=principal.role,
                source=SOURCE_SESSION,
            )

    # 上游管理员会话 → 系统属主
    try:
        from src.auth import COOKIE_NAME as ADMIN_COOKIE_NAME
        from src.auth import is_auth_enabled, verify_session

        if is_auth_enabled():
            admin_cookie = request.cookies.get(ADMIN_COOKIE_NAME)
            if admin_cookie and verify_session(admin_cookie):
                return system_principal(username="admin")
    except Exception as exc:  # noqa: BLE001 - 兼容性路径，失败不影响主流程
        logger.debug("[tenancy] admin session probe failed: %s", exc)

    return None


class TenancyContextMiddleware(BaseHTTPMiddleware):
    """解析并绑定租户身份；多用户模式下强制认证。"""

    async def dispatch(self, request: Request, call_next: Callable):
        if not multiuser_enabled():
            # 单用户模式：完全不干预，保持与上游一致的行为。
            return await call_next(request)

        path = request.url.path
        principal = resolve_request_principal(request)

        if principal is None and not _is_exempt(path) and path.startswith("/api/v1/"):
            return JSONResponse(
                status_code=401,
                content={
                    "error": "unauthorized",
                    "message": "需要登录或提供有效的 API Token",
                },
            )

        token = set_current_principal(principal)
        try:
            return await call_next(request)
        finally:
            reset_current_principal(token)


def add_tenancy_middleware(app) -> None:
    """注册中间件。

    必须在上游 ``add_auth_middleware`` **之后**调用，这样本中间件位于
    Starlette 中间件栈的更外层，从而先于上游中间件解析身份。
    """
    app.add_middleware(TenancyContextMiddleware)
