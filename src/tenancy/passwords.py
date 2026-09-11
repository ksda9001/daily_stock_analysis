# -*- coding: utf-8 -*-
"""密码哈希与校验。

复用上游 ``src.auth`` 的 PBKDF2-SHA256 参数（10 万次迭代、
``salt_b64:hash_b64`` 编码），保证：

1. 管理员密码与用户密码使用同一套强度参数；
2. 已有 ``.admin_password_hash`` 文件可以用同一解析器读取。

这里只做**纯函数**，不依赖模块级缓存，便于在子进程与测试中直接使用。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
from typing import Optional, Tuple

from src.auth import PBKDF2_ITERATIONS

logger = logging.getLogger(__name__)

#: 用户名 / 密码长度约束
MIN_USERNAME_LEN = 3
MAX_USERNAME_LEN = 32
MIN_PASSWORD_LEN = 6
MAX_PASSWORD_LEN = 128

#: 用户名允许的字符集（字母、数字、下划线、连字符、点）
_USERNAME_ALLOWED = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "_-."
)


def validate_username(username: str) -> Optional[str]:
    """校验用户名，返回错误信息或 ``None``。"""
    value = (username or "").strip()
    if not value:
        return "用户名不能为空"
    if len(value) < MIN_USERNAME_LEN:
        return f"用户名至少 {MIN_USERNAME_LEN} 个字符"
    if len(value) > MAX_USERNAME_LEN:
        return f"用户名最多 {MAX_USERNAME_LEN} 个字符"
    invalid = sorted({ch for ch in value if ch not in _USERNAME_ALLOWED})
    if invalid:
        return f"用户名包含非法字符: {''.join(invalid)}"
    return None


def validate_password(password: str) -> Optional[str]:
    """校验密码强度，返回错误信息或 ``None``。"""
    if not password or not password.strip():
        return "密码不能为空"
    if len(password) < MIN_PASSWORD_LEN:
        return f"密码至少 {MIN_PASSWORD_LEN} 位"
    if len(password) > MAX_PASSWORD_LEN:
        return f"密码最多 {MAX_PASSWORD_LEN} 位"
    return None


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """生成 ``salt_b64:hash_b64`` 形式的密码摘要。"""
    salt = secrets.token_bytes(32)
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt=salt,
        iterations=iterations,
    )
    salt_b64 = base64.standard_b64encode(salt).decode("ascii")
    hash_b64 = base64.standard_b64encode(derived).decode("ascii")
    return f"{salt_b64}:{hash_b64}"


def parse_password_hash(value: str) -> Optional[Tuple[bytes, bytes]]:
    """解析 ``salt_b64:hash_b64``，返回 ``(salt, hash)`` 或 ``None``。"""
    if not value or ":" not in value:
        return None
    parts = value.strip().split(":", 1)
    if len(parts) != 2:
        return None
    try:
        salt = base64.standard_b64decode(parts[0].strip())
        stored = base64.standard_b64decode(parts[1].strip())
    except (ValueError, TypeError):
        return None
    if salt and stored:
        return (salt, stored)
    return None


def verify_password_hash(
    password: str,
    encoded: str,
    *,
    iterations: int = PBKDF2_ITERATIONS,
) -> bool:
    """常量时间校验密码。任何解析失败都返回 ``False``。"""
    parsed = parse_password_hash(encoded)
    if parsed is None:
        return False
    salt, stored = parsed
    try:
        computed = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt=salt,
            iterations=iterations,
        )
    except (ValueError, TypeError):  # pragma: no cover - 防御性分支
        return False
    return hmac.compare_digest(computed, stored)
