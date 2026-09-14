#!/usr/bin/env python3
"""端到端验证：推送任务的「到点轮询」实跑证据（只读）。

前三支脚本是**分环节**验证（端点谁都能调、抑制逻辑单独喂输入、
任务定义单独断言）。本脚本回答另一个问题：

    把任务放在那儿让它自己跑，**调度器真的会派发吗**？

这是唯一不需要人为干预的观察，也是「整条链路连通」的决定性证据 ——
分环节全绿但调度器没接上，是完全可能的失败形态。

判据
----
对每一次 tick，日志里应依次出现四行（模块:行号是判据的一部分）：

1. ``scheduler_service.py:114`` —— 间隔调度器醒来派发
2. ``integration.py:1138``      —— 走 ``tool_call`` 分支，工具名对得上
3. ``mcp_client.py:325``        —— 真打到 DSA ``/push/digest`` 且 200
4. ``integration.py:1150``      —— 拿到 ``{"skip": true}`` → 判空 → 不投递

第 4 行是**预期行为**：一天里绝大多数轮询都不该发消息。
若当前时刻不在 ``PUSH_TIMES``（默认 ``["09:35","15:30"]``）内，
**不投递才是对的**；这里出现了投递，反而是 bug。

用法（在 cowagent 所在的宿主机上）::

    python3 e2e_push_tick_evidence.py          # 看最近 40 分钟
    python3 e2e_push_tick_evidence.py 720      # 看最近 12 小时

设计说明
--------
- 只看日志与 ``tasks.json``，不改任何状态。
- ``next_run_at - last_run_at`` 应等于 ``schedule.seconds``（900）。
  若不等，说明调度器没有按间隔推进，那才是真问题。

参见 ``docs/2026-09-14-push-feature-deploy.md`` §5.6。
"""
import json
import subprocess
import sys

TASKS = "/root/cowagent/cow/scheduler/tasks.json"


def sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=90)
    return r.stdout.strip()


def main():
    minutes = sys.argv[1] if len(sys.argv) > 1 else "40"

    print("服务器时间:", sh("date +%H:%M:%S"))
    print(f"\n--- 调度日志（最近 {minutes} 分钟）---")
    pattern = "Executing task: dsa-push|suppressed|dsa_push_digest"
    print(sh(
        f"docker logs cowagent --since {minutes}m 2>&1 "
        f"| grep -E '{pattern}' | tail -20"
    ))

    print("\n--- 任务状态 ---")
    with open(TASKS) as f:
        data = json.load(f)
    tasks = [t for t in data["tasks"].values() if "push" in t.get("name", "").lower()
             or "push" in t["action"].get("call_name", "")]
    if not tasks:
        print("FAIL 没有找到推送任务")
        return 1

    ok = True
    for task in tasks:
        print("id        =", task["id"])
        print("created_at=", task["created_at"])
        print("last_run  =", task.get("last_run_at"))
        print("next_run  =", task["next_run_at"])
        print("interval  =", task["schedule"])
        print("tool      =", task["action"]["call_name"])
        print("suppress  =", task["action"].get("suppress"))

        if not task.get("last_run_at"):
            print("  → 尚未跑过第一轮")
            continue

        # 间隔应等于 schedule.seconds
        from datetime import datetime
        fmt = "%Y-%m-%dT%H:%M:%S.%f"
        last = datetime.strptime(task["last_run_at"], fmt)
        nxt = datetime.strptime(task["next_run_at"], fmt)
        delta = int((nxt - last).total_seconds())
        want = int(task["schedule"]["seconds"])
        status = "PASS" if delta == want else "FAIL"
        print(f"  → 间隔 {delta}s（期望 {want}s）{status}")
        if delta != want:
            ok = False

        if task.get("last_run_at") > task["created_at"]:
            print("  → last_run_at 已晚于 created_at，说明调度器真派发过 PASS")
        else:
            print("  → 还没派发过")

    print("\n结果:", "全部通过" if ok else "存在失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
