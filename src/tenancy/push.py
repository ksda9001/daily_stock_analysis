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
    effective_market_push_times,
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


def _fmt_pct(value: Any, *, signed: bool = True) -> str:
    """百分比。``signed=False`` 用于振幅 —— 振幅恒为正，带 ``+`` 号反而费解。"""
    if value is None:
        return "--"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "--"
    return f"{number:+.2f}%" if signed else f"{number:.2f}%"


def _fmt_amount(value: Any) -> str:
    """成交额按 亿/万 缩写。

    与 ``notification.NotificationService._format_amount_cn`` **保持同一口径**
    （1e8 以上转「亿」、1e4 以上转「万」），这样推送里的数字与页面/研报里
    看到的读法一致 —— 用户不会在微信上看到「1627385746.00」这种原始值。
    """
    if value is None:
        return "--"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "--"
    if amount != amount:  # NaN
        return "--"
    sign = "-" if amount < 0 else ""
    magnitude = abs(amount)
    if magnitude >= 1e8:
        return f"{sign}{magnitude / 1e8:.2f} 亿"
    if magnitude >= 1e4:
        return f"{sign}{magnitude / 1e4:.2f} 万"
    return f"{sign}{magnitude:.0f}"


def _fmt_volume(value: Any) -> str:
    """成交量按 亿股/万股 缩写。

    ⚠️ 与成交额的换算**不同**：成交量的单位是「股」，A 股一只票一天常见量级
    在千万到几亿股，直接用原始值会有 9~10 位数字，手机上读不出量级。
    """
    if value is None:
        return "--"
    try:
        volume = float(value)
    except (TypeError, ValueError):
        return "--"
    if volume != volume:  # NaN
        return "--"
    if volume >= 1e8:
        return f"{volume / 1e8:.2f} 亿股"
    if volume >= 1e4:
        return f"{volume / 1e4:.2f} 万股"
    return f"{volume:.0f} 股"


def _fmt_ratio(value: Any, *, suffix: str = "") -> str:
    """倍率类字段（量比/换手率比率），无值时给 ``--``。"""
    if value is None:
        return "--"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "--"
    return f"{number:.2f}{suffix}"


def fetch_indices() -> List[Dict[str, Any]]:
    """取 A 股主要指数行情。失败返回空列表（推送里会显示为「暂不可用」）。

    ⚠️ **保留数据源的原始字段**，不要在抓取层裁剪。渲染层需要哪些字段是
    渲染层的事 —— 这里一旦只挑 4 个字段，后面想补全是补不回来的（只能改这里）。
    实测 ``get_main_indices`` 提供：``current`` / ``change`` / ``change_pct`` /
    ``open`` / ``high`` / ``low`` / ``prev_close`` / ``volume`` / ``amount`` /
    ``amplitude``。
    """
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
        item = dict(row)          # 先整体保留，再覆盖规范化后的 code/name
        item["code"] = code
        item["name"] = name
        out.append(item)
    return out


