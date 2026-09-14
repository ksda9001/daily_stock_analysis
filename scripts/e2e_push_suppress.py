#!/usr/bin/env python3
"""端到端验证：CowAgent 侧行情推送的两处 override 插入。

1. **静态**：``_should_suppress_delivery`` / ``_ensure_push_task`` 存在，
   ``_PUSH_TOOL_NAME == "dsa_push_digest"``
2. **行为**：用假输入**实际调用** ``_should_suppress_delivery``，覆盖
   空内容 / skip=true / skip=false / 非 JSON / 无规则 等情形
3. **接线**：grep 调用点，确认不是死代码

用法（在 cowagent 容器内，注意降权用户）::

    docker exec -u agent -e HOME=/home/agent cowagent python3 /tmp/e2e_push_suppress.py

设计说明
--------
- **契约**：``_should_suppress_delivery(content, action)`` —— ``content`` 是
  **字符串**，``action`` 是 **dict**，抑制规则在 ``action["suppress"]``，
  形如 ``{"json_field": "skip", "equals": True}``。探针写错会得到
  ``AttributeError: 'str' object has no attribute 'get'``。
- 解析失败**一律不抑制** —— 宁可多发一条，也不要静默吞掉真正的内容。
- 只读，不改任何数据。

参见 ``docs/2026-09-14-push-feature-deploy.md`` §5.3。
"""
import inspect
import sys


def line(msg):
    print(msg, flush=True)


def main():
    ok = True

    def chk(cond, msg):
        nonlocal ok
        line(("  PASS  " if cond else "  FAIL  ") + msg)
        if not cond:
            ok = False

    sys.path.insert(0, "/app")
    from agent.tools.scheduler import integration as m

    # ---------- 1. 静态 ----------
    line("=== 1. 静态：补丁是否在 ===")
    chk(hasattr(m, "_should_suppress_delivery"), "_should_suppress_delivery 存在")
    chk(hasattr(m, "_ensure_push_task"), "_ensure_push_task 存在")
    tool_name = getattr(m, "_PUSH_TOOL_NAME", "MISSING")
    chk(tool_name == "dsa_push_digest", f"_PUSH_TOOL_NAME == dsa_push_digest（实际 {tool_name}）")

    # ---------- 2. 行为 ----------
    line("")
    line("=== 2. 行为：_should_suppress_delivery 实际调用 ===")
    f = m._should_suppress_delivery
    sig = inspect.signature(f)
    params = list(sig.parameters.keys())
    line(f"  [info] 签名: {sig}")
    line("  [info] 契约: content 是字符串, action 是 dict, 规则在 action['suppress']")

    # 真实调用点传的 action 形如 {"suppress": {"json_field": "skip", "equals": True}}
    RULE = {"suppress": {"json_field": "skip", "equals": True}}

    cases = [
        ("空字符串", "", RULE, True),
        ("None", None, RULE, True),
        ("空白串", "   ", RULE, True),
        ("skip=true JSON", '{"ok": true, "skip": true, "reason": "missed_window"}', RULE, True),
        ("skip=false JSON", '{"ok": true, "skip": false, "indices": [{"code": "sh000001"}]}', RULE, False),
        ("无 skip 字段的 JSON", '{"ok": true, "indices": []}', RULE, False),
        ("非 JSON（工具报错文本）", 'Error: something went wrong', RULE, False),
        ("JSON 数组（非对象）", '[1,2,3]', RULE, False),
        ("无 suppress 规则", '{"skip": true}', None, False),
        ("无 suppress 规则(空 dict)", '{"skip": true}', {}, False),
    ]

    for label, content, action, expect in cases:
        try:
            got = f(content, action)
            chk(got == expect, f"{label} -> {got}（期望 {expect}）")
        except Exception as e:
            chk(False, f"{label} -> 异常 {type(e).__name__}: {e}")

    # ---------- 3. 接线 ----------
    line("")
    line("=== 3. 接线：调用点检查 ===")
    import subprocess

    for fn in ("_should_suppress_delivery", "_ensure_push_task"):
        r = subprocess.run(
            f"grep -rn '{fn}' /app/agent /app/bridge /app/channel /app/common 2>/dev/null "
            f"| grep -v '\\.pyc'",
            shell=True, capture_output=True, text=True,
        )
        hits = [ln for ln in r.stdout.strip().split("\n") if ln.strip()]
        callers = [ln for ln in hits if "def " not in ln]
        line(f"  [info] {fn}: {len(hits)} 处引用，{len(callers)} 处非定义")
        for ln in hits[:8]:
            line(f"        {ln[:160]}")
        chk(len(callers) > 0, f"{fn} 存在调用点（不是死代码）")

    # bringup: 检查 _ensure_push_task 是否在 scheduler 注册任务的地方被调用
    line("")
    r = subprocess.run(
        "grep -rn 'push' /app/agent/tools/scheduler/ 2>/dev/null | grep -iE 'task|regist|schedul' | head -20",
        shell=True, capture_output=True, text=True,
    )
    line("  [info] scheduler 里与 push 相关的行:")
    for ln in r.stdout.strip().split("\n")[:15]:
        if ln.strip():
            line(f"        {ln[:160]}")

    line("")
    line("结果：" + ("全部通过" if ok else "存在失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
