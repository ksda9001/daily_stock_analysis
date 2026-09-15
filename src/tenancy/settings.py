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
import re
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
    #: 用户是否只能读。**仅供前端置灰展示**，真正的写拦截来自
    #: :data:`USER_SCOPED_CONFIG_KEYS` —— 不要把鉴权建在这个布尔值上。
    readonly: bool = False


#: 用户可覆盖的配置项白名单。
#: 顺序即 API 返回顺序，便于前端渲染。
USER_SETTING_SPECS: tuple = (
    # ---- 问股 / Agent 偏好 ----
    SettingSpec(
        "AGENT_CONTEXT_COMPRESSION_ENABLED",
        "agent_context_compression_enabled",
        KIND_BOOL,
        "上下文压缩",
    ),
    # ---- 自选股与报告 ----
    SettingSpec("STOCK_LIST", "stock_list", KIND_CODES, "自选股列表"),
    SettingSpec("REPORT_TYPE", "report_type", KIND_STR, "报告类型"),
    SettingSpec("AGENT_MODE", "agent_mode", KIND_BOOL, "Agent 模式"),
    # ---- 行情推送（大盘 + 自选股）----
    # 这两个键刻意**不**放进 _FORBIDDEN_KEYS：它们是纯业务偏好，且用户必须
    # 能自己关掉推送、改推送时刻。注意不要和 SCHEDULE_* 混为一谈 ——
    # SCHEDULE_* 控制的是「完整分析」的调度，属于平台行为，继续禁止用户改。
    SettingSpec("PUSH_ENABLED", "push_enabled", KIND_BOOL, "行情推送"),
    # 自选股推送时刻：用户可以自由改（在 USER_SCOPED_CONFIG_KEYS 里）。
    SettingSpec("STOCK_PUSH_TIMES", "stock_push_times", KIND_TIMES, "自选股推送时间"),
    # 历史键：拆分前用户改的就是它，:func:`effective_push_times` 仍把它作为
    # 自选股时刻的回退来源。保留 spec 是为了让它可读可写 —— 从白名单里摘掉
    # 会造出「用户能写、但没有任何读取点会生效」的哑设置（见
    # USER_SCOPED_CONFIG_KEYS 上方的警告）。
    SettingSpec("PUSH_TIMES", "push_times", KIND_TIMES, "推送时间（旧）"),
    # 大盘复盘时刻：**用户只读**。列在这里是为了让 /settings 的视图把它一并
    # 返回，前端才能渲染「大盘 15:30（不可修改）」这行说明。
    #
    # 只读不是靠这个 spec 实现的，而是靠它**不在** USER_SCOPED_CONFIG_KEYS
    # 里：system_config 的写接口只认那个白名单，所以对 MARKET_PUSH_TIMES 的
    # 写请求会被拒，读请求照常（视图遍历的是 USER_SETTING_SPECS）。
    # readonly 标记供前端置灰，不参与任何鉴权判定。
    SettingSpec(
        "MARKET_PUSH_TIMES",
        "market_push_times",
        KIND_TIMES,
        "大盘复盘推送时间",
        readonly=True,
    ),
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
    "SCHEDULE_ENABLED",
    "SCHEDULE_TIMES",
    "SCHEDULE_TIME",
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


#: 「用户级系统配置」：普通成员（非管理员）可以经 ``/api/v1/system/config``
#: 读写的键。
#:
#: 写操作**不会**落到 ``.env``，而是写进该用户自己的 ``dsa_user_settings``，
#: 因此不会波及其他用户 —— 这也是它敢对普通成员开放的原因。
#:
#: ⚠️ 这里的键必须同时出现在 :data:`USER_SETTING_SPECS` 中，否则会出现
#: 「用户能写、但没有任何读取点会生效」的哑设置。
USER_SCOPED_CONFIG_KEYS = frozenset({
    "AGENT_CONTEXT_COMPRESSION_ENABLED",
    # 推送开关与时刻：必须让普通成员能自己改 —— 「取消推送」和「改推送时间」
    # 是用户级需求，且改动只落该用户自己的 dsa_user_settings，不碰 .env。
    "PUSH_ENABLED",
    "PUSH_TIMES",  # 历史键：保留可写，避免旧客户端写入时被拒。
    "STOCK_PUSH_TIMES",
    # ⚠️ 刻意不含 MARKET_PUSH_TIMES：大盘时刻是平台策略（全租户共享一份
    # 复盘），用户不可改。写请求会在这里被拒，读请求不受影响。
})


def is_user_scoped_key(key: str) -> bool:
    """该键是否属于「普通成员可自行调整」的用户级配置。"""
    normalized = (key or "").strip().upper()
    return normalized in USER_SCOPED_CONFIG_KEYS and is_supported_key(normalized)


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


def effective_config_value(key: str, tenant_id: Optional[int]) -> tuple:
    """返回 ``(生效值, 来源)``，来源为 ``"user"`` / ``"global"`` / ``"unsupported"``。

    解析顺序与 :func:`apply_user_overrides` 一致：**用户设置 → 全局 Config**。

    ⚠️ 这里刻意**不读** ``os.getenv`` —— 真正被运行时代码消费的是 ``Config``
    实例；``.env`` 改动后 ``os.environ`` 未必同步（上游走的是 ``Config`` 单例
    重载），用环境变量判断「全局值」会与真实行为对不上。
    """
    spec = get_spec(key)
    if spec is None:
        return None, "unsupported"

    if tenant_id is not None:
        raw = load_user_settings(tenant_id).get(spec.env_key)
        if raw not in (None, ""):
            decoded = decode_value(spec, raw)
            if decoded not in (None, "", []):
                return decoded, "user"

    try:
        from src.config import get_config

        return getattr(get_config(), spec.config_attr, None), "global"
    except Exception as exc:  # noqa: BLE001 - 读全局配置失败不应让接口整体 500
        logger.warning("[tenancy] cannot read global value for %s: %s", key, exc)
        return None, "global"


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


def tenant_config(config=None):
    """返回叠加了**当前请求用户**设置的 ``Config`` 副本。

    没有多用户上下文（未启用多用户 / 匿名请求 / 后台任务线程）时原样返回
    传入的 ``config``，行为与改动前完全一致。

    ⚠️ 必须在**有 contextvar 的线程**里调用（例如 FastAPI 的请求处理函数）。
    把结果传给线程池即可，不要在 worker 线程里再调一次 —— ``contextvars``
    不跨线程，那里拿不到当前用户。
    """
    from src.tenancy.context import current_user_id, multiuser_enabled

    if config is None:
        from src.config import get_config

        config = get_config()
    if not multiuser_enabled():
        return config
    tenant_id = current_user_id()
    if tenant_id is None:
        return config
    return apply_user_overrides(config, tenant_id)


def effective_stock_list(tenant_id: Optional[int]) -> Optional[List[str]]:
    """返回该用户的自选股列表；未配置时返回 ``None`` 以表示「沿用全局」。"""
    from src.tenancy.context import multiuser_enabled

    if tenant_id is None or not multiuser_enabled():
        return None
    settings = load_user_settings(tenant_id)
    if "STOCK_LIST" not in settings:
        return None
    raw = settings.get("STOCK_LIST")
    if not raw:
        return []
    spec = get_spec("STOCK_LIST")
    decoded = decode_value(spec, raw)
    return decoded if decoded is not None else []


def effective_push_enabled(tenant_id: Optional[int]) -> bool:
    """推送开关的生效值：用户设置 → 全局 Config → 默认 ``True``。"""
    value, _source = effective_config_value("PUSH_ENABLED", tenant_id)
    if value is None:
        return True
    return bool(value)


def effective_push_times(tenant_id: Optional[int]) -> List[str]:
    """**自选股**推送时刻的生效值：用户设置 → 全局 Config → 默认。

    返回值一定经过 :func:`normalize_schedule_times`，因此可以放心当成
    「已排序、去重、格式合法」的 ``HH:MM`` 列表使用。

    读 ``STOCK_PUSH_TIMES``，并兼容历史 ``PUSH_TIMES``：拆分前用户改的是
    后者，直接不认会让既有用户的设置悄悄失效。
    """
    from src.scheduler import (
        DEFAULT_PUSH_TIMES,
        STOCK_PUSH_TIMES_DEFAULT,
        normalize_schedule_times,
    )

    value, _source = effective_config_value("STOCK_PUSH_TIMES", tenant_id)
    if not value:
        # 兼容旧键：拆分前用户设置写的是 PUSH_TIMES。
        value, _source = effective_config_value("PUSH_TIMES", tenant_id)
    if not value:
        value = list(STOCK_PUSH_TIMES_DEFAULT)
    if isinstance(value, str):
        value = [item for item in value.split(",") if item.strip()]
    return normalize_schedule_times(
        list(value), fallback_time=STOCK_PUSH_TIMES_DEFAULT[0]
    )


def effective_market_push_times(tenant_id: Optional[int] = None) -> List[str]:
    """**大盘复盘**推送时刻的生效值：全局 Config → 默认。

    刻意**不读**用户设置：大盘时刻是平台策略（全租户共享一份复盘），用户
    改不了。``tenant_id`` 保留在签名里是为了与 :func:`effective_push_times`
    对称，调用点不必区分两条时间线的取法。
    """
    from src.scheduler import MARKET_PUSH_TIMES_DEFAULT, normalize_schedule_times

    value, _source = effective_config_value("MARKET_PUSH_TIMES", None)
    if not value:
        value = list(MARKET_PUSH_TIMES_DEFAULT)
    if isinstance(value, str):
        value = [item for item in value.split(",") if item.strip()]
    return normalize_schedule_times(
        list(value), fallback_time=MARKET_PUSH_TIMES_DEFAULT[0]
    )


#: 内部状态键的统一前缀。
#: 这些键**不在** :data:`USER_SETTING_SPECS` 白名单里，因此不会出现在
#: ``/settings`` 响应体中，也不会被用户的写请求碰到。
INTERNAL_KEY_PREFIX = "__"


def read_internal_setting(tenant_id: Optional[int], key: str) -> Optional[str]:
    """读取一个内部状态键（如推送去重标记）。不存在时返回 ``None``。"""
    if tenant_id is None:
        return None
    normalized = (key or "").strip().upper()
    if not normalized:
        return None
    return load_user_settings(int(tenant_id)).get(normalized)


def write_internal_setting(tenant_id: int, key: str, value: Optional[str]) -> None:
    """写入（或删除）一个内部状态键。

    ⚠️ 这条路径**绕过** :data:`_FORBIDDEN_KEYS` 与白名单校验，因此只允许
    平台自身使用（推送去重）。键名强制以 ``__`` 开头，防止把用户可写键
    误塞进来。``value`` 为 ``None`` 表示删除该键。
    """
    normalized = (key or "").strip().upper()
    if not normalized.startswith(INTERNAL_KEY_PREFIX):
        raise ValueError(
            f"internal setting key must start with {INTERNAL_KEY_PREFIX!r}: {normalized!r}"
        )
    if tenant_id is None:
        raise ValueError("tenant_id is required for internal settings")

    from src.storage import get_db

    with get_db().session_scope() as session:
        row = (
            session.query(TenantUserSetting)
            .filter(
                TenantUserSetting.tenant_id == int(tenant_id),
                TenantUserSetting.key == normalized,
            )
            .one_or_none()
        )
        if value is None:
            if row is not None:
                session.delete(row)
            return
        if row is None:
            session.add(
                TenantUserSetting(tenant_id=int(tenant_id), key=normalized, value=str(value))
            )
        else:
            row.value = str(value)


def public_settings_view(tenant_id: int) -> Dict[str, Any]:
    """构造给前端 / API 的配置视图（敏感项掩码）。

    每个键返回：

    * ``value`` —— **用户自己设的**原始值（未设则为 ``None``）；
    * ``effective_value`` —— 真正会生效的值（用户值 → 全局值），已掩码；
    * ``source`` —— ``"user"`` 或 ``"global"``，标明生效值来自哪一侧。

    区分「自己设的」与「真正生效的」很关键：用户没设过时，``value`` 是
    ``None``，但 ``effective_value`` 会回落到全局配置 —— 前端要显示的是后者，
    否则会把「沿用全局的 true」显示成「未启用」。
    """
    raw = load_user_settings(tenant_id)
    try:
        from src.config import get_config

        global_config = get_config()
    except Exception as exc:  # noqa: BLE001 - 视图构造失败不应让接口整体 500
        logger.warning("[tenancy] cannot read global config for settings view: %s", exc)
        global_config = None

    view: Dict[str, Any] = {}
    for spec in USER_SETTING_SPECS:
        value = raw.get(spec.env_key)
        own = decode_value(spec, value) if value not in (None, "") else None
        if own not in (None, "", []):
            effective, source = own, "user"
        else:
            effective, source = getattr(global_config, spec.config_attr, None), "global"
        view[spec.env_key] = {
            "label": spec.label,
            "kind": spec.kind,
            "secret": spec.secret,
            "readonly": spec.readonly,
            "configured": bool(value),
            "value": mask_value(spec, value),
            "effective_value": mask_value(
                spec, None if effective is None else encode_value(spec, effective)
            ),
            "source": source,
        }
    return view


# ---------------------------------------------------------------------------
# 自选股（按用户）
#
# ⚠️ 为什么不复用上游的 ``POST /api/v1/stocks/watchlist/add``：
# 那条路径写的是**进程级全局** ``STOCK_LIST``（经 SystemConfigService 落到
# 系统配置），在多租户下会造成「A 加自选，B 也跟着变」的串号。外部系统
# （MCP / CowAgent）必须走下面这组按用户读写的接口。
# ---------------------------------------------------------------------------

#: 股票代码的宽松白名单：字母数字开头，允许 . - _ ，总长 ≤16。
#: 刻意不在这里校验市场归属 —— 那属于数据源层的职责，这里只挡住明显的垃圾输入。
_STOCK_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,15}$")