def _fetch_one_quote(code: str) -> Dict[str, Any]:
    """抓单只股票行情。

    ⚠️ 每次调用都**新建** ``DataFetcherManager``。共享一个 manager 会让各 worker
    在它的 per-fetcher 调用锁上串行化，把「并发」变回「顺序」—— 这一点在
    ``portfolio_service._prefetch_realtime_position_prices`` 的注释里有明确记录。

    ⚠️ **这里保留数据源的全部字段**（原先只留 4 个：code/name/price/change_pct）。
    这是 2026-09-15 用户反馈「推送太省略」的**根因** —— 数据在抓取层就被丢弃，
    渲染层再想补全也无从取起。底层的 ``RealtimeQuote`` 有 21 个字段，实测可用：
    ``open_price`` / ``high`` / ``low`` / ``pre_close`` / ``change_amount`` /
    ``volume`` / ``amount`` / ``volume_ratio`` / ``turnover_rate`` /
    ``amplitude`` / ``pe_ratio`` / ``pb_ratio`` / ``total_mv`` / ``circ_mv`` /
    ``source`` / ``fetched_at`` 等。

    只丢弃 ``None`` 值以省内存 —— 保留 ``None`` 会让下游的 ``.get()`` 拿到
    ``None`` 而非「键不存在」，两种情况都要处理，不如统一成前者。
    """
    try:
        from data_provider.base import DataFetcherManager

        quote = DataFetcherManager().get_realtime_quote(code, log_final_failure=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[push] realtime quote failed for %s: %s", code, exc)
        return {"code": code, "ok": False}

    if quote is None:
        return {"code": code, "ok": False}

    import dataclasses

    fields: Dict[str, Any] = {}
    if dataclasses.is_dataclass(quote):
        for field in dataclasses.fields(quote):
            fields[field.name] = getattr(quote, field.name, None)
    else:  # 兜底：不是 dataclass 就退回 __dict__（历史上曾是普通对象）
        fields.update(getattr(quote, "__dict__", {}) or {})

    out: Dict[str, Any] = {"ok": True}
    for key, value in fields.items():
        if value is None:
            continue
        # ``RealtimeSource`` 之类的枚举取 ``.value``，否则 JSON 序列化会失败。
        out[key] = getattr(value, "value", value)

    out["code"] = out.get("code") or code
    return out


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
# 完整报告（推送正文的主体）
# ---------------------------------------------------------------------------

#: 大盘复盘记录在 ``analysis_history`` 里的 ``code`` / ``report_type`` 约定值。
MARKET_REVIEW_CODE = "MARKET"

#: ``collect_report_text`` 的选段标记 —— 决定这次组装哪几半内容。
#:
#: 两条推送时间线各自需要不同的正文：大盘复盘只在收盘后有意义（数据定稿），
#: 自选股研报则在开盘后就能看。用选段而不是「拿全量再裁掉一半」，是因为裁
#: 字符串要靠标题匹配，而报告正文里的标题格式由渲染层决定、随时可能变。
SECTION_MARKET = "market"
SECTION_STOCKS = "stocks"
MARKET_REVIEW_REPORT_TYPE = "market_review"

#: 个股完整研报的 ``report_type``（``simple`` 是精简版，不用）。
STOCK_REPORT_TYPE = "full"

#: 报告新鲜度上限（小时）。
#:
#: 推一份三天前的研报是**有害的** —— 里面的买卖点位早已失效，用户照做会亏钱。
#: 宁可退回实时行情，也不要推过期结论。
REPORT_MAX_AGE_HOURS = 24


def _latest_record_id(
    tenant_id: Optional[int],
    report_type: str,
    code: str,
    since: datetime,
) -> Optional[int]:
    """取最新一条分析记录的主键。

    ⚠️ **必须绑定租户上下文**：``src/tenancy/scope.py`` 在 ORM 层装了
    ``do_orm_execute`` 守卫，对 ``analysis_history`` 自动施加租户谓词，且
    失败策略是 **fail-closed** —— 上下文无身份时收窄到系统属主（1），
    表现为**静默查不到任何数据**（不报错）。本函数是后台任务，
    没有请求中间件代劳，必须显式 ``bind_user``。

    ``tenant_id=None`` 用于大盘复盘 —— 它是**共享行**（``SHARED_TENANT_ID``），
    不属于任何用户，所有租户可读。这不是「放宽过滤」，而是这类数据本来
    就没有归属（见 ``scope.py`` 的「共享行」一节）。
    """
    try:
        from sqlalchemy import desc, select

        from src.storage import AnalysisHistory, get_db
        from src.tenancy.context import SHARED_TENANT_ID, bind_user
    except Exception as exc:  # noqa: BLE001 - 依赖缺失不应让推送整体失败
        logger.warning("[push] cannot import storage for reports: %s", exc)
        return None

    scope_id = SHARED_TENANT_ID if tenant_id is None else int(tenant_id)
    try:
        with bind_user(scope_id):
            with get_db().session_scope() as session:
                stmt = select(AnalysisHistory).where(
                    AnalysisHistory.report_type == report_type,
                    AnalysisHistory.code == code,
                    AnalysisHistory.created_at >= since,
                )
                if tenant_id is not None:
                    stmt = stmt.where(AnalysisHistory.tenant_id == int(tenant_id))
                stmt = stmt.order_by(desc(AnalysisHistory.created_at)).limit(1)
                record = session.execute(stmt).scalars().first()
                return int(record.id) if record is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[push] report lookup failed (tenant=%s type=%s code=%s): %s",
            tenant_id,
            report_type,
            code,
            exc,
        )
        return None


