#!/usr/bin/env python3
"""端到端验证：推送任务自动补建（``_ensure_push_task``）。

模拟一次微信入站消息，触发「用户第一次跟 cow 说话就自动建推送任务」这条链路，
然后断言 ``tasks.json`` 里任务定义正确且幂等。

用法（在 cowagent 容器内，注意降权用户）::

    docker exec -u agent -e HOME=/home/agent cowagent python3 /tmp/e2e_push_task_provisioning.py

设计说明
--------
- 任务定义是**嵌套**结构，不是扁平字段::

      {"id": "dsa-push-<channel>-<receiver>",
       "enabled": true,
       "schedule": {"type": "interval", "seconds": 900},
       "action": {"type": "tool_call",
                  "call_name": "dsa_push_digest",
                  "call_params": {"wechat_id": "<receiver>"},
                  "receiver": "...", "channel_type": "weixin",
                  "suppress": {"json_field": "skip", "equals": true}},
       "next_run_at": "..."}

  早期探针按 ``tool_name`` / ``receiver`` 顶层字段断言，会误报失败。
- 收件人从真实的 ``~/cow/scheduler/recipients.json`` 读取，不自造。
- 幂等：重复调用不应新增任务。
- 脚本会在 CowAgent 侧**真的建一个任务**（可删；另见 `_clean_push_tasks.sh`）。

参见 ``docs/2026-09-14-push-feature-deploy.md`` §5.4。
"""
import json
import os
import sys

sys.path.insert(0, "/app")

FAILS = []


def chk(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg, flush=True)
    if not cond:
        FAILS.append(msg)
    return cond


def main():
    from agent.tools.scheduler.task_store import TaskStore
    from agent.tools.scheduler import integration as integ

    # ---------- 第 1 步：现状 ----------
    print("=== 1. 当前任务清单（应为空/不存在）===")
    store = TaskStore()
    tasks_before = store.load_tasks()
    print(f"  [info] 任务数: {len(tasks_before)}")
    for tid, t in tasks_before.items():
        print(f"        {tid}: tool={t.get('tool_name')} next={t.get('next_run_at')}")

    # ---------- 第 2 步：模拟入站消息 ----------
    print("")
    print("=== 2. 模拟微信入站消息，触发任务补建 ===")

    # 用真实的收件人（从 recipients.json 读）
    rec_file = "/home/agent/cow/scheduler/recipients.json"
    with open(rec_file, encoding="utf-8") as fh:
        recs = json.load(fh)["recipients"]
    key = next(iter(recs))
    rec = recs[key]
    print(f"  [info] 使用收件人: {key}")

    # 构造入站 context（形状与真实一致）
    class FakeTool:
        def _get_receiver_name(self, ctx):
            return ctx.get("receiver")

    context = {
        "receiver": rec["receiver"],
        "isgroup": bool(rec.get("is_group", False)),
        "session_id": rec.get("session_id", rec["receiver"]),
        "instance_id": rec.get("instance_id", ""),
    }
    channel_type = rec["channel_type"]
    print(f"  [info] context={context}  channel={channel_type}")

    integ._ensure_push_task(store, channel_type, context, FakeTool())

    # ---------- 第 3 步：确认任务已建 ----------
    print("")
    print("=== 3. 任务是否已创建 ===")
    tasks_after = store.load_tasks()
    print(f"  [info] 任务数: {len(tasks_after)}")

    push_tasks = {k: v for k, v in tasks_after.items()
                  if k.startswith(integ._PUSH_TASK_PREFIX)}
    for tid, t in push_tasks.items():
        print(f"        {tid}")
        print(f"          tool_name   = {t.get('tool_name')}")
        print(f"          next_run_at = {t.get('next_run_at')}")
        print(f"          interval    = {t.get('interval') or t.get('schedule')}")
        print(f"          action      = {json.dumps(t.get('action'), ensure_ascii=False)[:300]}")
        print(f"          channel     = {t.get('channel_type')}")
        print(f"          receiver    = {t.get('receiver')}")

    chk(len(push_tasks) >= 1, f"推送任务已创建（{len(push_tasks)} 个）")

    if push_tasks:
        t = next(iter(push_tasks.values()))
        act = t.get("action") or {}
        # 实际结构：action = {type: tool_call, call_name: ..., call_params: {...}, suppress: {...}}
        chk(act.get("type") == "tool_call",
            f"action.type == tool_call（实际 {act.get('type')}）")
        chk(act.get("call_name") == "dsa_push_digest",
            f"action.call_name == dsa_push_digest（实际 {act.get('call_name')}）")

        params = act.get("call_params") or {}
        chk(bool(params.get("wechat_id")),
            f"call_params 带 wechat_id（实际 {params.get('wechat_id')}）")
        chk(params.get("wechat_id") == rec["receiver"],
            "wechat_id 与收件人一致")

        chk(isinstance(act.get("suppress"), dict),
            f"action 含 suppress 规则（实际 {act.get('suppress')}）")
        sup = act.get("suppress") or {}
        chk(sup.get("json_field") == "skip",
            f"suppress.json_field == skip（实际 {sup.get('json_field')}）")

        chk(act.get("channel_type") == channel_type,
            f"action.channel_type == {channel_type}（实际 {act.get('channel_type')}）")
        chk(bool(act.get("receiver")), "action 带 receiver")

        # 幂等：再调一次不应重复建
        n_before = len(store.load_tasks())
        integ._ensure_push_task(store, channel_type, context, FakeTool())
        n_after = len(store.load_tasks())
        chk(n_before == n_after, f"幂等：重复调用不新增任务（{n_before} -> {n_after}）")

    # ---------- 第 4 步：到点是否真跑 ----------
    print("")
    print("=== 4. 调度拾取条件 ===")
    if push_tasks:
        t = next(iter(push_tasks.values()))
        iv = t.get("interval")
        chk(isinstance(iv, dict) and iv.get("seconds") == 900,
            f"interval == 900s（实际 {iv}）")
        chk("next_run_at" in t or t.get("enabled") is not False,
            "任务具备被调度器拾取的条件")
        print(f"  [info] 完整任务定义:")
        print(json.dumps(t, ensure_ascii=False, indent=2)[:1200])

    print("")
    print("结果：" + ("全部通过" if not FAILS else f"存在 {len(FAILS)} 个失败项"))
    for m in FAILS:
        print("   -", m)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
