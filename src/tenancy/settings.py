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
    """用户自己的自选；未配置返回 ``None``（表示「沿用全局」）。"""
    return effective_stock_list(tenant_id)


def resolve_stock_list(tenant_id: int) -> Dict[str, Any]:
    """自选视图：区分「个人已配置」与「沿用全局」。"""
    own = user_stock_list(tenant_id)
    if own is not None:
        return {"stock_codes": own, "source": "user", "inherited_from_global": False}
    return {"stock_codes": global_stock_list(), "source": "global", "inherited_from_global": True}


def _base_list_for_mutation(tenant_id: int):
    """取「改动的起点」：优先个人列表，未配置则继承全局。

    返回 ``(base, inherited)``。
    """
    own = user_stock_list(tenant_id)
    if own is None:
        return global_stock_list(), True
    return list(own), False


def add_user_stock(tenant_id: int, stock_code: Any) -> Dict[str, Any]:
    """加入自选。

    ⚠️ 用户尚未配置个人列表时**先继承全局列表再追加**。否则「加一只」会把
    继承来的整份自选替换成这一只 —— 这是很容易踩的坑。
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
    """从自选移除（同样先继承全局，避免误删继承来的其他股票）。"""
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
    """清空个人自选，回落到全局配置。"""
    delete_user_settings(tenant_id, ["STOCK_LIST"])
    return resolve_stock_list(tenant_id)
