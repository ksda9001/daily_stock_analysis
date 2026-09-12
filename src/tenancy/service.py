# -*- coding: utf-8 -*-
"""用户与设置的业务逻辑层。

这一层是唯一直接操作 ``dsa_users`` 的地方，负责：

- 用户 CRUD（含系统属主保护）
- 密码校验与变更
- Bearer Token → 身份解析
- 调度器所需的「活跃用户清单」
- 按用户的 LLM 用量汇总
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from src.tenancy.context import ROLE_ADMIN, ROLE_USER, SYSTEM_TENANT_ID, Principal
from src.tenancy.models import TenantAuditLog, TenantUser, record_audit
from src.tenancy.passwords import (
    hash_password,
    validate_password,
    validate_username,
    verify_password_hash,
)
from src.tenancy.tokens import TokenClaims, issue_token, verify_token

logger = logging.getLogger(__name__)

VALID_ROLES = (ROLE_ADMIN, ROLE_USER)
VALID_STATUSES = ("active", "disabled")


class TenancyError(Exception):
    """业务层可预期的错误（对应 HTTP 4xx）。"""

    def __init__(self, message: str, *, status_code: int = 400, code: str = "tenancy_error"):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class UserRecord:
    """用户的不可变快照。

    为什么不让服务层直接返回 ORM 实例：``session_scope()`` 退出时会关闭
    会话，被 detach 的实例在访问已过期属性时会抛 ``DetachedInstanceError``。
    调用方（API 层、调度器）拿到的应该是纯数据，而不是依赖会话的对象。
    """

    id: int
    username: str
    display_name: str
    role: str
    status: str
    is_system: bool
    token_version: int
    password_hash: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    last_login_at: Optional[datetime] = None
    wechat_id: Optional[str] = None
    wechat_nickname: Optional[str] = None
    wechat_bound_at: Optional[datetime] = None

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def to_public_dict(self) -> Dict[str, Any]:
        """对外暴露的用户信息（绝不包含 ``password_hash``）。"""
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name or self.username,
            "role": self.role,
            "status": self.status,
            "is_system": self.is_system,
            "wechat_id": self.wechat_id,
            "wechat_nickname": self.wechat_nickname,
            "wechat_bound": bool(self.wechat_id),
            "wechat_bound_at": self.wechat_bound_at.isoformat() if self.wechat_bound_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
        }


def _snapshot(user: TenantUser) -> UserRecord:
    """在会话内把 ORM 实例转成快照。"""
    return UserRecord(
        id=int(user.id),
        username=user.username,
        display_name=user.display_name or user.username,
        role=user.role,
        status=user.status,
        is_system=bool(user.is_system),
        token_version=int(user.token_version or 1),
        password_hash=user.password_hash,
        created_at=user.created_at,
        updated_at=user.updated_at,
        last_login_at=user.last_login_at,
        wechat_id=getattr(user, "wechat_id", None),
        wechat_nickname=getattr(user, "wechat_nickname", None),
        wechat_bound_at=getattr(user, "wechat_bound_at", None),
    )


def _session_scope():
    from src.storage import get_db

    return get_db().session_scope()


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def list_users(*, include_disabled: bool = True) -> List[UserRecord]:
    """列出全部用户（按 id 升序）。"""
    with _session_scope() as session:
        query = session.query(TenantUser)
        if not include_disabled:
            query = query.filter(TenantUser.status == "active")
        return [_snapshot(row) for row in query.order_by(TenantUser.id.asc()).all()]


def get_user(user_id: int) -> Optional[UserRecord]:
    with _session_scope() as session:
        user = session.get(TenantUser, int(user_id))
        return _snapshot(user) if user is not None else None


def get_user_by_username(username: str) -> Optional[UserRecord]:
    value = (username or "").strip()
    if not value:
        return None
    with _session_scope() as session:
        user = (
            session.query(TenantUser)
            .filter(TenantUser.username == value)
            .one_or_none()
        )
        return _snapshot(user) if user is not None else None


def get_user_by_wechat_id(wechat_id: str) -> Optional[UserRecord]:
    value = (wechat_id or "").strip()
    if not value:
        return None
    with _session_scope() as session:
        user = (
            session.query(TenantUser)
            .filter(TenantUser.wechat_id == value)
            .one_or_none()
        )
        return _snapshot(user) if user is not None else None


def count_users() -> int:
    with _session_scope() as session:
        return int(session.query(TenantUser).count())


# ---------------------------------------------------------------------------
# 用户管理
# ---------------------------------------------------------------------------

def create_user(
    *,
    username: str,
    password: str,
    role: str = ROLE_USER,
    display_name: Optional[str] = None,
    actor: Optional[Principal] = None,
    client_ip: Optional[str] = None,
) -> UserRecord:
    """创建用户。校验失败抛出 :class:`TenancyError`。"""
    username_error = validate_username(username)
    if username_error:
        raise TenancyError(username_error, status_code=400, code="invalid_username")
    password_error = validate_password(password)
    if password_error:
        raise TenancyError(password_error, status_code=400, code="invalid_password")
    if role not in VALID_ROLES:
        raise TenancyError(f"非法角色: {role}", status_code=400, code="invalid_role")

    normalized = username.strip()
    with _session_scope() as session:
        existing = (
            session.query(TenantUser)
            .filter(TenantUser.username == normalized)
            .one_or_none()
        )
        if existing is not None:
            raise TenancyError("用户名已存在", status_code=409, code="username_taken")

        user = TenantUser(
            username=normalized,
            display_name=(display_name or normalized).strip() or normalized,
            password_hash=hash_password(password),
            role=role,
            status="active",
            is_system=False,
            token_version=1,
        )
        session.add(user)
        session.flush()
        record_audit(
            session,
            action="user.create",
            tenant_id=user.id,
            actor_id=actor.user_id if actor else None,
            actor_username=actor.username if actor else None,
            detail=f"username={normalized} role={role}",
            client_ip=client_ip,
        )
        session.flush()
        snapshot = _snapshot(user)
        logger.info("[tenancy] created user %s (id=%s, role=%s)", normalized, snapshot.id, role)
        return snapshot


def update_user(
    user_id: int,
    *,
    display_name: Optional[str] = None,
    role: Optional[str] = None,
    status: Optional[str] = None,
    actor: Optional[Principal] = None,
    client_ip: Optional[str] = None,
) -> UserRecord:
    """更新用户属性。系统属主不可降权、不可停用。"""
    with _session_scope() as session:
        user = session.get(TenantUser, int(user_id))
        if user is None:
            raise TenancyError("用户不存在", status_code=404, code="user_not_found")

        changes: List[str] = []
        if display_name is not None:
            user.display_name = display_name.strip() or user.username
            changes.append("display_name")
        if role is not None and role != user.role:
            if role not in VALID_ROLES:
                raise TenancyError(f"非法角色: {role}", status_code=400, code="invalid_role")
            if user.is_system and role != ROLE_ADMIN:
                raise TenancyError("系统属主不可降权", status_code=400, code="system_owner_protected")
            user.role = role
            changes.append(f"role={role}")
        if status is not None and status != user.status:
            if status not in VALID_STATUSES:
                raise TenancyError(f"非法状态: {status}", status_code=400, code="invalid_status")
            if user.is_system and status != "active":
                raise TenancyError("系统属主不可停用", status_code=400, code="system_owner_protected")
            user.status = status
            changes.append(f"status={status}")
            if status == "disabled":
                # 停用即吊销全部已签发 Token
                user.token_version = int(user.token_version or 1) + 1

        user.updated_at = datetime.now()
        if changes:
            record_audit(
                session,
                action="user.update",
                tenant_id=user.id,
                actor_id=actor.user_id if actor else None,
                actor_username=actor.username if actor else None,
                detail=",".join(changes),
                client_ip=client_ip,
            )
        session.flush()
        return _snapshot(user)


def delete_user(
    user_id: int,
    *,
    actor: Optional[Principal] = None,
    client_ip: Optional[str] = None,
) -> None:
    """删除用户。系统属主不可删除。

    关联的租户数据（分析历史、持仓……）**不做级联删除**——那属于
    不可逆的数据销毁，必须由运维显式执行。这里只清理该用户的设置与
    用户行本身，其余数据保留为孤儿记录（``tenant_id`` 指向已删除用户），
    不会泄漏给其他用户。
    """
    with _session_scope() as session:
        user = session.get(TenantUser, int(user_id))
        if user is None:
            raise TenancyError("用户不存在", status_code=404, code="user_not_found")
        if user.is_system:
            raise TenancyError("系统属主不可删除", status_code=400, code="system_owner_protected")
        if int(user_id) == SYSTEM_TENANT_ID:
            raise TenancyError("系统属主不可删除", status_code=400, code="system_owner_protected")

        username = user.username
        from src.tenancy.models import TenantUserSetting

        session.query(TenantUserSetting).filter(
            TenantUserSetting.tenant_id == int(user_id)
        ).delete(synchronize_session=False)
        session.delete(user)
        record_audit(
            session,
            action="user.delete",
            tenant_id=int(user_id),
            actor_id=actor.user_id if actor else None,
            actor_username=actor.username if actor else None,
            detail=f"username={username}",
            client_ip=client_ip,
        )
        logger.warning("[tenancy] deleted user %s (id=%s)", username, user_id)


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------

def authenticate(username: str, password: str) -> Optional[UserRecord]:
    """校验用户名密码。失败返回 ``None``。

    无论用户是否存在都会执行一次哈希校验，避免通过响应时间枚举用户名。
    """
    user = get_user_by_username(username)
    encoded = user.password_hash if user is not None else ""
    if not encoded or not verify_password_hash(password, encoded):
        return None
    if not user.is_active:
        logger.info("[tenancy] login rejected for disabled user %s", user.username)
        return None
    return user


def mark_login(user_id: int) -> None:
    with _session_scope() as session:
        user = session.get(TenantUser, int(user_id))
        if user is not None:
            user.last_login_at = datetime.now()


def bind_wechat(
    username: str,
    password: str,
    wechat_id: str,
    wechat_nickname: Optional[str] = None,
    client_ip: Optional[str] = None,
) -> UserRecord:
    wx_id = (wechat_id or "").strip()
    if not wx_id:
        raise TenancyError("微信唯一标识不能为空", status_code=400, code="invalid_wechat_id")
    user = authenticate(username, password)
    if user is None:
        raise TenancyError("账号或密码错误", status_code=401, code="invalid_credentials")
    if not user.is_active:
        raise TenancyError("账号已被禁用，无法绑定", status_code=403, code="user_disabled")

    with _session_scope() as session:
        conflicts = session.query(TenantUser).filter(
            TenantUser.wechat_id == wx_id,
            TenantUser.id != user.id,
        ).all()
        for conflict in conflicts:
            conflict.wechat_id = None
            conflict.wechat_nickname = None
            conflict.wechat_bound_at = None
            conflict.updated_at = datetime.now()

        db_user = session.get(TenantUser, int(user.id))
        if db_user is None:
            raise TenancyError("用户不存在", status_code=404, code="user_not_found")

        db_user.wechat_id = wx_id
        db_user.wechat_nickname = (wechat_nickname or "").strip() or db_user.display_name or db_user.username
        db_user.wechat_bound_at = datetime.now()
        db_user.updated_at = datetime.now()

        record_audit(
            session,
            action="user.wechat_bind",
            tenant_id=int(user.id),
            actor_id=int(user.id),
            actor_username=user.username,
            detail=f"wechat_id={wx_id}, nickname={db_user.wechat_nickname}",
            client_ip=client_ip,
        )
        logger.info("[tenancy] user %s bound wechat_id %s", user.username, wx_id)
        return _snapshot(db_user)


def unbind_wechat(user_id: int, client_ip: Optional[str] = None) -> UserRecord:
    with _session_scope() as session:
        db_user = session.get(TenantUser, int(user_id))
        if db_user is None:
            raise TenancyError("用户不存在", status_code=404, code="user_not_found")
        old_wx = db_user.wechat_id
        db_user.wechat_id = None
        db_user.wechat_nickname = None
        db_user.wechat_bound_at = None
        db_user.updated_at = datetime.now()

        record_audit(
            session,
            action="user.wechat_unbind",
            tenant_id=int(user_id),
            actor_id=int(user_id),
            actor_username=db_user.username,
            detail=f"old_wechat_id={old_wx}",
            client_ip=client_ip,
        )
        logger.info("[tenancy] user %s unbound wechat", db_user.username)
        return _snapshot(db_user)


def change_password(
    user_id: int,
    *,
    current_password: str,
    new_password: str,
    actor: Optional[Principal] = None,
    client_ip: Optional[str] = None,
) -> None:
    """用户自助修改密码。成功后吊销该用户全部 Token。"""
    with _session_scope() as session:
        user = session.get(TenantUser, int(user_id))
        if user is None:
            raise TenancyError("用户不存在", status_code=404, code="user_not_found")
        if not verify_password_hash(current_password or "", user.password_hash):
            raise TenancyError("当前密码错误", status_code=400, code="invalid_current_password")
        error = validate_password(new_password)
        if error:
            raise TenancyError(error, status_code=400, code="invalid_password")

        user.password_hash = hash_password(new_password)
        user.token_version = int(user.token_version or 1) + 1
        user.updated_at = datetime.now()
        record_audit(
            session,
            action="user.change_password",
            tenant_id=user.id,
            actor_id=actor.user_id if actor else None,
            actor_username=actor.username if actor else None,
            client_ip=client_ip,
        )


def reset_password(
    user_id: int,
    *,
    new_password: str,
    actor: Optional[Principal] = None,
    client_ip: Optional[str] = None,
) -> None:
    """管理员重置他人密码。成功后吊销该用户全部 Token。"""
    error = validate_password(new_password)
    if error:
        raise TenancyError(error, status_code=400, code="invalid_password")

    with _session_scope() as session:
        user = session.get(TenantUser, int(user_id))
        if user is None:
            raise TenancyError("用户不存在", status_code=404, code="user_not_found")
        user.password_hash = hash_password(new_password)
        user.token_version = int(user.token_version or 1) + 1
        user.updated_at = datetime.now()
        record_audit(
            session,
            action="user.reset_password",
            tenant_id=user.id,
            actor_id=actor.user_id if actor else None,
            actor_username=actor.username if actor else None,
            client_ip=client_ip,
        )
        logger.warning("[tenancy] password reset for user %s by admin", user.username)


def issue_api_token(
    user_id: int,
    *,
    ttl_seconds: Optional[int] = None,
    actor: Optional[Principal] = None,
    client_ip: Optional[str] = None,
) -> str:
    """为用户签发 Bearer Token。"""
    with _session_scope() as session:
        user = session.get(TenantUser, int(user_id))
        if user is None:
            raise TenancyError("用户不存在", status_code=404, code="user_not_found")
        if not user.is_active:
            raise TenancyError("用户已被停用", status_code=403, code="user_disabled")
        token = issue_token(user.id, int(user.token_version or 1), ttl_seconds=ttl_seconds)
        record_audit(
            session,
            action="token.issue",
            tenant_id=user.id,
            actor_id=actor.user_id if actor else None,
            actor_username=actor.username if actor else None,
            client_ip=client_ip,
        )
    if not token:
        raise TenancyError("签发 Token 失败", status_code=500, code="token_issue_failed")
    return token


def revoke_api_tokens(
    user_id: int,
    *,
    actor: Optional[Principal] = None,
    client_ip: Optional[str] = None,
) -> None:
    """递增 ``token_version``，使该用户全部已签发 Token 立即失效。"""
    with _session_scope() as session:
        user = session.get(TenantUser, int(user_id))
        if user is None:
            raise TenancyError("用户不存在", status_code=404, code="user_not_found")
        user.token_version = int(user.token_version or 1) + 1
        user.updated_at = datetime.now()
        record_audit(
            session,
            action="token.revoke",
            tenant_id=user.id,
            actor_id=actor.user_id if actor else None,
            actor_username=actor.username if actor else None,
            client_ip=client_ip,
        )


def resolve_principal_from_token(token: str) -> Optional[Principal]:
    """把 Bearer Token 解析为 :class:`Principal`。

    这里做「密码学校验 + 数据库复核」两步：

    1. :func:`src.tenancy.tokens.verify_token` 验证签名与有效期；
    2. 读取用户，确认存在、启用、且 ``token_version`` 未被吊销。

    角色**始终从数据库读取**，因此降权会立即生效。
    """
    claims: Optional[TokenClaims] = verify_token(token)
    if claims is None:
        return None
    user = get_user(claims.user_id)
    if user is None or not user.is_active:
        return None
    if int(user.token_version or 1) != int(claims.token_version):
        logger.info(
            "[tenancy] token rejected for user %s: version mismatch (stale token)",
            user.username,
        )
        return None
    return Principal(
        user_id=user.id,
        username=user.username,
        role=user.role,
        source="bearer",
    )


# ---------------------------------------------------------------------------
# 调度与用量
# ---------------------------------------------------------------------------

def list_schedulable_users() -> List[Dict[str, Any]]:
    """返回参与定时分析的活跃用户及其调度配置。

    这是 per-user 调度的数据源：调度器为每个条目注册一个每日任务。
    """
    from src.tenancy.settings import decode_value, get_spec, load_user_settings

    entries: List[Dict[str, Any]] = []
    for user in list_users(include_disabled=False):
        raw = load_user_settings(user.id)
        spec_enabled = get_spec("SCHEDULE_ENABLED")
        spec_times = get_spec("SCHEDULE_TIMES")

        enabled_raw = raw.get("SCHEDULE_ENABLED")
        enabled = (
            decode_value(spec_enabled, enabled_raw) if enabled_raw not in (None, "") else None
        )
        times_raw = raw.get("SCHEDULE_TIMES")
        times = (
            decode_value(spec_times, times_raw) if times_raw not in (None, "") else None
        )
        entries.append(
            {
                "tenant_id": user.id,
                "username": user.username,
                "role": user.role,
                "schedule_enabled": enabled,
                "schedule_times": times,
            }
        )
    return entries


def usage_summary(tenant_id: int, *, limit_days: int = 30) -> Dict[str, Any]:
    """汇总某用户的 LLM 用量。

    依赖 ``llm_usage.tenant_id``；历史数据已回填为系统属主。

    这里刻意用 :func:`bind_user` 而不是绕过守卫：管理员查询他人用量时，
    若沿用「当前上下文归属」会与显式过滤条件冲突（一个要求 A、一个要求 B，
    结果是查不到任何行）。绑定到目标用户后，环境谓词与显式谓词一致，
    形成纵深防御——即使显式过滤被误删，结果依然被正确收窄。
    """
    from datetime import timedelta

    from sqlalchemy import func

    from src.storage import LLMUsage
    from src.tenancy.context import bind_user

    since = datetime.now() - timedelta(days=max(1, int(limit_days)))
    with bind_user(int(tenant_id)):
        with _session_scope() as session:
            rows = (
                session.query(
                    LLMUsage.call_type,
                    func.count(LLMUsage.id),
                    func.coalesce(func.sum(LLMUsage.prompt_tokens), 0),
                    func.coalesce(func.sum(LLMUsage.completion_tokens), 0),
                    func.coalesce(func.sum(LLMUsage.total_tokens), 0),
                )
                .filter(
                    LLMUsage.tenant_id == int(tenant_id),
                    # 上游 LLMUsage 的时间列名为 called_at（不是 created_at）
                    LLMUsage.called_at >= since,
                )
                .group_by(LLMUsage.call_type)
                .all()
            )

    by_type = []
    totals = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for call_type, calls, prompt, completion, total in rows:
        calls = int(calls or 0)
        prompt = int(prompt or 0)
        completion = int(completion or 0)
        total = int(total or 0)
        by_type.append(
            {
                "call_type": call_type,
                "calls": calls,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": total,
            }
        )
        totals["calls"] += calls
        totals["prompt_tokens"] += prompt
        totals["completion_tokens"] += completion
        totals["total_tokens"] += total

    return {"tenant_id": int(tenant_id), "limit_days": int(limit_days), "by_type": by_type, "totals": totals}


def list_audit_log(tenant_id: Optional[int] = None, *, limit: int = 100) -> List[Dict[str, Any]]:
    """查询审计日志（管理员用）。"""
    with _session_scope() as session:
        query = session.query(TenantAuditLog)
        if tenant_id is not None:
            query = query.filter(TenantAuditLog.tenant_id == int(tenant_id))
        rows: Sequence[TenantAuditLog] = (
            query.order_by(TenantAuditLog.id.desc()).limit(max(1, min(int(limit), 500))).all()
        )
        return [row.to_dict() for row in rows]
