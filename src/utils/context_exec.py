# -*- coding: utf-8 -*-
"""把 ``contextvars`` 上下文带进线程池。

问题
----
``concurrent.futures.ThreadPoolExecutor.submit`` **不会**复制调用方的
context。``contextvars`` 的语义是「每个线程各自持有一份」，因此 worker
线程里读到的永远是默认值，而不是提交方的值。

对本项目而言这是**有实际后果**的：多租户的当前用户身份存在 contextvar
里（``src.tenancy.context._current_principal``），所以裸 ``submit`` 会让
后台任务变成「没有身份」：

- 读：查不到自己刚提交的数据 —— 表现为「报告凭空消失」
- 写：归属被收窄到系统属主 —— 表现为「报告记到了管理员名下」

两种表现**都不报错**，因此极难发现。大盘复盘「在 CowAgent 里看不到」
就是这个缺陷的实例。

用法
----
::

    from src.utils.context_exec import submit_with_context

    future = submit_with_context(executor, self._execute_task, task_id, code)

同步路径（已经在线程里，只是想换一份干净的上下文副本）用
:func:`run_with_context`。

注意
----
复制的是**提交时刻的快照**，所以任务体内的身份等于触发者 —— 这正是
我们想要的语义。反过来说，**不要**在提交之后再去改上下文，那不会
影响已提交的任务。
"""

from __future__ import annotations

import contextvars
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, TypeVar

_T = TypeVar("_T")


def run_with_context(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """在**当前上下文的副本**里同步执行 ``fn``。"""
    return contextvars.copy_context().run(fn, *args, **kwargs)


class _ContextRunner:
    """在**提交时刻快照**的上下文里调用目标函数。

    为什么需要这一层：``executor.submit(ctx.run, fn, *args)`` 也能工作，
    但它把 ``submit`` 的位置参数**整体右移一位** —— ``args[0]`` 从 ``fn``
    变成 ``ctx.run``，``args[1]`` 从第一个业务参数变成 ``fn``。

    任何按位置解析提交参数的代码都会因此错位。实测踩到过：
    ``test_task_service`` 断言 ``executor.submit`` 的 ``args[1]`` 是股票
    代码，改动后它变成了 ``_run_analysis`` 方法本身。

    用一个可调用对象包一层，就能让 ``submit`` 的调用形态与裸提交完全
    一致：``submit(可调用对象, *业务参数)``。
    """

    __slots__ = ("_ctx", "_fn")

    def __init__(self, ctx: contextvars.Context, fn: Callable[..., Any]) -> None:
        self._ctx = ctx
        self._fn = fn

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._ctx.run(self._fn, *args, **kwargs)


def submit_with_context(
    executor: ThreadPoolExecutor,
    fn: Callable[..., _T],
    *args: Any,
    **kwargs: Any,
) -> Future:
    """提交任务到线程池，并携带当前 ``contextvars`` 上下文。

    与 ``executor.submit(fn, *args)`` 的唯一区别是执行时套了一层上下文
    副本，其余语义完全一致：返回值、异常传播、``future.cancel()``，
    以及**位置参数的布局**（``submit`` 收到的第一个参数仍然是「要调用
    的东西」，其后是业务参数）。
    """
    runner = _ContextRunner(contextvars.copy_context(), fn)
    return executor.submit(runner, *args, **kwargs)
