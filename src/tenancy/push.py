# -*- coding: utf-8 -*-
"""行情推送（大盘 + 自选股）的聚合与到点判定。

与「定时分析」是两条独立链路
----------------------------
``src/services/runtime_scheduler.py`` 的定时任务跑的是**完整分析**：个股研报
约 105 秒/只、大盘复盘约 23 秒。本模块是另一条更轻的链路 —— 只取指数与自选股
的**实时行情**（单次 2-4 秒），因此可以落在开盘后 5 分钟（09:35）这种完整分析
来不及的时刻。**不要**为了省事把它接到分析结果上，那会让 09:35 这个时刻直接
不可用。

谁在调用
--------
CowAgent 侧为每个用户建了一个**轮询**任务，定期调
``POST /api/v1/tenancy/push/digest``。时间表存在 DSA 侧
（``dsa_user_settings`` 的 ``PUSH_ENABLED`` / ``PUSH_TIMES``），所以「取消推送」
与「改推送时间」都只改一处，不需要跨服务同步。本模块只回答两件事：
**现在该不该推**、**推什么**。

到点判定必须是幂等的
--------------------
调用方是轮询的，同一个时段必然会被问到多次。去重标记写在 ``dsa_user_settings``
的 ``__PUSH_LAST_SENT_<HHMM>``（``__`` 前缀 = 内部键，不出现在 ``/settings``
响应体里，用户的写请求也碰不到它，见
:func:`src.tenancy.settings.write_internal_setting`）。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from src.tenancy.settings import (
    effective_push_enabled,
    effective_push_times,
    read_internal_setting,
    resolve_stock_list,
    write_internal_setting,
)

logger = logging.getLogger(__name__)

#: 市场区域。推送目前只覆盖 A 股 —— 自选股与指数的口径都按 ``cn`` 取。
MARKET_REGION = "cn"

#: 到点判定的时间窗（分钟）。
#:
#: 调用方是轮询的，所以「到点」不是一个瞬间而是一个窗口：窗口内任意一次调用
#: 都会触发推送，之后由去重标记压住重复。窗口同时是**迟到上限** —— 超过这个
#: 时长就认为这次时机已经错过，宁可不推，也不要推一份时间标签对不上的行情
#: （用户看到「09:35 速览」但实际是 10:20 的数据，比不推更糟）。
PUSH_WINDOW_MINUTES = 30

#: 抓取自选股行情的并发度。与 ``portfolio_service`` 保持同一量级 —— 再高只会
#: 撞上数据源的限频。
MAX_QUOTE_WORKERS = 4

#: 批量预取的触发门槛。低于这个数量，逐只抓反而更快（省一次全市场请求）。
PREFETCH_THRESHOLD = 5

#: 内部去重键模板。``__`` 前缀由 ``settings.INTERNAL_KEY_PREFIX`` 强制。
_LAST_SENT_KEY_TEMPLATE = "__PUSH_LAST_SENT_{slot}"

#: 指数名兜底。正常路径下名字来自 ``get_main_indices``；这里只在数据源返回
#: 残缺字段时补上，避免推送里出现「None」。
_INDEX_NAME_FALLBACK = {
    "sh000001": "上证指数",
    "000001": "上证指数",
    "sz399001": "深证成指",
    "399001": "深证成指",
    "sz399006": "创业板指",
    "399006": "创业板指",
}


# ---------------------------------------------------------------------------
# 时间与到点判定
# ---------------------------------------------------------------------------

def market_now(now: Optional[datetime] = None) -> datetime:
    """返回 A 股市场的本地时间。

    刻意**不**直接用 ``datetime.now()`` —— 那取决于容器的 ``TZ``。行情时刻
    必须按交易所所在时区判定，否则容器时区一变，09:35 就会变成别的时间点。
    日历不可用时回落到本机时间（fail-open）。
    """
    if now is not None:
        return now
    try:
        from src.core.trading_calendar import get_market_now

        return get_market_now(MARKET_REGION)
    except Exception as exc:  # noqa: BLE001 - 可选依赖，缺失不应中断推送
        logger.warning("[push] cannot resolve market time, using local clock: %s", exc)
        return datetime.now()


def is_trading_day(now: Optional[datetime] = None) -> bool:
    """当天是否 A 股交易日。**fail-open**。

    与 ``get_open_markets_today`` 的语义保持一致：交易所日历不可用时返回
    「开市」。宁可多推一次，也不要因为一个可选依赖缺失就静默关掉整个推送
    （那种故障没人会发现）。
    """
    try:
        from src.core.trading_calendar import get_open_markets_today

        return MARKET_REGION in get_open_markets_today()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[push] trading-day check failed, assuming open: %s", exc)
        return True


def _slot_key(slot: str) -> str:
    return _LAST_SENT_KEY_TEMPLATE.format(slot=slot.replace(":", ""))


def _slot_datetime(now: datetime, slot: str) -> Optional[datetime]:
    """把 ``HH:MM`` 落到 ``now`` 所在的那一天。格式非法时返回 ``None``。"""
    try:
        hour_text, minute_text = slot.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (ValueError, AttributeError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def due_slot(
    tenant_id: int,
    times: List[str],
    now: datetime,
) -> Tuple[Optional[str], str]:
    """判断此刻是否命中某个尚未推送的时段。

    返回 ``(命中的时段, 未命中的原因)``。命中时第二个元素为 ``""``。

    原因取值：

    * ``not_due`` —— 所有时段都还没到（一天里绝大多数轮询落在这里）
    * ``already_sent`` —— 到过点的时段今天都推过了
    * ``missed_window`` —— 到过点但已超出时间窗（服务重启 / 长时间不可用）
    * ``no_valid_times`` —— ``PUSH_TIMES`` 里没有一个合法值
    """
    today = now.strftime("%Y-%m-%d")
    pending = False
    missed = False
    valid_seen = False

    for slot in sorted(times):
        slot_dt = _slot_datetime(now, slot)
        if slot_dt is None:
            continue
        valid_seen = True
        if now < slot_dt:
            pending = True
            continue
        already_sent = read_internal_setting(tenant_id, _slot_key(slot)) == today
        if now - slot_dt > timedelta(minutes=PUSH_WINDOW_MINUTES):
            # 窗口已过。只有「本就没推过」才算错过 —— 推过的时段即便窗口已过
            # 也只是「今天做完了」，不该被报成异常。
            if not already_sent:
                missed = True
            continue
        if already_sent:
            continue
        return slot, ""

    if not valid_seen:
        return None, "no_valid_times"
    if pending:
        return None, "not_due"
    if missed:
        return None, "missed_window"
    return None, "already_sent"


def mark_sent(tenant_id: int, slot: str, now: datetime) -> None:
    """记下「今天这个时段已经推过了」，用于轮询去重。"""
    try:
        write_internal_setting(tenant_id, _slot_key(slot), now.strftime("%Y-%m-%d"))
    except Exception as exc:  # noqa: BLE001 - 去重写失败不应让推送本身失败
        logger.warning("[push] failed to record sent marker for %s: %s", slot, exc)


# ---------------------------------------------------------------------------
# 数据抓取
# ---------------------------------------------------------------------------

def _fmt_price(value: Any) -> str:
    if value is None:
        return "--"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "--"
    return f"{number:,.2f}"


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "--"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "--"
    return f"{number:+.2f}%"


def fetch_indices() -> List[Dict[str, Any]]:
    """取 A 股主要指数行情。失败返回空列表（推送里会显示为「暂不可用」）。"""
    try:
        from data_provider.base import DataFetcherManager

        rows = DataFetcherManager().get_main_indices(region=MARKET_REGION) or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("[push] fetch indices failed: %s", exc)
        return []

    out: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "")
        name = row.get("name") or _INDEX_NAME_FALLBACK.get(code)
        out.append(
            {
                "code": code,
                "name": name,
                "current": row.get("current"),
                "change_pct": row.get("change_pct"),
            }
        )
    return out


def _fetch_one_quote(code: str) -> Dict[str, Any]:
    """抓单只股票行情。

    ⚠️ 每次调用都**新建** ``DataFetcherManager``。共享一个 manager 会让各 worker
    在它的 per-fetcher 调用锁上串行化，把「并发」变回「顺序」—— 这一点在
    ``portfolio_service._prefetch_realtime_position_prices`` 的注释里有明确记录。
    """
    try:
        from data_provider.base import DataFetcherManager

        quote = DataFetcherManager().get_realtime_quote(code, log_final_failure=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[push] realtime quote failed for %s: %s", code, exc)
        return {"code": code, "ok": False}

    if quote is None:
        return {"code": code, "ok": False}
    return {
        "code": getattr(quote, "code", None) or code,
        "name": getattr(quote, "name", None),
        "price": getattr(quote, "price", None),
        "change_pct": getattr(quote, "change_pct", None),
        "ok": True,
    }


def fetch_watchlist_quotes(codes: List[str]) -> List[Dict[str, Any]]:
    """并发抓自选股行情，保持与入参一致的顺序。"""
    clean = [str(code).strip().upper() for code in (codes or []) if str(code).strip()]
    if not clean:
        return []

    if len(clean) >= PREFETCH_THRESHOLD:
        try:
            from data_provider.base import DataFetcherManager

            DataFetcherManager().prefetch_realtime_quotes(clean)
        except Exception as exc:  # noqa: BLE001 - 预取只是加速，失败无所谓
            logger.debug("[push] prefetch realtime quotes skipped: %s", exc)

    if len(clean) == 1:
        return [_fetch_one_quote(clean[0])]

    results: Dict[str, Dict[str, Any]] = {}
    workers = max(1, min(MAX_QUOTE_WORKERS, len(clean)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="push-quote") as pool:
        futures = {pool.submit(_fetch_one_quote, code): code for code in clean}
        for future in as_completed(futures):
            code = futures[future]
            try:
                results[code] = future.result()
            except Exception as exc:  # noqa: BLE001 - 兜底，单只失败不影响其余
                logger.warning("[push] quote worker crashed for %s: %s", code, exc)
                results[code] = {"code": code, "ok": False}
    return [results.get(code, {"code": code, "ok": False}) for code in clean]


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def render_digest(
    *,
    slot: str,
    indices: List[Dict[str, Any]],
    watchlist: List[Dict[str, Any]],
    watchlist_source: str,
    now: datetime,
) -> str:
    """把抓到的行情渲染成推送正文。

    刻意用「纯文本也能读」的 Markdown：这条正文会经由 CowAgent 的会话发到
    微信，而微信对 Markdown 的支持有限。加粗与短横线列表在纯文本下依然清楚，
    加粗在支持的客户端里也不会变成噪声。
    """
    lines: List[str] = []
    lines.append(f"**A股行情速览 · {slot}**" if slot else "**A股行情速览**")
    lines.append("")

    lines.append("**大盘**")
    if indices:
        for item in indices:
            name = item.get("name") or item.get("code") or "指数"
            lines.append(
                f"- {name} {_fmt_price(item.get('current'))} {_fmt_pct(item.get('change_pct'))}"
            )
    else:
        lines.append("- 指数行情暂不可用")
    lines.append("")

    lines.append("**自选股**")
    if not watchlist:
        if watchlist_source == "global":
            lines.append("- 还没有自选股（当前沿用全局列表，也是空的）")
        else:
            lines.append("- 还没有自选股，可在控制台或对话里添加")
    else:
        failed: List[str] = []
        for item in watchlist:
            code = item.get("code") or ""
            if not item.get("ok"):
                failed.append(code)
                continue
            name = item.get("name") or code
            lines.append(
                f"- {name} {code} {_fmt_price(item.get('price'))} {_fmt_pct(item.get('change_pct'))}"
            )
        if failed:
            lines.append(f"- （{'、'.join(failed)} 行情暂不可用）")
    lines.append("")

    lines.append(f"数据时间 {now.strftime('%Y-%m-%d %H:%M:%S')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def build_user_digest(
    tenant_id: int,
    *,
    now: Optional[datetime] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """组装某用户的行情推送。

    ``skip=True`` 表示「这次不推」，``reason`` 说明原因 —— 调用方应当**原样
    转达**，不要自己改写成「推送失败」：一天里绝大多数轮询都是 ``not_due``，
    那是正常状态而非错误。

    ``force=True`` 跳过到点判定与交易日判定（用于手动触发 / 联调），
    但仍然遵守 ``PUSH_ENABLED``。
    """
    moment = market_now(now)
    base: Dict[str, Any] = {
        "ok": True,
        "tenant_id": int(tenant_id),
        "checked_at": moment.isoformat(timespec="seconds"),
    }

    if not effective_push_enabled(tenant_id):
        return {**base, "skip": True, "reason": "disabled", "message": "该用户已关闭行情推送"}

    times = effective_push_times(tenant_id)

    if not force:
        if not is_trading_day(moment):
            return {
                **base,
                "skip": True,
                "reason": "non_trading_day",
                "push_times": times,
                "message": "今天不是 A 股交易日",
            }
        slot, reason = due_slot(tenant_id, times, moment)
        if slot is None:
            return {
                **base,
                "skip": True,
                "reason": reason,
                "push_times": times,
                "message": f"当前不在推送时段（{reason}）",
            }
    else:
        slot = times[0] if times else ""
        reason = ""

    indices = fetch_indices()
    stock_view = resolve_stock_list(tenant_id)
    codes = list(stock_view.get("stock_codes") or [])
    watchlist = fetch_watchlist_quotes(codes)

    if not indices and not any(item.get("ok") for item in watchlist):
        # 两条数据线都空 → 推出去只是一条「什么都拿不到」的噪声。
        # 刻意**不**写去重标记，让同一个时段内的下一次轮询还能重试。
        logger.warning("[push] user %s: no data available, skipping this slot", tenant_id)
        return {
            **base,
            "skip": True,
            "reason": "no_data",
            "push_times": times,
            "message": "行情数据源暂不可用，本次不推送",
        }

    text = render_digest(
        slot=slot,
        indices=indices,
        watchlist=watchlist,
        watchlist_source=str(stock_view.get("source") or ""),
        now=moment,
    )

    if slot and not force:
        mark_sent(tenant_id, slot, moment)

    logger.info(
        "[push] user %s slot=%s indices=%d watchlist=%d text_len=%d",
        tenant_id,
        slot or "-",
        len(indices),
        len([item for item in watchlist if item.get("ok")]),
        len(text),
    )

    return {
        **base,
        "skip": False,
        "slot": slot,
        "push_times": times,
        "indices": indices,
        "watchlist": watchlist,
        "stock_codes": codes,
        "stock_list_source": stock_view.get("source"),
        "text": text,
    }
