"""工具调度：批次划分与批内并发执行（技术方案 §6.2，需求 `TOL-004`）。

职责：把一次响应的 `ToolCall` 序列按 `ToolSpec.concurrency` 切成批次，并按批执行，
按**完成顺序**产出 `(ToolCall, ToolResult)`。
不负责：构造 `ToolInvocation`、调用 Hook、截断结果、判预算、处理取消——全部在 `engine.py`
的 `run_one` 回调里；本模块不认识 `CancelToken`，也不含 IO。

**批次规则**（旧实现同一结论的新口径）：连续的 `Concurrency.PARALLEL` 调用合成一批并发执行；
`Concurrency.EXCLUSIVE` 与**未知工具**各自单独成批，充当屏障。因此工具之间的相对顺序永远被
保留，`[a(P), b(P), c(E), d(P)]` 得到 `[[a, b], [c], [d]]`。

⚠️ **与旧实现的口径差异**：旧实现按 `concurrency_safe`（≈ 只读且非独占）合批，新层按
`ToolSpec.concurrency is PARALLEL`——而 `concurrency` 的默认值是 `PARALLEL`、`risk` 的默认值是
`MUTATING`。**一个会写的工具如果忘了声明 `EXCLUSIVE`，新层会并发执行它，旧层不会。**
写内建工具与插件工具时，具有写入或进程副作用的一律要显式给出 `concurrency`。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import TypeVar

from karyvia.contracts import Concurrency, ToolCall, ToolSpec

__all__ = ["execute_batch", "partition_tool_batches"]

#: `run_one` 的返回类型。engine 传的不是裸 `ToolResult` 而是「结果 + 处置」的小记录——
#: 本模块不需要认识它，泛型让调度与载荷彻底解耦（`ToolResult` 仍是默认的心智模型）。
_Outcome = TypeVar("_Outcome")


def partition_tool_batches(
    calls: Sequence[ToolCall], specs: Mapping[str, ToolSpec]
) -> tuple[tuple[ToolCall, ...], ...]:
    """把调用序列切成批次。同步纯函数，可以直接摆一张声明式表来测。

    未知工具当屏障：既然不知道它的语义就不能假设可并发。代价是零——它根本不会执行，
    engine 会为它合成一条 `unknown_tool_result`。
    """
    batches: list[tuple[ToolCall, ...]] = []
    current: list[ToolCall] = []
    for call in calls:
        spec = specs.get(call.name)
        if spec is not None and spec.concurrency is Concurrency.PARALLEL:
            current.append(call)
            continue
        if current:
            batches.append(tuple(current))
            current = []
        batches.append((call,))
    if current:
        batches.append(tuple(current))
    return tuple(batches)


async def execute_batch(
    batch: Sequence[ToolCall],
    run_one: Callable[[ToolCall], Awaitable[_Outcome]],
) -> AsyncIterator[tuple[ToolCall, _Outcome]]:
    """执行一批调用，按**完成顺序**产出结果。

    完成顺序只决定事件顺序；进入下一轮请求的消息顺序由调用方按 `tool_calls` 重排，
    两者是两件事（前者是给 UI 的实时反馈，后者是模型的输入契约）。

    **不用 `asyncio.TaskGroup`**：它在任一子任务异常时取消兄弟任务，而工具的副作用此刻
    正在发生——把它们掐掉既救不回已经写出去的东西，又让副作用状态从「已发生」退化成「未知」。

    **异常约定**：`run_one` 按约定不抛（engine 的 `_invoke_one` 已经把逸出异常折成
    `ToolResult`）。真抛了会连带丢下同批未完成的任务，因此那条路径视为 Kernel 缺陷，
    不做兜底——兜底只会让缺陷更难被发现。
    """
    if len(batch) == 1:
        yield batch[0], await run_one(batch[0])
        return

    owners = {asyncio.ensure_future(run_one(call)): call for call in batch}
    pending = set(owners)
    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            yield owners[task], task.result()
