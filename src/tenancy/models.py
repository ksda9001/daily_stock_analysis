# -*- coding: utf-8 -*-
"""多租户 ORM 模型。

这三张表使用与上游业务表相同的 ``Base``（``src.storage.Base``），
因此会随 ``Base.metadata.create_all()`` 一起建表，也能被同一套
Alembic-free 迁移流程管理。

表设计说明
----------
``dsa_users``
    用户主表。``id=1`` 保留为**系统属主**（``is_system=True``），
    上游单用户数据在迁移时全部归属到它。

``dsa_user_settings``
    键值型按用户配置。之所以不用「每用户一行宽表」：
    上游配置项有 50+ 个且仍在增加，键值表可以零迁移地支持新增配置项。

``dsa_tenancy_audit``
    租户操作审计。记录用户增删改、密码变更、Token 签发等敏感动作。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from src.storage import Base

logger = logging.getLogger(__name__)


class TenantUser(Base):
    """租户用户。"""

    __tablename__ = "dsa_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(32), nullable=False, unique=True, index=True)
    display_name = Column(String(64), nullable=True)
    password_hash = Column(String(256), nullable=False)
    #: 'admin' | 'user'
    role = Column(String(16), nullable=False, default="user", index=True)
    #: 'active' | 'disabled'
    status = Column(String(16), nullable=False, default="active", index=True)

    #: 系统属主标记。该系统属主不可删除、不可停用。
    is_system = Column(Boolean, nullable=False, default=False)

    #: 递增即可吊销该用户已签发的全部 Bearer Token。
    token_version = Column(Integer, nullable=False, default=1)

    created_at = Column(DateTime, default=datetime.now, nullable=False, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)
    last_login_at = Column(DateTime, nullable=True)

    #: 绑定的微信 ID 与微信昵称
    wechat_id = Column(String(64), nullable=True, unique=True, index=True)
    wechat_nickname = Column(String(64), nullable=True)
    wechat_bound_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_dsa_users_status_role", "status", "role"),
    )

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def wechat_bound(self) -> bool:
        return bool(self.wechat_id)

    def to_public_dict(self) -> Dict[str, Any]:
        """对外暴露的用户信息（绝不包含 ``password_hash``）。"""
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name or self.username,
            "role": self.role,
            "status": self.status,
            "is_system": bool(self.is_system),
            "wechat_id": self.wechat_id,
            "wechat_nickname": self.wechat_nickname,
            "wechat_bound": bool(self.wechat_id),
            "wechat_bound_at": self.wechat_bound_at.isoformat() if self.wechat_bound_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
        }


class TenantUserSetting(Base):
    """按用户键值配置。"""

    __tablename__ = "dsa_user_settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(
        Integer,
        ForeignKey("dsa_users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    key = Column(String(64), nullable=False)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "key", name="uq_dsa_user_settings_tenant_key"),
    )


class TenantAuditLog(Base):
    """租户操作审计日志。"""

    __tablename__ = "dsa_tenancy_audit"

    id = Column(Integer, primary_key=True, autoincrement=True)
    #: 被操作的用户（目标）
    tenant_id = Column(Integer, nullable=True, index=True)
    #: 执行者
    actor_id = Column(Integer, nullable=True, index=True)
    actor_username = Column(String(32), nullable=True)
    action = Column(String(48), nullable=False, index=True)
    detail = Column(Text, nullable=True)
    client_ip = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False, index=True)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "actor_id": self.actor_id,
            "actor_username": self.actor_username,
            "action": self.action,
            "detail": self.detail,
            "client_ip": self.client_ip,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


def record_audit(
    session,
    *,
    action: str,
    tenant_id: Optional[int] = None,
    actor_id: Optional[int] = None,
    actor_username: Optional[str] = None,
    detail: Optional[str] = None,
    client_ip: Optional[str] = None,
) -> None:
    """写入一条审计记录（不提交事务，由调用方决定）。"""
    session.add(
        TenantAuditLog(
            action=action,
            tenant_id=tenant_id,
            actor_id=actor_id,
            actor_username=actor_username,
            detail=detail,
            client_ip=client_ip,
        )
    )