class WatchlistError(ValueError):
    """自选股操作失败（代码为空 / 格式不合法）。"""


def normalize_stock_code(code: Any) -> str:
    """规范化股票代码：去空白、转大写、校验格式。"""
    text = str(code or "").strip().upper()
    if not text:
        raise WatchlistError("股票代码不能为空")
    if not _STOCK_CODE_RE.match(text):
        raise WatchlistError(f"股票代码格式不合法: {code!r}")
    return text


def global_stock_list() -> List[str]:
    """全局自选（``.env`` 里的 ``STOCK_LIST``）。读不到时返回空列表。"""
    try:
        from src.config import get_config

        raw = getattr(get_config(), "stock_list", None) or []
    except Exception as exc:  # noqa: BLE001 —— 配置读取失败不应让自选接口整体 500
        logger.warning("[tenancy] 读取全局 STOCK_LIST 失败: %s", exc)
        return []
    if isinstance(raw, str):
        raw = _split_csv(raw)
    return [str(item).strip().upper() for item in raw if str(item).strip()]


def _write_user_stock_list(tenant_id: int, codes: List[str]) -> List[str]:
    """写回用户个人自选（大写 + 去重 + 保序）。"""
    seen: set = set()
    normalized: List[str] = []
    for code in codes:
        text = str(code).strip().upper()
        if text and text not in seen:
            seen.add(text)
            normalized.append(text)
    save_user_settings(tenant_id, {"STOCK_LIST": normalized})
    return normalized


