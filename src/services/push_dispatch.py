# -*- coding: utf-8 -*-
"""到点推送的执行入口。

调度器在 ``STOCK_PUSH_TIMES`` / ``MARKET_PUSH_TIMES`` 两个时刻各触发一次
:func:`dispatch_due_pushes`；本模块遍历所有可推送的租户，让
:func:`src.tenancy.push.build_user_digest` 判断「此刻该不该推、推什么」，
把结果交给 CowAgent 投递。

为什么这里不做「到点判定」
--------------------------
判定逻辑全在 ``build_user_digest`` 里（读 DSA 自己的时间表 + 交易日 + 去重
标记）。本模块只是「被叫醒 → 问一遍每个人 → 该推的推出去」。

这一点是刻意的：判定和触发分开，才不会有第二份时间表。调度器只管"什么时候
叫醒我"，`push` 模块只管"现在该不该推"。两边都以 DSA 的配置为准，用户改了
推送时间后，下一次触发就按新时间走，不需要重建任何任务。

与「轮询」的区别
----------------
这里每个时刻**只被叫醒一次**，不是隔几秒问一次「到了吗」。所有到点判定都由
DSA 自己的时间表驱动，不去询问任何外部服务。
"""

import logging
from typing import Dict, List

logger = logging.getLogger(__name__)


def _push_targets() -> List[int]:
    """返回需要检查推送的租户 ID 列表。

    只挑**绑定过微信**的用户：没有 ``wechat_id`` 就没有收件地址，
    CowAgent 无从投递（用户从没跟机器人说过话就拿不到 ``context_token``）。
    提前过滤掉，省得每个时刻对一批注定失败的账号各发一次 HTTP。
    """
    try:
        from src.tenancy.context import multiuser_enabled

        if not multiuser_enabled():
            # 单用户模式：系统属主自己一个租户。
            from src.tenancy.context import SYSTEM_TENANT_ID

            return [SYSTEM_TENANT_ID]

        from src.storage import get_db
        from src.tenancy.models import TenantUser
        from sqlalchemy import select

        with get_db().session_scope() as session:
            rows = session.execute(
                select(TenantUser.id).where(TenantUser.wechat_id.isnot(None))
            ).all()
        return [int(row[0]) for row in rows]
    except Exception as exc:  # noqa: BLE001 - 取不到目标不应让调度线程崩掉
        logger.warning("[push-dispatch] failed to load push targets: %s", exc)
        return []


def _deliver(tenant_id: int, text: str) -> bool:
    """把一条正文交给 CowAgent 投递到该租户绑定的微信。"""
    try:
        from src.storage import get_db
        from src.tenancy.models import TenantUser
        from sqlalchemy import select

        with get_db().session_scope() as session:
            row = session.execute(
                select(TenantUser.wechat_id).where(TenantUser.id == int(tenant_id))
            ).first()
        receiver = str(row[0]).strip() if row and row[0] else ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("[push-dispatch] cannot resolve wechat_id for %s: %s", tenant_id, exc)
        return False

    if not receiver:
        logger.info("[push-dispatch] tenant %s has no wechat_id, skipping", tenant_id)
        return False

    from src.notification_sender.cowagent_sender import CowAgentSender
    from src.config import get_config

    return CowAgentSender(get_config()).send_to_cowagent(text, receiver)


def dispatch_due_pushes(*, now=None, force: bool = False) -> Dict[str, int]:
    """检查所有租户，把到点的推送发出去。

    Args:
        now:  覆盖当前时间（联调/测试用）
        force: 跳过到点判定，直接为每个租户推一次

    Returns:
        统计字典：``checked`` / ``sent`` / ``skipped`` / ``failed``
    """
    stats = {"checked": 0, "sent": 0, "skipped": 0, "failed": 0}

    from src.tenancy.push import build_user_digest
    from src.tenancy.context import bind_user

    for tenant_id in _push_targets():
        stats["checked"] += 1
        try:
            # 推送链路要按租户过滤自选股与研报，必须带上租户上下文，
            # 否则 fail-closed 的守卫会把查询收窄成空结果（静默无数据）。
            with bind_user(tenant_id):
                result = build_user_digest(int(tenant_id), now=now, force=force)
        except Exception as exc:  # noqa: BLE001 - 单个租户失败不影响其他租户
            logger.warning("[push-dispatch] digest build failed for %s: %s", tenant_id, exc)
            stats["failed"] += 1
            continue

        if result.get("skip"):
            # 绝大多数时刻的正常结果（没到点 / 已推过 / 非交易日）。
            logger.debug(
                "[push-dispatch] tenant %s skipped: %s",
                tenant_id,
                result.get("reason"),
            )
            stats["skipped"] += 1
            continue

        messages = [m for m in (result.get("messages") or []) if str(m).strip()]
        if not messages:
            logger.info("[push-dispatch] tenant %s produced no message, skipping", tenant_id)
            stats["skipped"] += 1
            continue

        logger.info(
            "[push-dispatch] tenant %s slot=%s sections=%s messages=%d lens=%s",
            tenant_id,
            result.get("slot"),
            result.get("sections"),
            len(messages),
            [len(m) for m in messages],
        )

        # 逐条投递：一份报告一条消息。拼成一条再发会让微信渠道的分片边界
        # 落在两份报告之间，用户看到的就是排版错乱（见 collect_report_messages）。
        delivered = 0
        for message in messages:
            if _deliver(int(tenant_id), str(message)):
                delivered += 1
            else:
                break
        if delivered == len(messages):
            stats["sent"] += 1
        elif delivered:
            # 部分成功：已经发出的不再重发，避免用户收到重复消息。
            logger.warning(
                "[push-dispatch] tenant %s partial delivery %d/%d",
                tenant_id,
                delivered,
                len(messages),
            )
            stats["sent"] += 1
        else:
            stats["failed"] += 1

    if stats["sent"] or stats["failed"]:
        logger.info("[push-dispatch] done: %s", stats)
    return stats


def dispatch_stock_pushes(*, now=None) -> Dict[str, int]:
    """自选股时间线的触发入口。"""
    return dispatch_due_pushes(now=now)


def dispatch_market_pushes(*, now=None) -> Dict[str, int]:
    """大盘复盘时间线的触发入口。"""
    return dispatch_due_pushes(now=now)
