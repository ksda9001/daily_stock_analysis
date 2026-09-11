# -*- coding: utf-8 -*-
"""按用户解析配置项。

上游的配置模型是「进程级全局」：``src/config.py`` 从 ``.env`` 读一次，
所有逻辑共享同一个 ``Config`` 实例。多用户要求每个用户有自己的自选股、
通知渠道与调度时间。

实现方式：**声明式映射 + 配置副本覆盖**
--------------------------------------
1. :data:`USER_SETTING_SPECS` 声明「哪些环境变量可以被用户覆盖」以及
   它对应的 ``Config`` 属性名与类型。
2. :func:`apply_user_overrides` 复制一份 ``Config``，把该用户的设置写上去，
   返回副本。

这样做的好处是：上游所有读取 ``config.<attr>`` 的代码（通知发送、
报告渲染、Agent 工具……）**完全不需要修改**，就能自动获得按用户生效的
配置。改动面从「上百处读取点」收敛到「一个复制点」。

安全约定
--------
:data:`USER_SETTING_SPECS` 中**不允许**出现 LLM 供应商密钥、数据库路径、
认证开关等基础设施配置。用户可覆盖的范围仅限「业务偏好 + 推送目标」。
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from src.tenancy.models import TenantUserSetting

logger = logging.getLogger(__name__)

#: 类型标签
KIND_STR = "str"
KIND_BOOL = "bool"
KIND_CSV = "csv"      # 逗号分隔字符串 → List[str]
KIND_CODES = "codes"  # 股票代码列表 → List[str]（大写、去重）
KIND_TIMES = "times"  # "HH:MM,HH:MM" → List[str]


@dataclass(frozen=True)
class SettingSpec:
    """一个可被用户覆盖的配置项。"""

    env_key: str
    config_attr: str
    kind: str = KIND_STR
    label: str = ""
    #: 是否属于敏感信息（对外返回时做掩码）
    secret: bool = False


#: 用户可覆盖的配置项白名单。
#: 顺序即 API 返回顺序，便于前端渲染。
USER_SETTING_SPECS: tuple = (
    # ---- 自选股与报告 ----
    SettingSpec("STOCK_LIST", "stock_list", KIND_CODES, "自选股列表"),
    SettingSpec("REPORT_TYPE", "report_type", KIND_STR, "报告类型"),
    SettingSpec("AGENT_MODE", "agent_mode", KIND_BOOL, "Agent 模式"),
    # ---- 调度 ----
    SettingSpec("SCHEDULE_ENABLED", "schedule_enabled", KIND_BOOL, "启用定时分析"),
    SettingSpec("SCHEDULE_TIMES", "schedule_times", KIND_TIMES, "定时执行时间"),
    # ---- 通知：企业微信 / 钉钉 / 飞书 ----
    SettingSpec("WECHAT_WEBHOOK_URL", "wechat_webhook_url", KIND_STR, "企业微信机器人", secret=True),
    SettingSpec("DINGTALK_WEBHOOK_URL", "dingtalk_webhook_url", KIND_STR, "钉钉机器人", secret=True),
    SettingSpec("FEISHU_WEBHOOK_URL", "feishu_webhook_url", KIND_STR, "飞书机器人", secret=True),
    SettingSpec("FEISHU_WEBHOOK_SECRET", "feishu_webhook_secret", KIND_STR, "飞书签名密钥", secret=True),
    SettingSpec("FEISHU_WEBHOOK_KEYWORD", "feishu_webhook_keyword", KIND_STR, "飞书关键词"),
    # ---- 通知：Telegram ----
    SettingSpec("TELEGRAM_BOT_TOKEN", "telegram_bot_token", KIND_STR, "Telegram Bot Token", secret=True),
    SettingSpec("TELEGRAM_CHAT_ID", "telegram_chat_id", KIND_STR, "Telegram Chat ID"),
    # ---- 通知：邮件 ----
    SettingSpec("EMAIL_SENDER", "email_sender", KIND_STR, "发件人邮箱"),
    SettingSpec("EMAIL_PASSWORD", "email_password", KIND_STR, "邮箱授权码", secret=True),
    SettingSpec("EMAIL_RECEIVERS", "email_receivers", KIND_CSV, "收件人列表"),
    # ---- 通知：其它 ----
    SettingSpec("SERVERCHAN3_SENDKEY", "serverchan3_sendkey", KIND_STR, "Server酱3 SendKey", secret=True),
    SettingSpec("PUSHPLUS_TOKEN", "pushplus_token", KIND_STR, "PushPlus Token", secret=True),
    SettingSpec("PUSHOVER_USER_KEY", "pushover_user_key", KIND_STR, "Pushover User Key", secret=True),
    SettingSpec("PUSHOVER_API_TOKEN", "pushover_api_token", KIND_STR, "Pushover API Token", secret=True),
    SettingSpec("NTFY_URL", "ntfy_url", KIND_STR, "ntfy 地址"),
    SettingSpec("GOTIFY_URL", "gotify_url", KIND_STR, "Gotify 地址"),
    SettingSpec("GOTIFY_TOKEN", "gotify_token", KIND_STR, "Gotify Token", secret=True),
    SettingSpec("DISCORD_WEBHOOK_URL", "discord_webhook_url", KIND_STR, "Discord Webhook", secret=True),
    SettingSpec("SLACK_WEBHOOK_URL", "slack_webhook_url", KIND_STR, "Slack Webhook", secret=True),
    SettingSpec("CUSTOM_WEBHOOK_URLS", "custom_webhook_urls", KIND_CSV, "自定义 Webhook", secret=True),
    SettingSpec("ASTRBOT_URL", "astrbot_url", KIND_STR, "AstrBot 地址"),
)

_SPECS_BY_KEY: Dict[str, SettingSpec] = {spec.env_key: spec for spec in USER_SETTING_SPECS}

#: 显式禁止用户覆盖的基础设施配置（防御性：即使被误写入 DB 也不会生效）
_FORBIDDEN_KEYS = frozenset({
    "DATABASE_PATH",
    "ADMIN_AUTH_ENABLED",
    "DSA_MULTIUSER_ENABLED",
    "LLM_CHANNELS",
    "LITELLM_CONFIG",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "QWEN_API_KEY",
    "DSA_API_TOKEN_TTL_SECONDS",
})


def supported_keys() -> List[str]:
    """返回允许用户覆盖的配置键。"""
    return [spec.env_key for spec in USER_SETTING_SPECS]


def get_spec(key: str) -> Optional[SettingSpec]:
    return _SPECS_BY_KEY.get((key or "").strip().upper())


def is_supported_key(key: str) -> bool:
    normalized = (key or "").strip().upper()
    return normalized in _SPECS_BY_KEY and normalized not in _FORBIDDEN_KEYS


# ---------------------------------------------------------------------------
# 值编解码
# ---------------------------------------------------------------------------

def _split_csv(value: str) -> List[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def encode_value(spec: SettingSpec, value: Any) -> Optional[str]:
    """把 API 传入的值编码为数据库中的字符串。"""
    if value is None:
        return None
    if spec.kind == KIND_BOOL:
        if isinstance(value, bool):
            return "true" if value else "false"
        return "true" if str(value).strip().lower() in {"1", "true", "yes", "on"} else "false"
    if spec.kind in (KIND_CSV, KIND_CODES, KIND_TIMES):
        if isinstance(value, (list, tuple)):
            items = [str(item).strip() for item in value if str(item).strip()]
        else:
            items = _split_csv(str(value))
        if spec.kind == KIND_CODES:
            seen, normalized = set(), []
            for item in items:
                code = item.upper()
                if code not in seen:
                    seen.add(code)
                    normalized.append(code)
            items = normalized
        return ",".join(items)
    return str(value)


def decode_value(spec: SettingSpec, raw: Optional[str]) -> Any:
    """把数据库字符串解码为 ``Config`` 期望的类型。"""
    if raw is None:
        return None
    if spec.kind == KIND_BOOL:
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    if spec.kind in (KIND_CSV, KIND_CODES):
        return _split_csv(raw)
    if spec.kind == KIND_TIMES:
        return _split_csv(raw)
    text = str(raw)
    return text or None


def mask_value(spec: SettingSpec, raw: Optional[str]) -> Optional[str]:
    """敏感项对外输出时做掩码。"""
    if raw is None:
        return None
    if not spec.secret:
        return raw
    text = str(raw)
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}{'*' * 6}{text[-4:]}"


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------

def load_user_settings(tenant_id: int) -> Dict[str, str]:
    """读取某用户的全部设置（原始字符串）。失败时返回空字典。"""
    if tenant_id is None:
        return {}
    try:
        from src.storage import get_db
    except Exception as exc:  # pragma: no cover - 导入期异常
        logger.warning("[tenancy] cannot import storage for settings: %s", exc)
        return {}

    try:
        with get_db().session_scope() as session:
            rows = (
                session.query(TenantUserSetting)
                .filter(TenantUserSetting.tenant_id == int(tenant_id))
                .all()
            )
            return {row.key: row.value for row in rows}
    except Exception as exc:  # noqa: BLE001 - 配置读取失败不应中断分析
        logger.warning("[tenancy] failed to load settings for user %s: %s", tenant_id, exc)
        return {}


def save_user_settings(
    tenant_id: int,
    updates: Dict[str, Any],
    *,
    session=None,
) -> Dict[str, str]:
    """写入某用户的设置。返回实际写入的键值对。

    会忽略 :data:`_FORBIDDEN_KEYS` 与不在白名单中的键。
    """
    accepted: Dict[str, str] = {}
    for raw_key, raw_value in (updates or {}).items():
        key = (raw_key or "").strip().upper()
        if key in _FORBIDDEN_KEYS:
            logger.warning("[tenancy] rejected attempt to set protected key %r", key)
            continue
        spec = get_spec(key)
        if spec is None:
            logger.debug("[tenancy] ignoring unsupported setting key %r", key)
            continue
        accepted[key] = encode_value(spec, raw_value)

    if not accepted:
        return {}

    from src.storage import get_db

    def _write(active_session) -> None:
        for key, value in accepted.items():
            row = (
                active_session.query(TenantUserSetting)
                .filter(
                    TenantUserSetting.tenant_id == int(tenant_id),
                    TenantUserSetting.key == key,
                )
                .one_or_none()
            )
            if row is None:
                active_session.add(
                    TenantUserSetting(tenant_id=int(tenant_id), key=key, value=value)
                )
            else:
                row.value = value

    if session is not None:
        _write(session)
    else:
        with get_db().session_scope() as own_session:
            _write(own_session)
    return accepted


def delete_user_settings(tenant_id: int, keys: Sequence[str]) -> int:
    """删除指定键，使其回落到全局 ``.env`` 值。返回删除行数。"""
    normalized = [(key or "").strip().upper() for key in keys]
    normalized = [key for key in normalized if key]
    if not normalized:
        return 0
    from src.storage import get_db

    with get_db().session_scope() as session:
        deleted = (
            session.query(TenantUserSetting)
            .filter(
                TenantUserSetting.tenant_id == int(tenant_id),
                TenantUserSetting.key.in_(normalized),
            )
            .delete(synchronize_session=False)
        )
        return int(deleted or 0)


def resolve_setting(key: str, tenant_id: Optional[int], default: Any = None) -> Any:
    """按「用户设置 → 进程环境变量 → 默认值」顺序解析单个配置项。"""
    import os

    spec = get_spec(key)
    if spec is None:
        return default

    if tenant_id is not None:
        raw = load_user_settings(tenant_id).get(spec.env_key)
        if raw not in (None, ""):
            decoded = decode_value(spec, raw)
            if decoded not in (None, "", []):
                return decoded

    env_raw = os.getenv(spec.env_key)
    if env_raw not in (None, ""):
        return decode_value(spec, env_raw)
    return default


# ---------------------------------------------------------------------------
# 应用到 Config
# ---------------------------------------------------------------------------

def apply_user_overrides(config, tenant_id: Optional[int]):
    """返回一个应用了该用户设置的 ``Config`` **副本**。

    - ``tenant_id`` 为 ``None`` 或多用户未启用时，原样返回 ``config``；
    - 用户未设置的项保持全局 ``.env`` 的值；
    - 副本是浅拷贝：列表类属性会被替换为新列表，不影响原对象。
    """
    from src.tenancy.context import multiuser_enabled

    if tenant_id is None or not multiuser_enabled():
        return config

    raw_settings = load_user_settings(tenant_id)
    if not raw_settings:
        return config

    overridden = False
    try:
        effective = copy.copy(config)
    except Exception as exc:  # pragma: no cover - Config 不可复制时的兜底
        logger.warning("[tenancy] cannot copy Config for user %s: %s", tenant_id, exc)
        return config

    for key, raw_value in raw_settings.items():
        spec = get_spec(key)
        if spec is None or key in _FORBIDDEN_KEYS:
            continue
        if raw_value in (None, ""):
            continue
        try:
            decoded = decode_value(spec, raw_value)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[tenancy] invalid value for %s (user %s): %s", key, tenant_id, exc
            )
            continue
        if decoded in (None, "", []):
            continue
        if not hasattr(effective, spec.config_attr):
            logger.debug(
                "[tenancy] Config has no attribute %r; skipping %s",
                spec.config_attr,
                key,
            )
            continue
        try:
            setattr(effective, spec.config_attr, decoded)
            overridden = True
        except Exception as exc:  # noqa: BLE001 - frozen dataclass 等
            logger.warning(
                "[tenancy] cannot set %s on Config for user %s: %s",
                spec.config_attr,
                tenant_id,
                exc,
            )

    if overridden:
        logger.debug("[tenancy] applied per-user config overrides for user %s", tenant_id)
    return effective


def effective_stock_list(tenant_id: Optional[int]) -> Optional[List[str]]:
    """返回该用户的自选股列表；未配置时返回 ``None`` 以表示「沿用全局」。"""
    from src.tenancy.context import multiuser_enabled

    if tenant_id is None or not multiuser_enabled():
        return None
    raw = load_user_settings(tenant_id).get("STOCK_LIST")
    if not raw:
        return None
    spec = get_spec("STOCK_LIST")
    decoded = decode_value(spec, raw)
    return decoded or None


def public_settings_view(tenant_id: int) -> Dict[str, Any]:
    """构造给前端 / API 的配置视图（敏感项掩码）。"""
    raw = load_user_settings(tenant_id)
    view: Dict[str, Any] = {}
    for spec in USER_SETTING_SPECS:
        value = raw.get(spec.env_key)
        view[spec.env_key] = {
            "label": spec.label,
            "kind": spec.kind,
            "secret": spec.secret,
            "configured": bool(value),
            "value": mask_value(spec, value),
        }
    return view