def user_stock_list(tenant_id: int) -> Optional[List[str]]:
    """用户自己的自选；未配置返回 ``None``。

    ⚠️ 返回值 ``None`` 仅表示「该租户没有个人配置」，**不再表示「沿用全局」**。
    全局 ``STOCK_LIST`` 是进程级基础设施配置，多租户下任何情况下都不应当被
    当作个人自选的兜底 —— 否则未配置用户会看到全局值，而全局值一旦被写入
    污染，就等价于跨租户泄漏。调用方需要「空列表」语义时用
    :func:`resolve_stock_list`。
    """
    return effective_stock_list(tenant_id)


def resolve_stock_list(tenant_id: int) -> Dict[str, Any]:
    """自选视图：只返回该租户自己的列表，**不回退全局**。

    未配置个人自选时返回空列表（``source="user"``）。这是刻意的隔离设计：
    多租户下不存在「共享自选」的概念，全局 ``STOCK_LIST`` 只是单用户模式
    与调度兜底用的基础设施配置，不参与租户视图。
    """
    own = user_stock_list(tenant_id)
    if own is None:
        return {"stock_codes": [], "source": "user", "inherited_from_global": False}
    return {"stock_codes": own, "source": "user", "inherited_from_global": False}


def _base_list_for_mutation(tenant_id: int):
    """取「改动的起点」：只取个人列表，未配置从空列表开始。

    返回 ``(base, inherited)``。``inherited`` 恒为 ``False`` —— 保留该键是
    为了兼容既有返回结构，避免调用方 KeyError。
    """
    own = user_stock_list(tenant_id)
    if own is None:
        return [], False
    return list(own), False


