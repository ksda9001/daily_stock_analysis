# -*- coding: utf-8 -*-
"""服务端到服务端 Bearer Token。

用途
----
CowAgent（或任意自动化系统）需要以**某个具体用户**的身份调用 DSA 的
REST API。会话 Cookie 不适合这种场景（无浏览器、需要长期有效），
因此这里提供一个无状态、可吊销的签名 Token：

    v1.<user_id>.<token_version>.<expires_at>.<nonce>.<signature>

- 签名密钥独立存放于 ``DATA_DIR/.api_token_secret``，与浏览器会话密钥
  （``.session_secret``）分离，可以单独轮换。
- ``token_version`` 来自 ``dsa_users.token_version``，递增即可吊销该用户的
  全部已签发 Token，无需维护黑名单。
- Token **不携带角色**：角色在每次请求时从数据库读取，避免「签发后降权」
  仍然有效的越权问题。

安全边界
--------
- Token 是**全权凭证**，等同于该用户的密码。必须通过 HTTPS 传输。
- 默认有效期 30 天，可用 ``DSA_API_TOKEN_TTL_SECONDS`` 调整。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "v1"
DEFAULT_TTL_SECONDS = 30 * 24 * 3600
SECRET_FILENAME = ".api_token_secret"
SECRET_BYTES = 32

_secret_cache: Optional[bytes] = None


def _get_data_dir() -> Path:
    """与 ``src.auth`` 保持一致：DATABASE_PATH 所在目录。"""
    db_path = os.getenv("DATABASE_PATH", "./data/stock_analysis.db")
    return Path(db_path).resolve().parent


def _get_secret_path() -> Path:
    return _get_data_dir() / SECRET_FILENAME


def reset_token_secret_cache() -> None:
    """清除内存中的密钥缓存（测试 / 轮换后调用）。"""
    global _secret_cache
    _secret_cache = None


def rotate_token_secret() -> bool:
    """轮换 Token 签名密钥，使所有已签发 Token 立即失效。"""
    global _secret_cache
    data_dir = _get_data_dir()
    secret_path = _get_secret_path()
    new_secret = secrets.token_bytes(SECRET_BYTES)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = secret_path.with_suffix(".tmp")
        tmp_path.write_bytes(new_secret)
        try:
            tmp_path.chmod(0o600)
        except OSError:  # pragma: no cover - Windows 上无意义
            pass
        tmp_path.replace(secret_path)
        _secret_cache = new_secret
        logger.info("[tenancy] API token secret rotated")
        return True
    except OSError as exc:
        logger.error("[tenancy] failed to rotate %s: %s", SECRET_FILENAME, exc)
        return False


def load_token_secret() -> Optional[bytes]:
    """读取或创建签名密钥。失败返回 ``None``（调用方须拒绝签发/校验）。"""
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache

    data_dir = _get_data_dir()
    secret_path = _get_secret_path()
    try:
        if secret_path.exists():
            secret = secret_path.read_bytes()
            if len(secret) != SECRET_BYTES:
                logger.warning(
                    "[tenancy] invalid %s length (%d), regenerating",
                    SECRET_FILENAME,
                    len(secret),
                )
                if rotate_token_secret():
                    return _secret_cache
                return None
            _secret_cache = secret
            return _secret_cache

        data_dir.mkdir(parents=True, exist_ok=True)
        new_secret = secrets.token_bytes(SECRET_BYTES)
        try:
            with open(secret_path, "xb") as handle:
                handle.write(new_secret)
            try:
                secret_path.chmod(0o600)
            except OSError:  # pragma: no cover
                pass
        except FileExistsError:
            _secret_cache = secret_path.read_bytes()
        else:
            _secret_cache = new_secret
        return _secret_cache
    except OSError as exc:
        logger.error("[tenancy] failed to read/create %s: %s", SECRET_FILENAME, exc)
        return None


def _ttl_seconds() -> int:
    raw = os.getenv("DSA_API_TOKEN_TTL_SECONDS", "")
    if not raw:
        return DEFAULT_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "[tenancy] invalid DSA_API_TOKEN_TTL_SECONDS=%r; using %d",
            raw,
            DEFAULT_TTL_SECONDS,
        )
        return DEFAULT_TTL_SECONDS
    # 下限 5 分钟，上限 365 天，避免误配置导致永久凭证
    return max(300, min(value, 365 * 24 * 3600))


@dataclass(frozen=True)
class TokenClaims:
    """已验证的 Token 声明。"""

    user_id: int
    token_version: int
    expires_at: int

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at


def _sign(secret: bytes, payload: str) -> str:
    return hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def issue_token(user_id: int, token_version: int, *, ttl_seconds: Optional[int] = None) -> str:
    """为用户签发一个 Bearer Token。签发失败返回空字符串。"""
    secret = load_token_secret()
    if not secret:
        logger.error("[tenancy] cannot issue token: signing secret unavailable")
        return ""

    expires_at = int(time.time()) + (ttl_seconds if ttl_seconds is not None else _ttl_seconds())
    nonce = base64.urlsafe_b64encode(secrets.token_bytes(12)).decode("ascii").rstrip("=")
    payload = f"{TOKEN_PREFIX}.{int(user_id)}.{int(token_version)}.{expires_at}.{nonce}"
    return f"{payload}.{_sign(secret, payload)}"


def verify_token(token: str) -> Optional[TokenClaims]:
    """校验 Token 签名与有效期。失败返回 ``None``。

    只做**密码学**校验；用户是否存在、是否被禁用、``token_version`` 是否
    匹配，由 :mod:`src.tenancy.service` 在数据库层面二次确认。
    """
    if not token:
        return None
    secret = load_token_secret()
    if not secret:
        return None

    parts = token.strip().split(".")
    if len(parts) != 6:
        return None
    prefix, user_id_raw, version_raw, expires_raw, nonce, signature = parts
    if prefix != TOKEN_PREFIX:
        return None

    payload = f"{prefix}.{user_id_raw}.{version_raw}.{expires_raw}.{nonce}"
    if not hmac.compare_digest(signature, _sign(secret, payload)):
        return None

    try:
        user_id = int(user_id_raw)
        token_version = int(version_raw)
        expires_at = int(expires_raw)
    except ValueError:
        return None

    claims = TokenClaims(user_id=user_id, token_version=token_version, expires_at=expires_at)
    if claims.is_expired:
        return None
    return claims


def extract_bearer_token(authorization_header: Optional[str]) -> Optional[str]:
    """从 ``Authorization`` 头中提取 Bearer Token。"""
    if not authorization_header:
        return None
    value = authorization_header.strip()
    if not value:
        return None
    scheme, _, rest = value.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = rest.strip()
    return token or None
