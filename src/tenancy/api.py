# -*- coding: utf-8 -*-
"""多租户 REST API。

挂载在 ``/api/v1/tenancy`` 下。之所以不复用上游的 ``/api/v1/auth``：
上游那套是「单管理员 + 文件凭据」，语义不同，混在一起会让两条鉴权链路
互相污染。分开放，职责清晰，也便于日后把多用户能力独立出去。

给 CowAgent 的接入契约
----------------------
1. ``POST /api/v1/tenancy/auth/token`` 用用户名密码换一个长期 Bearer Token；
2. 后续所有调用带上 ``Authorization: Bearer <token>``；
3. 服务端据此确定 ``tenant_id``，用户只能看到/修改自己的数据。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.tenancy import service
from src.tenancy import push as push_service
from src.tenancy.context import (
    Principal,
    current_principal,
    multiuser_enabled,
)
from src.tenancy.middleware import USER_COOKIE_NAME
from src.tenancy.scope import scope_status
from src.tenancy.settings import (
    WatchlistError,
    add_user_stock,
    delete_user_settings,
    extract_inquired_stocks,
    public_settings_view,
    remove_user_stock,
    replace_user_stock_list,
    reset_user_stock_list,
    resolve_stock_list,
    save_user_settings,
    supported_keys,
)

logger = logging.getLogger(__name__)

router = APIRouter()

#: 用户会话 Cookie 的有效期（秒），与 Token 默认 TTL 对齐
_COOKIE_MAX_AGE = 30 * 24 * 3600


# ---------------------------------------------------------------------------
# 依赖
# ---------------------------------------------------------------------------

def require_principal() -> Principal:
    """要求已认证。"""
    principal = current_principal()
    if principal is None:
        raise _unauthorized()
    return principal


def require_admin() -> Principal:
    """要求管理员身份。"""
    principal = require_principal()
    if not principal.is_admin:
        raise _forbidden("需要管理员权限")
    return principal


def require_push_service() -> Principal:
    """要求「推送服务」身份：管理员，或数据库中标记为系统账号的调用者。

    这个守卫存在的原因：``POST /push/digest`` 会**按入参指定的收件人**返回
    数据，也就是说它能读到别人的自选股。因此绝不能对普通成员开放。

    判据刻意**不用角色名白名单** —— 角色是管理员可以随时改的，把服务账号
    的权限绑在角色上，等于让一次「改角色」的操作悄悄扩大了数据可见范围。
    改用 ``is_system``：它只在建号时设置，普通成员拿不到，也不会被日常
    运维动作改动。
    """
    principal = require_principal()
    if principal.is_admin:
        return principal
    try:
        user = service.get_user(principal.user_id)
    except Exception as exc:  # noqa: BLE001 - 查询失败按「无权」处理，不放行
        logger.warning("[tenancy] cannot verify push-service identity: %s", exc)
        raise _forbidden("需要推送服务权限")
    if user is not None and bool(getattr(user, "is_system", False)):
        return principal
    raise _forbidden("需要推送服务权限")


def _unauthorized() -> Exception:
    from fastapi import HTTPException

    return HTTPException(status_code=401, detail="需要登录或提供有效的 API Token")


def _forbidden(message: str = "无权访问") -> Exception:
    from fastapi import HTTPException

    return HTTPException(status_code=403, detail=message)


def _error_response(exc: service.TenancyError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "message": exc.message},
    )


def _watchlist_error(exc: WatchlistError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": "invalid_stock_code", "message": str(exc)},
    )


def _client_ip(request: Request) -> str:
    try:
        from src.auth import get_client_ip

        return get_client_ip(request)
    except Exception:  # noqa: BLE001
        return request.client.host if request.client else ""


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str = Field(..., description="用户名")
    password: str = Field(..., description="密码")
    issue_token: bool = Field(
        default=False,
        alias="issueToken",
        description="是否同时返回 Bearer Token（自动化客户端用）",
    )

    model_config = {"populate_by_name": True}


class TokenRequest(BaseModel):
    username: str
    password: str
    ttl_seconds: Optional[int] = Field(default=None, alias="ttlSeconds")

    model_config = {"populate_by_name": True}


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., alias="currentPassword")
    new_password: str = Field(..., alias="newPassword")

    model_config = {"populate_by_name": True}


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "user"
    display_name: Optional[str] = Field(default=None, alias="displayName")

    model_config = {"populate_by_name": True}


class UpdateUserRequest(BaseModel):
    display_name: Optional[str] = Field(default=None, alias="displayName")
    role: Optional[str] = None
    status: Optional[str] = None

    model_config = {"populate_by_name": True}


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(..., alias="newPassword")

    model_config = {"populate_by_name": True}


class SettingsRequest(BaseModel):
    settings: Dict[str, Any] = Field(default_factory=dict)


class ResetSettingsRequest(BaseModel):
    keys: List[str] = Field(default_factory=list)


class WatchlistItemRequest(BaseModel):
    stock_code: str = Field(..., alias="stockCode", description="股票代码，如 600519")

    model_config = {"populate_by_name": True}


class WatchlistReplaceRequest(BaseModel):
    stock_codes: List[str] = Field(
        default_factory=list, alias="stockCodes", description="完整替换为这些代码"
    )

    model_config = {"populate_by_name": True}


class WatchlistInquireRequest(BaseModel):
    wechat_id: str = Field(..., alias="wechatId", description="微信用户唯一标识")
    query: str = Field(..., description="用户咨询文本")

    model_config = {"populate_by_name": True}



class WeChatBindRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=32, description="Web 端注册用户名")
    password: str = Field(..., min_length=1, max_length=128, description="Web 端注册密码")
    wechat_id: str = Field(..., min_length=1, max_length=64, description="微信唯一标识 wxid")
    wechat_nickname: Optional[str] = Field(None, max_length=64, description="微信昵称")


class PushDigestRequest(BaseModel):
    """「按收件人取行情推送内容」的入参。

    ``wechat_id`` 与 ``tenant_id`` 二选一：CowAgent 侧天然只知道微信标识
    （调度任务的 receiver），所以 ``wechat_id`` 是主路径；``tenant_id``
    留给管理端调试。
    """

    wechat_id: Optional[str] = Field(
        default=None, alias="wechatId", description="微信唯一标识，据此反查账号"
    )
    tenant_id: Optional[int] = Field(
        default=None, alias="tenantId", description="直接指定账号 ID（管理端调试用）"
    )
    force: bool = Field(
        default=False, description="跳过到点与交易日判定，仅用于联调"
    )

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# 能力探测
# ---------------------------------------------------------------------------

@router.get("/capabilities", summary="多租户能力探测")
async def capabilities() -> Dict[str, Any]:
    """公开端点：前端据此决定是否显示登录页 / 用户管理入口。"""
    return {
        "multiuser_enabled": multiuser_enabled(),
        "supported_setting_keys": supported_keys(),
        "scope": scope_status(),
        "auth": {
            "cookie_name": USER_COOKIE_NAME,
            "bearer_header": "Authorization: Bearer <token>",
            "login_endpoint": "/api/v1/tenancy/auth/login",
            "token_endpoint": "/api/v1/tenancy/auth/token",
        },
    }


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------

@router.post("/auth/login", summary="用户登录")
async def login(payload: LoginRequest, request: Request):
    if not multiuser_enabled():
        return JSONResponse(
            status_code=400,
            content={"error": "multiuser_disabled", "message": "多用户模式未启用"},
        )

    user = service.authenticate(payload.username, payload.password)
    if user is None:
        logger.info("[tenancy] failed login for %r from %s", payload.username, _client_ip(request))
        return JSONResponse(
            status_code=401,
            content={"error": "invalid_credentials", "message": "用户名或密码错误"},
        )

    service.mark_login(user.id)
    token = service.issue_api_token(user.id, actor=None, client_ip=_client_ip(request))

    body: Dict[str, Any] = {"user": user.to_public_dict()}
    if payload.issue_token:
        body["token"] = token

    response = JSONResponse(content=body)
    response.set_cookie(
        key=USER_COOKIE_NAME,
        value=token,
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/auth/token", summary="签发 Bearer Token（供自动化客户端）")
async def create_token(payload: TokenRequest, request: Request):
    """用用户名密码换取长期 Token。CowAgent 等自动化系统使用此端点。"""
    if not multiuser_enabled():
        return JSONResponse(
            status_code=400,
            content={"error": "multiuser_disabled", "message": "多用户模式未启用"},
        )

    user = service.authenticate(payload.username, payload.password)
    if user is None:
        logger.info("[tenancy] failed token request for %r", payload.username)
        return JSONResponse(
            status_code=401,
            content={"error": "invalid_credentials", "message": "用户名或密码错误"},
        )

    try:
        token = service.issue_api_token(
            user.id, ttl_seconds=payload.ttl_seconds, client_ip=_client_ip(request)
        )
    except service.TenancyError as exc:
        return _error_response(exc)

    return {
        "token": token,
        "token_type": "Bearer",
        "user": user.to_public_dict(),
    }


@router.post("/auth/logout", summary="退出登录")
async def logout():
    response = JSONResponse(content={"ok": True})
    response.delete_cookie(key=USER_COOKIE_NAME, path="/")
    return response


@router.get("/auth/me", summary="当前用户信息")
async def me(principal: Principal = Depends(require_principal)) -> Dict[str, Any]:
    user = service.get_user(principal.user_id)
    if user is None:
        return JSONResponse(
            status_code=404,
            content={"error": "user_not_found", "message": "用户不存在"},
        )
    return {
        "user": user.to_public_dict(),
        "principal": principal.to_dict(),
    }


@router.post("/auth/change-password", summary="修改自己的密码")
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    principal: Principal = Depends(require_principal),
):
    try:
        service.change_password(
            principal.user_id,
            current_password=payload.current_password,
            new_password=payload.new_password,
            actor=principal,
            client_ip=_client_ip(request),
        )
    except service.TenancyError as exc:
        return _error_response(exc)

    # 密码变更会吊销全部 Token，包括当前这个会话，因此需要清理 Cookie。
    response = JSONResponse(
        content={"ok": True, "message": "密码已更新，请重新登录"}
    )
    response.delete_cookie(key=USER_COOKIE_NAME, path="/")
    return response


# ---------------------------------------------------------------------------
# 微信绑定与实时二维码（门禁协同）
# ---------------------------------------------------------------------------

@router.post("/auth/wechat-bind", summary="微信门禁绑定账号")
async def wechat_bind(payload: WeChatBindRequest, request: Request):
    """微信机器人门禁验证：输入账号密码，将微信 wechat_id 绑定到该用户并签发 Token。"""
    try:
        user = service.bind_wechat(
            username=payload.username,
            password=payload.password,
            wechat_id=payload.wechat_id,
            wechat_nickname=payload.wechat_nickname,
            client_ip=_client_ip(request),
        )
        token = service.issue_api_token(
            user.id, ttl_seconds=365 * 86400, client_ip=_client_ip(request)
        )
        return {
            "ok": True,
            "message": "绑定成功",
            "user": user.to_public_dict(),
            "token": token,
            "token_type": "Bearer",
        }
    except service.TenancyError as exc:
        return _error_response(exc)


@router.get("/auth/wechat-status", summary="查询微信绑定状态")
async def wechat_status(wechat_id: str = Query(..., min_length=1)):
    """查询某个 wechat_id 是否已绑定有效账号。"""
    user = service.get_user_by_wechat_id(wechat_id)
    if user is None:
        return {"bound": False, "user": None}
    return {
        "bound": True,
        "user": user.to_public_dict(),
    }


@router.post("/auth/wechat-unbind", summary="解绑微信")
async def wechat_unbind(request: Request, principal: Principal = Depends(require_principal)):
    """当前登录用户解绑自己的微信。"""
    try:
        user = service.unbind_wechat(principal.user_id, client_ip=_client_ip(request))
        return {"ok": True, "message": "解绑成功", "user": user.to_public_dict()}
    except service.TenancyError as exc:
        return _error_response(exc)


@router.get("/wechat/qrlogin", summary="获取微信二维码（代理 CowAgent）")
async def get_wechat_qr(
    req: Request,
    force: bool = False,
):
    """获取当前 CowAgent 的实时微信二维码。未绑定微信的用户自动获取新二维码。"""
    import httpx
    from src.tenancy.middleware import resolve_request_principal

    principal = resolve_request_principal(req)
    should_force = force
    if principal is not None:
        user = service.get_user(principal.user_id)
        if user and not getattr(user, "wechat_id", None):
            should_force = True

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            if should_force:
                resp = await client.post(
                    "http://cowagent:9899/api/weixin/qrlogin",
                    json={"action": "refresh", "force": 1},
                )
            else:
                resp = await client.get("http://cowagent:9899/api/weixin/qrlogin")
            return JSONResponse(status_code=resp.status_code, content=resp.json())
    except Exception as exc:
        logger.error("[tenancy] failed to proxy qrlogin from cowagent: %s", exc)
        return JSONResponse(
            status_code=502,
            content={"status": "error", "message": f"无法连接到微信机器人通道: {exc}"},
        )


@router.post("/wechat/qrlogin", summary="轮询微信二维码状态（代理 CowAgent）")
async def poll_wechat_qr(request: Request):
    """轮询微信二维码的扫描与确认状态。"""
    import httpx
    try:
        body = await request.json()
    except Exception:
        body = {"action": "poll"}
    if body.get("action") == "refresh" and not body.get("force"):
        from src.tenancy.middleware import resolve_request_principal
        principal = resolve_request_principal(request)
        if principal is not None:
            user = service.get_user(principal.user_id)
            if user and not getattr(user, "wechat_id", None):
                body["force"] = 1
    try:
        async with httpx.AsyncClient(timeout=40.0) as client:
            resp = await client.post("http://cowagent:9899/api/weixin/qrlogin", json=body)
            return JSONResponse(status_code=resp.status_code, content=resp.json())
    except Exception as exc:
        logger.error("[tenancy] failed to proxy qr poll from cowagent: %s", exc)
        return JSONResponse(
            status_code=502,
            content={"status": "error", "message": f"轮询微信通道异常: {exc}"},
        )


# ---------------------------------------------------------------------------
# 用户管理（管理员）
# ---------------------------------------------------------------------------

@router.get("/users", summary="用户列表")
async def list_users(
    include_disabled: bool = True,
    _: Principal = Depends(require_admin),
) -> Dict[str, Any]:
    users = service.list_users(include_disabled=include_disabled)
    return {"users": [user.to_public_dict() for user in users], "total": len(users)}


@router.post("/users", summary="创建用户")
async def create_user(
    payload: CreateUserRequest,
    request: Request,
    principal: Principal = Depends(require_admin),
):
    try:
        user = service.create_user(
            username=payload.username,
            password=payload.password,
            role=payload.role,
            display_name=payload.display_name,
            actor=principal,
            client_ip=_client_ip(request),
        )
    except service.TenancyError as exc:
        return _error_response(exc)
    return JSONResponse(status_code=201, content={"user": user.to_public_dict()})


@router.patch("/users/{user_id}", summary="更新用户")
async def update_user(
    user_id: int,
    payload: UpdateUserRequest,
    request: Request,
    principal: Principal = Depends(require_admin),
):
    try:
        user = service.update_user(
            user_id,
            display_name=payload.display_name,
            role=payload.role,
            status=payload.status,
            actor=principal,
            client_ip=_client_ip(request),
        )
    except service.TenancyError as exc:
        return _error_response(exc)
    return {"user": user.to_public_dict()}


@router.delete("/users/{user_id}", summary="删除用户")
async def delete_user(
    user_id: int,
    request: Request,
    principal: Principal = Depends(require_admin),
):
    try:
        service.delete_user(user_id, actor=principal, client_ip=_client_ip(request))
    except service.TenancyError as exc:
        return _error_response(exc)
    return {"ok": True}


@router.post("/users/{user_id}/reset-password", summary="管理员重置用户密码")
async def reset_password(
    user_id: int,
    payload: ResetPasswordRequest,
    request: Request,
    principal: Principal = Depends(require_admin),
):
    try:
        service.reset_password(
            user_id,
            new_password=payload.new_password,
            actor=principal,
            client_ip=_client_ip(request),
        )
    except service.TenancyError as exc:
        return _error_response(exc)
    return {"ok": True, "message": "密码已重置，该用户的所有 Token 已失效"}


@router.post("/users/{user_id}/token", summary="为用户签发 Token")
async def issue_user_token(
    user_id: int,
    request: Request,
    ttl_seconds: Optional[int] = None,
    principal: Principal = Depends(require_admin),
):
    try:
        token = service.issue_api_token(
            user_id,
            ttl_seconds=ttl_seconds,
            actor=principal,
            client_ip=_client_ip(request),
        )
    except service.TenancyError as exc:
        return _error_response(exc)
    return {"token": token, "token_type": "Bearer", "user_id": int(user_id)}


@router.post("/users/{user_id}/revoke-tokens", summary="吊销用户全部 Token")
async def revoke_user_tokens(
    user_id: int,
    request: Request,
    principal: Principal = Depends(require_admin),
):
    try:
        service.revoke_api_tokens(user_id, actor=principal, client_ip=_client_ip(request))
    except service.TenancyError as exc:
        return _error_response(exc)
    return {"ok": True, "message": "该用户的所有 Token 已失效"}


# ---------------------------------------------------------------------------
# 用户设置
# ---------------------------------------------------------------------------

@router.get("/settings", summary="读取自己的配置")
async def get_settings(principal: Principal = Depends(require_principal)) -> Dict[str, Any]:
    return {
        "user_id": principal.user_id,
        "settings": public_settings_view(principal.user_id),
    }


@router.put("/settings", summary="更新自己的配置")
async def put_settings(
    payload: SettingsRequest,
    request: Request,
    principal: Principal = Depends(require_principal),
):
    written = save_user_settings(principal.user_id, payload.settings)
    if not written:
        return JSONResponse(
            status_code=400,
            content={
                "error": "no_valid_keys",
                "message": "没有可写入的配置项，请检查键名是否在支持列表中",
                "supported": supported_keys(),
            },
        )
    logger.info(
        "[tenancy] user %s updated settings: %s",
        principal.username,
        ",".join(sorted(written)),
    )
    return {
        "ok": True,
        "written": sorted(written),
        "settings": public_settings_view(principal.user_id),
    }


@router.delete("/settings", summary="重置部分配置（回落到全局 .env）")
async def reset_settings(
    payload: ResetSettingsRequest,
    principal: Principal = Depends(require_principal),
):
    deleted = delete_user_settings(principal.user_id, payload.keys)
    return {
        "ok": True,
        "deleted": deleted,
        "settings": public_settings_view(principal.user_id),
    }


# ---------------------------------------------------------------------------
# 自选股（按用户）
#
# 外部系统（MCP / CowAgent）请走这组接口，**不要**用上游的
# ``/api/v1/stocks/watchlist/*`` —— 那条路径写的是全局 STOCK_LIST，会串号。
# ---------------------------------------------------------------------------

@router.get("/watchlist", summary="读取自己的自选股")
async def get_watchlist(principal: Principal = Depends(require_principal)) -> Dict[str, Any]:
    return {"user_id": principal.user_id, **resolve_stock_list(principal.user_id)}


@router.post("/watchlist", summary="加入自选")
async def add_watchlist_item(
    payload: WatchlistItemRequest,
    principal: Principal = Depends(require_principal),
):
    try:
        result = add_user_stock(principal.user_id, payload.stock_code)
    except WatchlistError as exc:
        return _watchlist_error(exc)
    logger.info("[tenancy] user %s 加入自选 %s", principal.username, result["added"])
    return {"ok": True, "user_id": principal.user_id, **result}


@router.put("/watchlist", summary="整体替换自选")
async def replace_watchlist(
    payload: WatchlistReplaceRequest,
    principal: Principal = Depends(require_principal),
):
    try:
        result = replace_user_stock_list(principal.user_id, payload.stock_codes)
    except WatchlistError as exc:
        return _watchlist_error(exc)
    return {"ok": True, "user_id": principal.user_id, **result}


@router.delete("/watchlist", summary="清空个人自选（回落到全局）")
async def reset_watchlist(principal: Principal = Depends(require_principal)) -> Dict[str, Any]:
    return {
        "ok": True,
        "user_id": principal.user_id,
        **reset_user_stock_list(principal.user_id),
    }


@router.delete("/watchlist/{stock_code}", summary="从自选移除")
async def delete_watchlist_item(
    stock_code: str,
    principal: Principal = Depends(require_principal),
):
    try:
        result = remove_user_stock(principal.user_id, stock_code)
    except WatchlistError as exc:
        return _watchlist_error(exc)
    logger.info("[tenancy] user %s 移除自选 %s", principal.username, result["removed"])
    return {"ok": True, "user_id": principal.user_id, **result}


@router.post("/watchlist/inquire", summary="处理微信用户咨询股票并自动加入自选")
async def inquire_watchlist(payload: WatchlistInquireRequest) -> Dict[str, Any]:
    wx_id = (payload.wechat_id or "").strip()
    bound = service.get_user_by_wechat_id(wx_id)
    if bound is None:
        return {
            "ok": False,
            "reason": "unbound_recipient",
            "message": "该微信尚未绑定账号",
            "stocks": [],
        }

    stocks = extract_inquired_stocks(payload.query)
    if not stocks:
        return {
            "ok": True,
            "user_id": bound.id,
            "username": bound.username,
            "stocks": [],
            "action": "none",
        }

    added_list = []
    stock_labels = []
    for code, name in stocks:
        try:
            res = add_user_stock(bound.id, code)
            newly_added = not res.get("already_present", False)
            added_list.append({
                "code": code,
                "name": name,
                "newly_added": newly_added,
            })
            label = f"{name}({code})" if name else code
            stock_labels.append(label)
            logger.info("[tenancy] 用户 %s 咨询股票自动加自选: %s (%s)", bound.username, code, name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[tenancy] failed to auto-add %s for user %s: %s", code, bound.id, exc)

    notice = ""
    if stock_labels:
        stocks_str = "】、【".join(stock_labels)
        notice = f"📌 已默认将【{stocks_str}】添加到您的自选股，交易日 09:35/15:30 将为您定时推送行情。"

    return {
        "ok": True,
        "user_id": bound.id,
        "username": bound.username,
        "stocks": added_list,
        "notice": notice,
    }



# ---------------------------------------------------------------------------
# 行情推送（大盘 + 自选股）
#
# 给 CowAgent 的定时任务用。调用方（调度任务）只知道「收件人」，也就是微信
# 标识，不知道 tenant_id，所以反查放在这里。
#
# 时间表（PUSH_ENABLED / PUSH_TIMES）存在各用户自己的 dsa_user_settings 里，
# 于是「取消推送」和「改推送时间」都只改一处 —— CowAgent 侧的任务不需要重建，
# 也就没有跨服务同步。任务只需要定期问一句「现在该推吗」。
#
# ⚠️ 这是**能读到他人数据**的接口（按入参指定收件人），所以守卫用
# require_push_service 而不是 require_principal。普通成员改自己的推送偏好
# 走的是 /settings，不经过这里。
# ---------------------------------------------------------------------------

@router.post("/push/digest", summary="组装某收件人的行情推送（推送服务专用）")
async def build_push_digest(
    payload: PushDigestRequest,
    _: Principal = Depends(require_push_service),
) -> Any:
    """返回 ``skip=True`` 是**正常**结果（一天里绝大多数轮询都如此）。

    调用方应原样转达 ``reason``，不要改写成「推送失败」—— ``not_due`` 与
    ``already_sent`` 都表示链路健康。
    """
    target_id: Optional[int] = payload.tenant_id

    if target_id is None:
        wx_id = (payload.wechat_id or "").strip()
        if not wx_id:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error": "missing_target",
                    "message": "必须提供 wechat_id 或 tenant_id 之一",
                },
            )
        bound = service.get_user_by_wechat_id(wx_id)
        if bound is None:
            # 尚未绑定账号的微信收不到推送。这是「还没建立会话」，不是故障。
            return {
                "ok": True,
                "skip": True,
                "reason": "unbound_recipient",
                "wechat_id": wx_id,
                "message": "该微信尚未绑定账号，跳过",
            }
        target_id = bound.id
        username = bound.username
    else:
        target = service.get_user(int(target_id))
        if target is None:
            return JSONResponse(
                status_code=404,
                content={"ok": False, "error": "user_not_found", "message": "账号不存在"},
            )
        username = target.username

    result = push_service.build_user_digest(int(target_id), force=payload.force)
    return {"username": username, **result}


# ---------------------------------------------------------------------------
# 用量与运维
# ---------------------------------------------------------------------------

@router.get("/usage", summary="自己的 LLM 用量")
async def my_usage(
    limit_days: int = 30,
    principal: Principal = Depends(require_principal),
) -> Dict[str, Any]:
    return service.usage_summary(principal.user_id, limit_days=limit_days)


@router.get("/usage/{user_id}", summary="指定用户的 LLM 用量（管理员）")
async def user_usage(
    user_id: int,
    limit_days: int = 30,
    _: Principal = Depends(require_admin),
) -> Dict[str, Any]:
    return service.usage_summary(user_id, limit_days=limit_days)


@router.get("/scheduler/users", summary="参与定时分析的用户（管理员）")
async def scheduler_users(_: Principal = Depends(require_admin)) -> Dict[str, Any]:
    return {"users": service.list_schedulable_users()}


@router.get("/audit", summary="租户审计日志（管理员）")
async def audit_log(
    user_id: Optional[int] = None,
    limit: int = 100,
    _: Principal = Depends(require_admin),
) -> Dict[str, Any]:
    return {"entries": service.list_audit_log(user_id, limit=limit)}


@router.get("/health", summary="多租户子系统健康检查")
async def tenancy_health() -> Dict[str, Any]:
    return {
        "ok": True,
        "multiuser_enabled": multiuser_enabled(),
        "user_count": service.count_users() if multiuser_enabled() else 0,
        "scope": scope_status(),
    }