def add_user_stock(tenant_id: int, stock_code: Any) -> Dict[str, Any]:
    """加入自选。

    ⚠️ 用户尚未配置个人列表时**从空列表开始**，不再继承全局 —— 继承会让
    「加一只」把全局其他租户的股票一并复制进个人列表。
    """
    code = normalize_stock_code(stock_code)
    base, inherited = _base_list_for_mutation(tenant_id)
    already_present = code in base
    if not already_present:
        base = base + [code]
    codes = _write_user_stock_list(tenant_id, base)
    return {
        "stock_codes": codes,
        "source": "user",
        "inherited_from_global": inherited,
        "added": code,
        "already_present": already_present,
    }


def remove_user_stock(tenant_id: int, stock_code: Any) -> Dict[str, Any]:
    """从自选移除：只操作该租户个人列表，未配置时起点为空。"""
    code = normalize_stock_code(stock_code)
    base, inherited = _base_list_for_mutation(tenant_id)
    was_present = code in base
    if was_present:
        base = [item for item in base if item != code]
    codes = _write_user_stock_list(tenant_id, base)
    return {
        "stock_codes": codes,
        "source": "user",
        "inherited_from_global": inherited,
        "removed": code,
        "was_present": was_present,
    }


def replace_user_stock_list(tenant_id: int, codes: Sequence[Any]) -> Dict[str, Any]:
    """整体替换个人自选。"""
    normalized = [normalize_stock_code(code) for code in (codes or [])]
    result = _write_user_stock_list(tenant_id, normalized)
    return {"stock_codes": result, "source": "user", "inherited_from_global": False}