def _render_record_markdown(record_id: int, scope_id: int) -> Optional[str]:
    """渲染单条记录的报告全文。

    复用 ``HistoryService.get_markdown_report()`` —— 这是 Web 首页「查看报告」
    与 DSA 原生推送（Server酱3 / 企业微信）**共用的同一个渲染入口**。
    不自己拼 Markdown：一旦各写一份，口径迟早分叉。

    随后做 ``strip_hidden_markdown_metadata`` —— 这一步不是可选项：
    ``get_markdown_report`` 的产物开头带 ``[dsa-market-region]: # (cn)``
    这类内部引用定义，DSA 原生推送在发送前**同样会剥掉它**
    （``serverchan3_sender`` 第 71 行）。少了这步，用户会在正文最上面
    看到一行莫名其妙的内部标记。
    """
    try:
        from src.formatters import strip_hidden_markdown_metadata
        from src.services.history_service import HistoryService
        from src.storage import get_db
        from src.tenancy.context import bind_user
    except Exception as exc:  # noqa: BLE001
        logger.warning("[push] cannot import history service: %s", exc)
        return None

    try:
        with bind_user(scope_id):
            text = HistoryService(get_db()).get_markdown_report(str(record_id))
    except Exception as exc:  # noqa: BLE001 - 单份报告失败不该拖垮整条推送
        logger.warning("[push] render report failed for record %s: %s", record_id, exc)
        return None
    if not text or not text.strip():
        return None
    return strip_hidden_markdown_metadata(text).strip()


def collect_report_text(
    tenant_id: int,
    codes: List[str],
    now: datetime,
    *,
    sections: Tuple[str, ...] = (SECTION_MARKET, SECTION_STOCKS),
) -> str:
    """收集该租户的推送正文：大盘复盘 + 各只自选股研报。

    组装顺序与读报习惯一致：**先大盘、后个股**。

    ⚠️ **不做长度分页。** 微信渠道层 ``channel/weixin/weixin_channel.py``
    已有 ``_split_text``（``TEXT_CHUNK_LIMIT`` = 4000），按
    「段落 → 行 → 硬切」切分，段间还会 ``sleep(0.5)``。业务层再分一次
    只会把表格从中间切断。

    返回 ``""`` 表示该租户暂时没有任何可用报告，由调用方决定兜底。
    """
    from src.tenancy.context import SHARED_TENANT_ID

    since = now - timedelta(hours=REPORT_MAX_AGE_HOURS)
    parts: List[str] = []

    # 大盘复盘：共享内容，不按租户过滤。
    if SECTION_MARKET in sections:
        review_id = _latest_record_id(
            None, MARKET_REVIEW_REPORT_TYPE, MARKET_REVIEW_CODE, since
        )
        if review_id is not None:
            review = _render_record_markdown(review_id, SHARED_TENANT_ID)
            if review:
                parts.append(review)
        else:
            logger.info(
                "[push] tenant %s: no market review within %sh",
                tenant_id,
                REPORT_MAX_AGE_HOURS,
            )

    # 个股研报：**必须**按租户过滤 —— 私有数据，跨租户可见即越权。
    if SECTION_STOCKS in sections:
        for code in codes:
            clean = str(code or "").strip().upper()
            if not clean:
                continue
            record_id = _latest_record_id(
                int(tenant_id), STOCK_REPORT_TYPE, clean, since
            )
            if record_id is None:
                logger.info("[push] tenant %s: no fresh report for %s", tenant_id, clean)
                continue
            body = _render_record_markdown(record_id, int(tenant_id))
            if body:
                parts.append(body)

    if not parts:
        return ""

    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _render_index_table(indices: List[Dict[str, Any]]) -> List[str]:
    """大盘指数表。字段口径对齐 ``notification._append_market_snapshot``。"""
    lines: List[str] = [
        "| 指数 | 最新 | 涨跌幅 | 涨跌额 | 今开 | 最高 | 最低 | 昨收 | 振幅 | 成交额 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for item in indices:
        name = item.get("name") or item.get("code") or "指数"
        lines.append(
            "| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |"
            % (
                name,
                _fmt_price(item.get("current")),
                _fmt_pct(item.get("change_pct")),
                _fmt_price(item.get("change")),
                _fmt_price(item.get("open")),
                _fmt_price(item.get("high")),
                _fmt_price(item.get("low")),
                _fmt_price(item.get("prev_close")),
                _fmt_pct(item.get("amplitude"), signed=False),
                _fmt_amount(item.get("amount")),
            )
        )
    return lines


def _render_stock_block(item: Dict[str, Any]) -> List[str]:
    """单只自选股的行情块。

    刻意**一只一块**而不是塞进一张大表：微信里宽表会折行到读不出来，而
    「标签：值」的竖排在小屏上反而清楚。字段与页面 ``/stocks/{code}/quote``
    以及研报里的「当日行情」表保持一致。
    """
    code = item.get("code") or ""
    name = item.get("name") or code
    lines = [
        f"**{name} {code}**",
        "",
        "| 现价 | 涨跌幅 | 涨跌额 | 今开 | 最高 | 最低 | 昨收 |",
        "|---|---|---|---|---|---|---|",
        "| %s | %s | %s | %s | %s | %s | %s |"
        % (
            _fmt_price(item.get("price")),
            _fmt_pct(item.get("change_pct")),
            _fmt_price(item.get("change_amount")),
            _fmt_price(item.get("open_price")),
            _fmt_price(item.get("high")),
            _fmt_price(item.get("low")),
            _fmt_price(item.get("pre_close")),
        ),
        "",
        "| 振幅 | 量比 | 换手率 | 成交量 | 成交额 |",
        "|---|---|---|---|---|",
        "| %s | %s | %s | %s | %s |"
        % (
            _fmt_pct(item.get("amplitude"), signed=False),
            _fmt_ratio(item.get("volume_ratio")),
            _fmt_pct(item.get("turnover_rate"), signed=False),
            _fmt_volume(item.get("volume")),
            _fmt_amount(item.get("amount")),
        ),
    ]
    return lines


def render_digest(
    *,
    slot: str,
    indices: List[Dict[str, Any]],
    watchlist: List[Dict[str, Any]],
    watchlist_source: str,
    now: datetime,
) -> str:
    """把抓到的行情渲染成推送正文。

    数据质量对齐 DSA 自身的报告口径（``notification.NotificationService``
    的「当日行情」表）：成交额按 亿/万 缩写、涨跌幅带符号、振幅不带符号。

    ⚠️ **不在这里做长度分页。** 微信渠道层
    (``channel/weixin/weixin_channel.py``) 已有 ``_split_text``：超过
    ``TEXT_CHUNK_LIMIT``(4000) 会自动按「段落 → 行 → 硬切」的优先级切分，
    段间还会 ``sleep(0.5)``。这里再分一次只会把表格从中间切断，
    反而破坏可读性。让它长，交给渠道切。
    """
    lines: List[str] = []
    lines.append(f"**A股行情速览 · {slot}**" if slot else "**A股行情速览**")
    lines.append("")

    lines.append("**大盘**")
    lines.append("")
    if indices:
        lines.extend(_render_index_table(indices))
    else:
        lines.append("- 指数行情暂不可用")
    lines.append("")

    lines.append("**自选股**")
    lines.append("")
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
            block = _render_stock_block(item)
            if lines and lines[-1] != "":
                lines.append("")
            lines.extend(block)
            lines.append("")
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

    # 两条独立时间线：自选股（用户可改）与大盘复盘（平台固定）。各自算到点
    # 情况，再合并成「这次该推什么」——同一时刻若两边都命中，就一次推完整
    # 两条正文，而不是发两条消息。
    stock_times = effective_push_times(tenant_id)
    market_times = effective_market_push_times(tenant_id)
    times = sorted(set(stock_times) | set(market_times))

    if not force:
        if not is_trading_day(moment):
            return {
                **base,
                "skip": True,
                "reason": "non_trading_day",
                "push_times": times,
                "message": "今天不是 A 股交易日",
            }

        stock_slot, _stock_reason = due_slot(tenant_id, stock_times, moment)
        market_slot, _market_reason = due_slot(tenant_id, market_times, moment)

        if stock_slot is None and market_slot is None:
            # 两边都没到点。报告原因时以自选股为准（用户能改的就是它），
            # 否则用户看到 already_sent 会以为是平台漏推。
            return {
                **base,
                "skip": True,
                "reason": _stock_reason if _stock_reason != "not_due" else _market_reason,
                "push_times": times,
                "stock_push_times": stock_times,
                "market_push_times": market_times,
                "message": "当前不在推送时段",
            }

        slot = stock_slot or market_slot
        reason = ""
        parts: List[str] = []
        if market_slot is not None:
            parts.append(SECTION_MARKET)
        if stock_slot is not None:
            parts.append(SECTION_STOCKS)
    else:
        # 手动触发：两条正文都推，便于联调时一次看全。
        slot = (stock_times or market_times or [""])[0]
        reason = ""
        parts = [SECTION_MARKET, SECTION_STOCKS]

    indices = fetch_indices()
    stock_view = resolve_stock_list(tenant_id)
    codes = list(stock_view.get("stock_codes") or [])
    watchlist = fetch_watchlist_quotes(codes)

    # 正文主体 = 该租户已落库的完整分析报告（大盘复盘 + 各只自选股研报）。
    # 这份内容与 DSA 原生推送（Server酱3 / 企业微信）**完全同源** ——
    # 都出自 notification.NotificationService 的 dashboard 渲染，经由
    # HistoryService.get_markdown_report() 取回。不再自己拼一份简版，
    # 否则两处口径必然分叉（用户看到的微信推送和 App 推送不一样）。
    #
    # ⚠️ 只读 analysis_history，**不在这里跑分析**：完整分析个股约 105 秒/只，
    # 09:35 这个时段来不及。报告由 runtime_scheduler 的租户 fan-out 预先产出。
    report_text = collect_report_text(
        tenant_id, codes, moment, sections=tuple(parts)
    )

    watchlist_ok = [item for item in watchlist if item.get("ok")]
    if not report_text and not indices and not watchlist_ok:
        # 三条数据线都空 → 推出去只是一条「什么都拿不到」的噪声。
        # 刻意**不**写去重标记，让同一个时段内的下一次轮询还能重试。
        logger.warning("[push] user %s: no data available, skipping this slot", tenant_id)
        return {
            **base,
            "skip": True,
            "reason": "no_data",
            "push_times": times,
            "message": "行情与研报数据源暂不可用，本次不推送",
        }

    # 有报告就用报告；报告缺失时才退回实时行情，避免推出一条空消息。
    text = report_text or render_digest(
        slot=slot,
        indices=indices,
        watchlist=watchlist,
        watchlist_source=str(stock_view.get("source") or ""),
        now=moment,
    )

    if not force:
        # 只标记**本次真的推过**的时段。把两边都标上会让另一条时间线在
        # 当天剩余时间里静默失效 —— 15:30 推完大盘顺手把自选股也标了，
        # 用户就再也收不到自选股那条。
        if stock_slot:
            mark_sent(tenant_id, stock_slot, moment)
        if market_slot:
            mark_sent(tenant_id, market_slot, moment)

    logger.info(
        "[push] user %s slot=%s report=%d indices=%d watchlist=%d text_len=%d",
        tenant_id,
        slot or "-",
        len(report_text),
        len(indices),
        len(watchlist_ok),
        len(text),
    )

    return {
        **base,
        "skip": False,
        "slot": slot,
        "push_times": times,
        "stock_push_times": stock_times,
        "market_push_times": market_times,
        "sections": list(parts),
        "indices": indices,
        "watchlist": watchlist,
        "stock_codes": codes,
        "stock_list_source": stock_view.get("source"),
        "text": text,
    }