def reset_user_stock_list(tenant_id: int) -> Dict[str, Any]:
    """清空个人自选（删除该租户的 STOCK_LIST 覆盖）。

    删除后 :func:`resolve_stock_list` 返回空列表，**不再回落到全局**。
    """
    delete_user_settings(tenant_id, ["STOCK_LIST"])
    return resolve_stock_list(tenant_id)


def extract_inquired_stocks(query: str) -> List[Tuple[str, str]]:
    """从用户咨询文本中提取股票列表 [(code, name), ...]。

    - 排除负向/删除/管理类指令（如“删除”、“移除”、“清空”、“查看自选”、“解绑”等）；
    - 支持股票全名/别名识别（结合分词管道与名称库）；
    - 支持直接 5-6 位股票代码提取；
    - 结果去重保序。
    """
    if not query or not isinstance(query, str):
        return []

    text = query.strip()
    negative_patterns = [
        r"删除", r"移除", r"去掉", r"清空", r"取消.*自选",
        r"查看.*自选", r"我的自选", r"自选列表", r"自选股列表",
        r"解绑", r"登录", r"绑定", r"whoami", r"我的账号",
    ]
    for pat in negative_patterns:
        if re.search(pat, text, re.IGNORECASE):
            return []

    found_stocks: List[Tuple[str, str]] = []
    seen_codes: set = set()

    # 1. 尝试使用分词层识别股票实体
    try:
        from src.agent.web_intent_tokenizer import _preprocess_text
        _, tokens = _preprocess_text(text)
        for t in tokens:
            if getattr(t, "stocks", None):
                for s in t.stocks:
                    code = str(s.code).strip().upper()
                    name = str(s.name or "").strip()
                    if code and code not in seen_codes:
                        seen_codes.add(code)
                        found_stocks.append((code, name))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[extract_inquired_stocks] tokenizer skipped: %s", exc)

    # 2. 正则提取 A 股 / 港股代码
    code_matches = re.findall(
        r"\b(00\d{4}|30\d{4}|60\d{4}|68\d{4}|43\d{4}|83\d{4}|87\d{4}|92\d{4})\b",
        text,
    )
    hk_matches = re.findall(r"\b(HK\d{5}|0\d{4})\b", text, re.IGNORECASE)

    all_raw_codes = code_matches + hk_matches
    for raw in all_raw_codes:
        code = str(raw).strip().upper()
        if code.startswith("HK"):
            code = code[2:]
        if code and code not in seen_codes:
            name = ""
            try:
                from src.services.name_to_code_resolver import lookup_stock_by_code
                st = lookup_stock_by_code(code)
                if st:
                    name = getattr(st, "name", "") or ""
            except Exception:  # noqa: BLE001
                pass
            seen_codes.add(code)
            found_stocks.append((code, name))

    return found_stocks

