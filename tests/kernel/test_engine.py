"""Turn Engine 的行为测试（`D09` 验收）。

九组，对应技术方案 §6.2 与 `D09` 的验收项：

| 组 | 验收内容 |
| --- | --- |
| A 事件流不变量 | 恰好一个终态、终态在末尾、终态之后无事件 |
| B 迭代与终止 | 无工具即完成、多轮迭代、上限终止（`KER-005`） |
| C 工具阶段 | 未知工具、Hook 拦截、截断、并发批真的重叠、消息顺序 |
| D 取消 | 4 个检查点各自的中断后语义（§6.4） |
| E 预算 | 五项越界的终态与可诊断说明 |
| F Hook | 4 个 turn 内 Hook 的分发、`REPLACE` 生效、观察者返回值被忽略 |
| G 异常不逸出 | 每个 `deps` 回调抛异常都转成终态事件（不变量 2） |
| H 流式 | 增量事件、折叠结果进下一轮、供应商错误 |
| I 结构守卫 | `engine.py` 的 import 白名单（不变量 1） |

夹具在 `_engine_support.py`；「为什么不用 `sdk/testing` 的 Fake」写在那里。
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
from dataclasses import replace

import pytest

from karyvia.contracts import (
    CancelReason,
    ChunkKind,
    Concurrency,
    ErrorCode,
    HookAction,
    HookContext,
    HookName,
    HookOutcome,
    KaryviaError,
    ModelChunk,
    Role,
    SideEffect,
    StopReason,
    ToolResult,
    TurnStatus,
)
from karyvia.kernel.turn import (
    ENGINE_HOOKS,
    BudgetLedger,
    CancelToken,
    Checkpoint,
    EngineDeps,
    LimitKind,
    ModelReasoningDelta,
    ModelResponseCompleted,
    ModelTextDelta,
    ToolCallCompleted,
    ToolCallStarted,
    ToolDisposition,
    TurnCancelled,
    TurnCompleted,
    TurnEvent,
    TurnFailed,
    TurnLimits,
    TurnStoppedByLimit,
    run_turn,
    terminal_from_error,
)

from ._engine_support import (
    CORRELATION,
    FakeClock,
    RecordingHookDispatcher,
    RecordingToolInvoker,
    ScriptedProvider,
    chunks_for,
    collect,
    make_request,
    ok_result,
    text_response,
    tool_call,
    tool_response,
    tool_spec,
    user_message,
)

ENGINE_PATH = pathlib.Path(__file__).resolve().parents[2] / "src/karyvia/kernel/turn/engine.py"


def build_deps(
    model: ScriptedProvider,
    *,
    tools: RecordingToolInvoker | None = None,
    hooks: RecordingHookDispatcher | None = None,
    limits: TurnLimits | None = None,
) -> EngineDeps:
    return EngineDeps(
        model=model,
        tools=tools or RecordingToolInvoker(),
        hooks=hooks or RecordingHookDispatcher(),
        limits=limits or TurnLimits(),
    )


def events_of(events: list[TurnEvent], kind: type) -> list[TurnEvent]:
    return [event for event in events if isinstance(event, kind)]


# ======================================================================================
# A. 事件流不变量
# ======================================================================================


async def test_simple_turn_completes_with_two_events() -> None:
    model = ScriptedProvider([text_response("你好")])
    events = await collect(run_turn(make_request(), build_deps(model), CancelToken()))
    assert [type(event) for event in events] == [ModelResponseCompleted, TurnCompleted]
    completed = events[-1]
    assert isinstance(completed, TurnCompleted)
    assert completed.response.content == "你好"
    assert completed.iterations == 1
    assert completed.tool_calls == 0


async def test_response_completed_carries_no_message_when_turn_ends() -> None:
    """本轮就此收尾时那条 assistant 消息不进下一轮，因此 `message` 是 `None`。"""
    model = ScriptedProvider([text_response()])
    events = await collect(run_turn(make_request(), build_deps(model), CancelToken()))
    first = events[0]
    assert isinstance(first, ModelResponseCompleted)
    assert first.message is None


async def test_every_terminal_maps_to_a_turn_status() -> None:
    """`D14` 要把终态事件翻成 `TurnStatus`；四个终态各自有唯一落点。"""
    assert {TurnStatus.COMPLETED, TurnStatus.FAILED, TurnStatus.CANCELLED} <= set(TurnStatus)
    assert TurnStatus.STOPPED_BY_LIMIT in set(TurnStatus)


# ======================================================================================
# B. 迭代与终止
# ======================================================================================


async def test_two_iterations_with_one_tool_call() -> None:
    model = ScriptedProvider([tool_response(tool_call("echo")), text_response("完成")])
    invoker = RecordingToolInvoker()
    deps = build_deps(model, tools=invoker)
    events = await collect(run_turn(make_request(tools=[tool_spec("echo")]), deps, CancelToken()))

    assert [type(event) for event in events] == [
        ModelResponseCompleted,
        ToolCallStarted,
        ToolCallCompleted,
        ModelResponseCompleted,
        TurnCompleted,
    ]
    terminal = events[-1]
    assert isinstance(terminal, TurnCompleted)
    assert (terminal.iterations, terminal.tool_calls) == (2, 1)
    assert len(invoker.invocations) == 1


async def test_tool_result_enters_next_request_in_call_order() -> None:
    """工具结果必须以 tool 消息回到下一轮请求，否则模型看不到自己调用的结果。"""
    calls = (tool_call("a"), tool_call("b"))
    model = ScriptedProvider([tool_response(*calls), text_response()])
    deps = build_deps(model, tools=RecordingToolInvoker(delays={"a": 3, "b": 1}))
    await collect(run_turn(make_request(tools=[tool_spec("a"), tool_spec("b")]), deps, CancelToken()))

    second = model.requests[1]
    assert [message.role for message in second.messages] == [
        Role.USER,
        Role.ASSISTANT,
        Role.TOOL,
        Role.TOOL,
    ]
    # 消息顺序恒等于 tool_calls 顺序，与完成顺序无关（a 比 b 慢，却仍排在前面）。
    assert [message.tool_call_id for message in second.messages[2:]] == ["call-a", "call-b"]


async def test_completion_order_may_differ_from_call_order() -> None:
    """并发批内 `ToolCallCompleted` 按完成顺序发出——它是给 UI 的实时反馈，不是模型契约。"""
    model = ScriptedProvider([tool_response(tool_call("slow"), tool_call("fast")), text_response()])
    deps = build_deps(model, tools=RecordingToolInvoker(delays={"slow": 5, "fast": 1}))
    events = await collect(
        run_turn(make_request(tools=[tool_spec("slow"), tool_spec("fast")]), deps, CancelToken())
    )
    completed = [event.call.name for event in events_of(events, ToolCallCompleted)]
    assert completed == ["fast", "slow"]
    started = [event.call.name for event in events_of(events, ToolCallStarted)]
    assert started == ["slow", "fast"], "开始事件按调用顺序整批发出"


async def test_unbounded_loop_stops_by_iteration_limit() -> None:
    """缺省配置下不存在无界执行路径（`KER-005`）：模型永远要工具，engine 仍会停。"""
    model = ScriptedProvider(default=tool_response(tool_call("echo")))
    deps = build_deps(model, limits=TurnLimits(max_iterations=3))
    events = await collect(run_turn(make_request(tools=[tool_spec("echo")]), deps, CancelToken()))

    terminal = events[-1]
    assert isinstance(terminal, TurnStoppedByLimit)
    assert terminal.breach.kind is LimitKind.MAX_ITERATIONS
    assert terminal.iterations == 3
    assert model.call_count == 3
    assert "3" in terminal.breach.describe()


async def test_stopped_by_limit_keeps_completed_tool_results() -> None:
    """撞上限不是失败：已经执行过的工具结果仍在事件流里，`D14` 据此持久化。"""
    model = ScriptedProvider(default=tool_response(tool_call("echo")))
    deps = build_deps(model, limits=TurnLimits(max_iterations=3))
    events = await collect(run_turn(make_request(tools=[tool_spec("echo")]), deps, CancelToken()))
    results = events_of(events, ToolCallCompleted)
    assert len(results) == 2
    assert all(event.result.side_effect is SideEffect.OCCURRED for event in results)


async def test_final_iteration_does_not_execute_tools_it_cannot_report() -> None:
    """最后一轮若还要工具，工具**不执行**——这是刻意的，不是差一错。

    结果无法再回给模型（没有下一轮了），执行它只会白白产生副作用；`limits.check` 的
    「发起前判定」正是为此。代价是 `max_iterations=N` 只能容纳 `N-1` 轮工具，
    上限的可诊断说明会明说停在第几轮。
    """
    model = ScriptedProvider(default=tool_response(tool_call("write")))
    invoker = RecordingToolInvoker()
    deps = build_deps(model, tools=invoker, limits=TurnLimits(max_iterations=1))
    events = await collect(run_turn(make_request(tools=[tool_spec("write")]), deps, CancelToken()))

    assert invoker.invocations == []
    assert events_of(events, ToolCallStarted) == []
    terminal = events[-1]
    assert isinstance(terminal, TurnStoppedByLimit)
    assert terminal.breach.kind is LimitKind.MAX_ITERATIONS


# ======================================================================================
# C. 工具阶段
# ======================================================================================


async def test_unknown_tool_becomes_error_result_not_turn_failure() -> None:
    """模型幻觉出一个工具名不该让整个 turn 失败：回一条错误 tool 消息，让它自己纠正。"""
    model = ScriptedProvider([tool_response(tool_call("ghost")), text_response("抱歉")])
    invoker = RecordingToolInvoker()
    deps = build_deps(model, tools=invoker)
    events = await collect(run_turn(make_request(tools=[tool_spec("echo")]), deps, CancelToken()))

    completed = events_of(events, ToolCallCompleted)[0]
    assert isinstance(completed, ToolCallCompleted)
    assert completed.disposition is ToolDisposition.UNKNOWN_TOOL
    assert completed.result.ok is False
    assert completed.result.side_effect is SideEffect.NONE
    assert invoker.invocations == [], "未知工具不得进入执行器"
    assert isinstance(events[-1], TurnCompleted)


async def test_blocked_tool_reports_hook_reason_and_skips_execution() -> None:
    hooks = RecordingHookDispatcher(
        {HookName.BEFORE_TOOL_CALL: HookOutcome(HookAction.BLOCK, reason="策略不允许写文件")}
    )
    model = ScriptedProvider([tool_response(tool_call("write")), text_response()])
    invoker = RecordingToolInvoker()
    deps = build_deps(model, tools=invoker, hooks=hooks)
    events = await collect(run_turn(make_request(tools=[tool_spec("write")]), deps, CancelToken()))

    completed = events_of(events, ToolCallCompleted)[0]
    assert isinstance(completed, ToolCallCompleted)
    assert completed.disposition is ToolDisposition.BLOCKED
    assert completed.result.side_effect is SideEffect.NONE
    assert "策略不允许写文件" in completed.result.content
    assert invoker.invocations == []


async def test_hook_can_replace_tool_invocation() -> None:
    """`before_tool_call` 的 `REPLACE` 必须真的改到发给执行器的那个 invocation。"""
    model = ScriptedProvider([tool_response(tool_call("echo", text="原始")), text_response()])
    invoker = RecordingToolInvoker()
    hooks = RecordingHookDispatcher()
    hooks.replace_tool_arguments = {"text": "改写后"}
    deps = build_deps(model, tools=invoker, hooks=hooks)
    await collect(run_turn(make_request(tools=[tool_spec("echo")]), deps, CancelToken()))

    assert invoker.invocations[0].call.arguments == {"text": "改写后"}


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("call.name", lambda item: replace(item, call=replace(item.call, name="other"))),
        ("call.call_id", lambda item: replace(item, call=replace(item.call, call_id="other"))),
        ("timeout_ms", lambda item: replace(item, timeout_ms=item.timeout_ms + 1)),
        ("idempotency_key", lambda item: replace(item, idempotency_key="other")),
    ],
)
async def test_tool_hook_cannot_replace_kernel_owned_invocation_fields(
    field: str, replacement: object
) -> None:
    def swap(context: HookContext) -> HookOutcome:
        assert context.invocation is not None
        return HookOutcome(action=HookAction.REPLACE, invocation=replacement(context.invocation))  # type: ignore[operator]

    hooks = RecordingHookDispatcher({HookName.BEFORE_TOOL_CALL: swap})
    model = ScriptedProvider([tool_response(tool_call("echo"))])
    events = await collect(
        run_turn(
            make_request(tools=[tool_spec("echo")]),
            build_deps(model, hooks=hooks),
            CancelToken(),
        )
    )
    terminal = events[-1]
    assert isinstance(terminal, TurnFailed)
    assert terminal.error.code is ErrorCode.KERNEL_INVARIANT_VIOLATED
    assert field in terminal.error.detail["fields"]


async def test_tool_result_is_truncated_by_limit() -> None:
    model = ScriptedProvider([tool_response(tool_call("dump")), text_response()])
    invoker = RecordingToolInvoker(results={"dump": ok_result("x" * 500)})
    deps = build_deps(model, tools=invoker, limits=TurnLimits(tool_result_max_bytes=32))
    events = await collect(run_turn(make_request(tools=[tool_spec("dump")]), deps, CancelToken()))

    completed = events_of(events, ToolCallCompleted)[0]
    assert isinstance(completed, ToolCallCompleted)
    assert completed.result.truncated is True
    # 预算作用在**结果正文**上。消息还要多一层不可信数据块（`D42`），
    # 那几行是常数开销，不受 `tool_result_max_bytes` 管——见 `fold_tool_result`。
    assert len(completed.result.content.encode()) == 32
    assert completed.result.content in completed.message.content


async def test_parallel_batch_really_overlaps() -> None:
    """用 `asyncio.Barrier` 而不是看耗时：串行执行会直接超时死锁，慢机器上也不会假阳性。"""
    barrier = asyncio.Barrier(2)

    async def rendezvous(name: str) -> ToolResult:
        async with asyncio.timeout(1.0):
            await barrier.wait()
        return ok_result(f"{name} 到位")

    model = ScriptedProvider([tool_response(tool_call("a"), tool_call("b")), text_response()])
    invoker = RecordingToolInvoker(handlers={"a": rendezvous, "b": rendezvous})
    deps = build_deps(model, tools=invoker)
    events = await collect(
        run_turn(make_request(tools=[tool_spec("a"), tool_spec("b")]), deps, CancelToken())
    )
    assert all(event.result.ok for event in events_of(events, ToolCallCompleted))
    assert isinstance(events[-1], TurnCompleted)


async def test_exclusive_tool_is_not_overlapped() -> None:
    """`EXCLUSIVE` 工具独占一批：两个都想会合就会撞上超时，证明它们没有重叠。"""
    barrier = asyncio.Barrier(2)

    async def rendezvous(name: str) -> ToolResult:
        try:
            async with asyncio.timeout(0.05):
                await barrier.wait()
        except TimeoutError:
            return ok_result("等不到同伴")
        return ok_result("重叠了")

    specs = [tool_spec(name, concurrency=Concurrency.EXCLUSIVE) for name in ("w1", "w2")]
    model = ScriptedProvider([tool_response(tool_call("w1"), tool_call("w2")), text_response()])
    invoker = RecordingToolInvoker(handlers={"w1": rendezvous, "w2": rendezvous})
    events = await collect(
        run_turn(make_request(tools=specs), build_deps(model, tools=invoker), CancelToken())
    )
    assert [event.result.content for event in events_of(events, ToolCallCompleted)] == [
        "等不到同伴",
        "等不到同伴",
    ]


# ======================================================================================
# D. 取消（技术方案 §6.4 的 4 个 engine 检查点）
# ======================================================================================


async def test_cancel_before_model_request_produces_no_content() -> None:
    """检查点 2：turn 未产生内容，模型一次都没被调用。"""
    cancel = CancelToken()
    cancel.request(CancelReason.USER)
    model = ScriptedProvider([text_response()])
    events = await collect(run_turn(make_request(), build_deps(model), cancel))

    assert len(events) == 1
    terminal = events[0]
    assert isinstance(terminal, TurnCancelled)
    assert terminal.reason is CancelReason.USER
    assert terminal.checkpoint is Checkpoint.BEFORE_MODEL_REQUEST
    assert terminal.iterations == 0, "没跑起来的一轮不该被计入迭代数"
    assert model.call_count == 0


async def test_cancel_between_stream_chunks_keeps_produced_text() -> None:
    """检查点 3：已产生的文本必须仍在事件流里（`KER-007`），不能被中断吞掉。"""
    cancel = CancelToken()
    chunks = [
        ModelChunk(kind=ChunkKind.TEXT, text="已经"),
        ModelChunk(kind=ChunkKind.TEXT, text="说出口的话"),
        ModelChunk(kind=ChunkKind.DONE, stop_reason=StopReason.END_TURN),
    ]
    model = ScriptedProvider(stream_chunks=chunks, cancel_after_chunk=(cancel, 2))
    events = await collect(run_turn(make_request(stream=True), build_deps(model), cancel))

    assert [event.text for event in events_of(events, ModelTextDelta)] == ["已经", "说出口的话"]
    terminal = events[-1]
    assert isinstance(terminal, TurnCancelled)
    assert terminal.checkpoint is Checkpoint.BETWEEN_STREAM_CHUNKS


async def test_cancel_before_tool_call_executes_nothing() -> None:
    """检查点 5：一个工具都没跑，因此没有任何副作用可言。"""
    cancel = CancelToken()
    model = ScriptedProvider([tool_response(tool_call("write"))], cancel_after_response=cancel)
    invoker = RecordingToolInvoker()
    deps = build_deps(model, tools=invoker)
    events = await collect(run_turn(make_request(tools=[tool_spec("write")]), deps, cancel))

    terminal = events[-1]
    assert isinstance(terminal, TurnCancelled)
    assert terminal.checkpoint is Checkpoint.BEFORE_TOOL_CALL
    assert invoker.invocations == []
    assert events_of(events, ToolCallCompleted) == []


async def test_cancel_after_tool_result_keeps_the_real_result() -> None:
    """检查点 6：已执行的工具保留真实结果，不得被改写成「已取消」。"""
    cancel = CancelToken()
    model = ScriptedProvider(default=tool_response(tool_call("echo")))
    invoker = RecordingToolInvoker(cancel_after_first=cancel)
    deps = build_deps(model, tools=invoker)
    events = await collect(run_turn(make_request(tools=[tool_spec("echo")]), deps, cancel))

    completed = events_of(events, ToolCallCompleted)[0]
    assert isinstance(completed, ToolCallCompleted)
    assert completed.result.ok is True
    assert completed.result.side_effect is SideEffect.OCCURRED
    assert completed.disposition is ToolDisposition.EXECUTED
    terminal = events[-1]
    assert isinstance(terminal, TurnCancelled)
    assert terminal.checkpoint is Checkpoint.AFTER_TOOL_RESULT


async def test_pending_calls_are_skipped_with_no_side_effect() -> None:
    """取消发生在批次之间：未轮到的调用记 `SKIPPED` + `side_effect=NONE`（§6.4 检查点 5）。

    「已执行但失败」与「根本没执行」在诊断上必须可区分——用户要判定的是副作用有没有发生。
    """
    cancel = CancelToken()
    specs = [tool_spec(name, concurrency=Concurrency.EXCLUSIVE) for name in ("first", "second")]
    model = ScriptedProvider(default=tool_response(tool_call("first"), tool_call("second")))
    invoker = RecordingToolInvoker(cancel_after_first=cancel)
    events = await collect(
        run_turn(make_request(tools=specs), build_deps(model, tools=invoker), cancel)
    )

    completed = events_of(events, ToolCallCompleted)
    assert [event.disposition for event in completed] == [
        ToolDisposition.EXECUTED,
        ToolDisposition.SKIPPED,
    ]
    assert completed[1].result.side_effect is SideEffect.NONE
    assert [name for name, _ in invoker.invocations_by_name] == ["first"]


async def test_skipped_tool_still_gets_a_message() -> None:
    """未执行也要有 tool 消息：`tool_calls` 悬空会让这段历史无法重放。"""
    cancel = CancelToken()
    specs = [tool_spec(name, concurrency=Concurrency.EXCLUSIVE) for name in ("first", "second")]
    model = ScriptedProvider(default=tool_response(tool_call("first"), tool_call("second")))
    invoker = RecordingToolInvoker(cancel_after_first=cancel)
    events = await collect(
        run_turn(make_request(tools=specs), build_deps(model, tools=invoker), cancel)
    )
    messages = [event.message for event in events_of(events, ToolCallCompleted)]
    assert [message.tool_call_id for message in messages] == ["call-first", "call-second"]
    assert all(message.content for message in messages)


async def test_cancel_reason_comes_from_the_token() -> None:
    """`TIMEOUT` 与 `BUDGET` 共用一个错误码，令牌才是取消原因的权威来源。"""
    cancel = CancelToken()
    cancel.request(CancelReason.SHUTDOWN)
    events = await collect(
        run_turn(make_request(), build_deps(ScriptedProvider([text_response()])), cancel)
    )
    terminal = events[-1]
    assert isinstance(terminal, TurnCancelled)
    assert terminal.reason is CancelReason.SHUTDOWN


async def test_tools_receive_a_child_token() -> None:
    """工具拿到的是子令牌：它能观察取消，但无权取消整个 turn（`D08` 的两个面）。"""
    cancel = CancelToken()
    model = ScriptedProvider([tool_response(tool_call("echo")), text_response()])
    invoker = RecordingToolInvoker()
    deps = build_deps(model, tools=invoker)
    await collect(run_turn(make_request(tools=[tool_spec("echo")]), deps, cancel))

    child = invoker.cancels[0]
    assert isinstance(child, CancelToken)
    assert child is not cancel
    child.request(CancelReason.USER)
    assert cancel.requested is False, "子令牌取消不得反向传播到 turn"


# ======================================================================================
# E. 预算
# ======================================================================================


async def test_tool_call_budget_stops_the_turn_before_the_batch_runs() -> None:
    model = ScriptedProvider(default=tool_response(tool_call("a"), tool_call("b")))
    invoker = RecordingToolInvoker()
    deps = build_deps(
        model, tools=invoker, limits=TurnLimits(max_iterations=10, max_tool_calls_per_turn=3)
    )
    events = await collect(
        run_turn(make_request(tools=[tool_spec("a"), tool_spec("b")]), deps, CancelToken())
    )

    terminal = events[-1]
    assert isinstance(terminal, TurnStoppedByLimit)
    assert terminal.breach.kind is LimitKind.MAX_TOOL_CALLS_PER_TURN
    # 第二批会让总数到 4 > 3，于是整批不发起：上限判定在发起**前**，副作用不该白发生。
    assert terminal.tool_calls == 2
    assert len(invoker.invocations) == 2


async def test_turn_timeout_becomes_cancelled_not_stopped_by_limit() -> None:
    """`turn_timeout_ms` 是预算项，终态却是 `CANCELLED`——由 `LIMIT_OUTCOMES` 唯一决定。"""
    clock = FakeClock()

    def burn_time(_: HookContext) -> HookOutcome:
        clock.advance_ms(5_000)
        return HookOutcome(action=HookAction.CONTINUE)

    cancel = CancelToken()
    hooks = RecordingHookDispatcher({HookName.AFTER_MODEL_RESPONSE: burn_time})
    model = ScriptedProvider(default=tool_response(tool_call("echo")))
    ledger = BudgetLedger(TurnLimits(turn_timeout_ms=4_000), clock=clock)
    deps = build_deps(model, hooks=hooks)
    events = await collect(
        run_turn(make_request(tools=[tool_spec("echo")]), deps, cancel, ledger=ledger)
    )

    terminal = events[-1]
    assert isinstance(terminal, TurnCancelled)
    assert terminal.reason is CancelReason.TIMEOUT
    # 超时必须落到令牌上，否则在途的子任务收不到「该停了」这个信号。
    assert cancel.reason is CancelReason.TIMEOUT


async def test_tool_timeout_is_clamped_to_remaining_turn_time() -> None:
    """一个 120 秒的工具不该把只剩 3 秒的 turn 拖成 123 秒。"""
    clock = FakeClock()
    model = ScriptedProvider([tool_response(tool_call("echo")), text_response()])
    invoker = RecordingToolInvoker()
    limits = TurnLimits(tool_timeout_ms=120_000, turn_timeout_ms=10_000)
    ledger = BudgetLedger(limits, clock=clock)
    clock.advance_ms(7_000)
    deps = build_deps(model, tools=invoker, limits=limits)
    await collect(
        run_turn(make_request(tools=[tool_spec("echo")]), deps, CancelToken(), ledger=ledger)
    )
    assert invoker.invocations[0].timeout_ms == 3_000


async def test_model_request_timeout_is_positive_and_bounded() -> None:
    """`ToolInvocation` 不接受 0（那是「永不超时」），模型请求这边也一律给正数。"""
    model = ScriptedProvider([text_response()])
    deps = build_deps(model, limits=TurnLimits(turn_timeout_ms=5_000))
    await collect(run_turn(make_request(), deps, CancelToken()))
    assert 0 < model.requests[0].timeout_ms <= 5_000


async def test_shared_ledger_accumulates_across_two_runs() -> None:
    """`D14` 的续写要多次调用 `run_turn`：共享一本账，迭代数才不会分裂。"""
    ledger = BudgetLedger(TurnLimits(max_iterations=4))
    for _ in range(2):
        model = ScriptedProvider([text_response()])
        await collect(run_turn(make_request(), build_deps(model), CancelToken(), ledger=ledger))
    assert ledger.iterations == 2


async def test_exhausted_ledger_stops_before_calling_the_model() -> None:
    """迭代前那道预算判定的意义：账本进来时就已经用完，模型一次都不该被调用。

    这正是 `D14` 续写的失败路径——前几次 `run_turn` 已经把 `max_iterations` 花光，
    下一次调用必须立刻以 `STOPPED_BY_LIMIT` 收场，而不是再问一次模型。
    """
    ledger = BudgetLedger(TurnLimits(max_iterations=1))
    ledger.begin_iteration()
    model = ScriptedProvider([text_response()])
    events = await collect(
        run_turn(make_request(), build_deps(model), CancelToken(), ledger=ledger)
    )

    assert model.call_count == 0
    terminal = events[-1]
    assert isinstance(terminal, TurnStoppedByLimit)
    assert terminal.breach.kind is LimitKind.MAX_ITERATIONS


# ======================================================================================
# F0. 异常到终态的翻译表（`terminal_from_error`，engine 只有一处 except 就靠它）
# ======================================================================================


def test_terminal_from_error_without_checkpoint() -> None:
    """`raise_if_requested()`（而非 `checkpoint()`）抛出的取消没有检查点，不许乱猜一个。"""
    token = CancelToken()
    token.request(CancelReason.USER)
    error = pytest.raises(KaryviaError, token.raise_if_requested).value
    terminal = terminal_from_error(error, reason=token.reason, iterations=2, tool_calls=1)

    assert isinstance(terminal, TurnCancelled)
    assert terminal.checkpoint is None
    assert (terminal.iterations, terminal.tool_calls) == (2, 1)


def test_terminal_from_error_falls_back_to_the_code_when_reason_is_unknown() -> None:
    """能力实现自己抛的取消错误没有令牌可查，只能按错误码反查。"""
    error = KaryviaError(ErrorCode.CANCELLED_BY_SHUTDOWN, "实例要关了")
    terminal = terminal_from_error(error, reason=None, iterations=0, tool_calls=0)
    assert isinstance(terminal, TurnCancelled)
    assert terminal.reason is CancelReason.SHUTDOWN


# ======================================================================================
# F. Hook（engine 只分发 4 个）
# ======================================================================================


async def test_engine_dispatches_exactly_its_four_hooks() -> None:
    hooks = RecordingHookDispatcher()
    model = ScriptedProvider([tool_response(tool_call("echo")), text_response()])
    deps = build_deps(model, hooks=hooks)
    await collect(run_turn(make_request(tools=[tool_spec("echo")]), deps, CancelToken()))

    assert set(hooks.hooks_seen) == ENGINE_HOOKS
    # turn_start / context_assemble / turn_end 属于 orchestrator：engine 拿不出它们的必填槽。
    assert HookName.TURN_START not in hooks.hooks_seen
    assert HookName.TURN_END not in hooks.hooks_seen


async def test_before_model_request_fires_once_per_iteration() -> None:
    """§10.2 把它画在进 engine 之前，实际由 engine 每轮分发；`D14` 不得再分发一次。"""
    hooks = RecordingHookDispatcher()
    model = ScriptedProvider(
        [tool_response(tool_call("echo")), tool_response(tool_call("echo")), text_response()]
    )
    deps = build_deps(model, hooks=hooks)
    await collect(run_turn(make_request(tools=[tool_spec("echo")]), deps, CancelToken()))
    assert hooks.count(HookName.BEFORE_MODEL_REQUEST) == 3
    assert model.call_count == 3


async def test_hook_contexts_carry_the_turn_correlation() -> None:
    """`KER-010`：关联标识贯穿模型调用与工具调用。"""
    hooks = RecordingHookDispatcher()
    model = ScriptedProvider([tool_response(tool_call("echo")), text_response()])
    deps = build_deps(model, hooks=hooks)
    await collect(run_turn(make_request(tools=[tool_spec("echo")]), deps, CancelToken()))
    assert all(context.correlation is CORRELATION for context in hooks.contexts)


async def test_hook_can_replace_the_model_request() -> None:
    replacement = make_request(messages=[user_message("被插件改写过")])

    def swap(_: HookContext) -> HookOutcome:
        return HookOutcome(action=HookAction.REPLACE, request=replacement)

    hooks = RecordingHookDispatcher({HookName.BEFORE_MODEL_REQUEST: swap})
    model = ScriptedProvider([text_response()])
    await collect(run_turn(make_request(), build_deps(model, hooks=hooks), CancelToken()))
    assert model.requests[0].messages[0].content == "被插件改写过"


async def test_model_request_replacement_controls_the_executable_tool_set() -> None:
    replacement = make_request(tools=[])

    def swap(_: HookContext) -> HookOutcome:
        return HookOutcome(action=HookAction.REPLACE, request=replacement)

    hooks = RecordingHookDispatcher({HookName.BEFORE_MODEL_REQUEST: swap})
    model = ScriptedProvider([tool_response(tool_call("echo")), text_response()])
    invoker = RecordingToolInvoker()
    events = await collect(
        run_turn(
            make_request(tools=[tool_spec("echo")]),
            build_deps(model, tools=invoker, hooks=hooks),
            CancelToken(),
        )
    )
    completed = events_of(events, ToolCallCompleted)[0]
    assert isinstance(completed, ToolCallCompleted)
    assert completed.disposition is ToolDisposition.UNKNOWN_TOOL
    assert invoker.invocations == []


async def test_model_request_replacement_can_add_an_executable_tool() -> None:
    replacement = make_request(tools=[tool_spec("echo")])

    def swap(_: HookContext) -> HookOutcome:
        return HookOutcome(action=HookAction.REPLACE, request=replacement)

    hooks = RecordingHookDispatcher({HookName.BEFORE_MODEL_REQUEST: swap})
    model = ScriptedProvider([tool_response(tool_call("echo")), text_response()])
    invoker = RecordingToolInvoker()
    await collect(
        run_turn(make_request(), build_deps(model, tools=invoker, hooks=hooks), CancelToken())
    )
    assert [item.call.name for item in invoker.invocations] == ["echo"]


async def test_model_request_replacement_cannot_change_correlation() -> None:
    replacement = replace(
        make_request(),
        correlation=replace(CORRELATION, turn_id=type(CORRELATION.turn_id)("other")),
    )

    def swap(_: HookContext) -> HookOutcome:
        return HookOutcome(action=HookAction.REPLACE, request=replacement)

    hooks = RecordingHookDispatcher({HookName.BEFORE_MODEL_REQUEST: swap})
    events = await collect(
        run_turn(make_request(), build_deps(ScriptedProvider([text_response()]), hooks=hooks), CancelToken())
    )
    terminal = events[-1]
    assert isinstance(terminal, TurnFailed)
    assert terminal.error.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


async def test_model_request_replacement_cannot_expand_the_timeout() -> None:
    replacement = make_request(timeout_ms=999_999)

    def swap(_: HookContext) -> HookOutcome:
        return HookOutcome(action=HookAction.REPLACE, request=replacement)

    hooks = RecordingHookDispatcher({HookName.BEFORE_MODEL_REQUEST: swap})
    model = ScriptedProvider([text_response()])
    await collect(
        run_turn(
            make_request(timeout_ms=25),
            build_deps(model, hooks=hooks, limits=TurnLimits(turn_timeout_ms=25)),
            CancelToken(),
        )
    )
    assert 0 < model.requests[0].timeout_ms <= 25


async def test_after_tool_call_replacement_is_still_truncated() -> None:
    """`after_tool_call` 的 `REPLACE` 发生在截断**之前**，否则插件能绕过预算。"""

    def swap(_: HookContext) -> HookOutcome:
        return HookOutcome(action=HookAction.REPLACE, result=ok_result("y" * 100))

    hooks = RecordingHookDispatcher({HookName.AFTER_TOOL_CALL: swap})
    model = ScriptedProvider([tool_response(tool_call("echo")), text_response()])
    deps = build_deps(model, hooks=hooks, limits=TurnLimits(tool_result_max_bytes=10))
    events = await collect(run_turn(make_request(tools=[tool_spec("echo")]), deps, CancelToken()))

    completed = events_of(events, ToolCallCompleted)[0]
    assert isinstance(completed, ToolCallCompleted)
    assert completed.result.truncated is True
    assert len(completed.result.content.encode()) == 10


async def test_observer_hook_cannot_change_the_response() -> None:
    """`after_model_response` 是观察者：契约层就没有 response 槽可换（§6.6）。"""
    hooks = RecordingHookDispatcher(
        {HookName.AFTER_MODEL_RESPONSE: HookOutcome(action=HookAction.CONTINUE)}
    )
    model = ScriptedProvider([text_response("原始答案")])
    events = await collect(run_turn(make_request(), build_deps(model, hooks=hooks), CancelToken()))
    terminal = events[-1]
    assert isinstance(terminal, TurnCompleted)
    assert terminal.response.content == "原始答案"


async def test_reject_on_a_hook_that_does_not_support_it_fails_the_turn() -> None:
    """静默忽略会让插件以为自己拒掉了 turn，而用户看到的是「什么都没发生」。"""
    hooks = RecordingHookDispatcher(
        {HookName.BEFORE_MODEL_REQUEST: HookOutcome(HookAction.REJECT, reason="不该在这里")}
    )
    model = ScriptedProvider([text_response()])
    events = await collect(run_turn(make_request(), build_deps(model, hooks=hooks), CancelToken()))

    terminal = events[-1]
    assert isinstance(terminal, TurnFailed)
    assert terminal.error.code is ErrorCode.KERNEL_INVARIANT_VIOLATED
    assert terminal.error.detail["hook"] == HookName.BEFORE_MODEL_REQUEST.value


# ======================================================================================
# G. 异常不逸出（不变量 2）
# ======================================================================================


async def test_model_complete_error_becomes_turn_failed() -> None:
    boom = KaryviaError(ErrorCode.EXTERNAL_MODEL_PROVIDER, "供应商 500")
    events = await collect(
        run_turn(make_request(), build_deps(ScriptedProvider([boom])), CancelToken())
    )
    terminal = events[-1]
    assert isinstance(terminal, TurnFailed)
    assert terminal.error is boom


async def test_model_stream_error_becomes_turn_failed() -> None:
    boom = KaryviaError(ErrorCode.EXTERNAL_MODEL_PROVIDER, "流断了")
    events = await collect(
        run_turn(make_request(stream=True), build_deps(ScriptedProvider([boom])), CancelToken())
    )
    assert isinstance(events[-1], TurnFailed)


async def test_bare_exception_is_wrapped_not_propagated() -> None:
    """能力实现抛裸异常本身就是契约违规；包起来才有码可查、才已脱敏。"""
    model = ScriptedProvider([RuntimeError("裸的")])
    events = await collect(run_turn(make_request(), build_deps(model), CancelToken()))
    terminal = events[-1]
    assert isinstance(terminal, TurnFailed)
    assert terminal.error.code is ErrorCode.KERNEL_UNEXPECTED
    assert terminal.error.detail["exception"] == "RuntimeError"


async def test_tool_prepare_error_becomes_turn_failed() -> None:
    """`prepare` 阶段还没碰到工具本体，失败就是 Kernel 侧的失败，不该折成工具结果。"""
    model = ScriptedProvider([tool_response(tool_call("echo")), text_response()])
    invoker = RecordingToolInvoker(prepare_error=RuntimeError("准备阶段炸了"))
    deps = build_deps(model, tools=invoker)
    events = await collect(run_turn(make_request(tools=[tool_spec("echo")]), deps, CancelToken()))
    assert isinstance(events[-1], TurnFailed)


async def test_hook_error_becomes_turn_failed() -> None:
    hooks = RecordingHookDispatcher(
        {HookName.BEFORE_MODEL_REQUEST: KaryviaError(ErrorCode.PLUGIN_HOOK_FAILED, "插件炸了")}
    )
    model = ScriptedProvider([text_response()])
    events = await collect(run_turn(make_request(), build_deps(model, hooks=hooks), CancelToken()))
    terminal = events[-1]
    assert isinstance(terminal, TurnFailed)
    assert terminal.error.code is ErrorCode.PLUGIN_HOOK_FAILED


async def test_tool_invoke_error_does_not_fail_the_turn() -> None:
    """**与开发方案验收表措辞不同的一处，刻意如此。**

    验收表写「每个 `deps` 回调注入异常都产出 `TurnFailed` 而非异常穿透」。`tools.invoke`
    这一条只满足前半句：异常不穿透，但折成 `ToolResult(ok=False, side_effect=UNKNOWN)`
    回给模型，turn 继续。理由有两条——`contracts/protocols.py` 已经写死「工具执行器逸出的
    异常由 engine 兜成 `UNKNOWN` 结果」；旧实现同样让模型自己纠错（`tests/baseline` 的
    `B2`）。一个工具炸了就打掉整轮对话是行为倒退。
    """
    model = ScriptedProvider([tool_response(tool_call("echo")), text_response("我换个办法")])
    invoker = RecordingToolInvoker(results={"echo": RuntimeError("工具炸了")})
    deps = build_deps(model, tools=invoker)
    events = await collect(run_turn(make_request(tools=[tool_spec("echo")]), deps, CancelToken()))

    completed = events_of(events, ToolCallCompleted)[0]
    assert isinstance(completed, ToolCallCompleted)
    assert completed.result.ok is False
    assert completed.result.side_effect is SideEffect.UNKNOWN
    assert completed.disposition is ToolDisposition.EXECUTED
    assert isinstance(events[-1], TurnCompleted)


async def test_cancelled_error_from_deps_propagates() -> None:
    """`asyncio.CancelledError` 是**任务本身**被杀，不是 turn 被取消——必须穿透。

    engine 捕 `Exception` 而不是 `BaseException` 就是为了这个：把它也吞掉等于把
    「进程正在退出」伪装成「这轮对话结束了」。
    """
    model = ScriptedProvider([asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await collect(run_turn(make_request(), build_deps(model), CancelToken()))


# ======================================================================================
# H. 流式
# ======================================================================================


async def test_stream_deltas_join_to_the_final_content() -> None:
    """`B3` 基线：增量按序到达，拼起来等于最终内容。"""
    chunks = chunks_for(text_response("你好，世界！"), text_pieces=["你好", "，", "世界！"])
    model = ScriptedProvider([chunks])
    events = await collect(run_turn(make_request(stream=True), build_deps(model), CancelToken()))

    deltas = [event.text for event in events_of(events, ModelTextDelta)]
    assert deltas == ["你好", "，", "世界！"]
    terminal = events[-1]
    assert isinstance(terminal, TurnCompleted)
    assert "".join(deltas) == terminal.response.content


async def test_reasoning_deltas_are_separate_from_content() -> None:
    chunks = [
        ModelChunk(kind=ChunkKind.REASONING, text="先想想"),
        ModelChunk(kind=ChunkKind.TEXT, text="答案"),
        ModelChunk(kind=ChunkKind.DONE, stop_reason=StopReason.END_TURN),
    ]
    model = ScriptedProvider([chunks])
    events = await collect(run_turn(make_request(stream=True), build_deps(model), CancelToken()))

    assert [event.text for event in events_of(events, ModelReasoningDelta)] == ["先想想"]
    terminal = events[-1]
    assert isinstance(terminal, TurnCompleted)
    assert terminal.response.content == "答案", "推理不进答案"


async def test_streamed_tool_calls_drive_the_tool_phase() -> None:
    """流式与非流式在工具阶段之后必须完全同构。"""
    model = ScriptedProvider(
        [chunks_for(tool_response(tool_call("echo"))), chunks_for(text_response("好了"))]
    )
    deps = build_deps(model)
    events = await collect(
        run_turn(make_request(tools=[tool_spec("echo")], stream=True), deps, CancelToken())
    )
    assert len(events_of(events, ToolCallCompleted)) == 1
    assert isinstance(events[-1], TurnCompleted)


async def test_provider_error_chunk_fails_the_turn_but_keeps_deltas() -> None:
    """`DONE(ERROR)` 不得被折成一个「看起来正常」的响应（`EDG-304`）。"""
    chunks = [
        ModelChunk(kind=ChunkKind.TEXT, text="半句"),
        ModelChunk(kind=ChunkKind.DONE, stop_reason=StopReason.ERROR),
    ]
    model = ScriptedProvider([chunks])
    events = await collect(run_turn(make_request(stream=True), build_deps(model), CancelToken()))

    assert [event.text for event in events_of(events, ModelTextDelta)] == ["半句"]
    terminal = events[-1]
    assert isinstance(terminal, TurnFailed)
    assert terminal.error.code is ErrorCode.EXTERNAL_MODEL_PROVIDER


async def test_max_tokens_turn_is_marked_truncated() -> None:
    """续写上限耗尽后，engine 才把 `TurnCompleted.truncated` 置为 `True`（`EDG-304`）。"""
    from karyvia.contracts import ModelResponse

    response = ModelResponse(
        model_id="fake-model", stop_reason=StopReason.MAX_TOKENS, content="被截断的答案"
    )
    model = ScriptedProvider([], default=response)
    events = await collect(run_turn(make_request(), build_deps(model), CancelToken()))

    terminal = events[-1]
    assert isinstance(terminal, TurnCompleted)
    assert terminal.truncated is True
    assert terminal.response.content == "被截断的答案"
    assert model.call_count == 4


async def test_max_tokens_response_is_continued_with_previous_assistant_message() -> None:
    """`MAX_TOKENS` 续写共享 ledger，并把每一段 assistant 消息带回下一次请求。"""
    from karyvia.contracts import ModelResponse

    model = ScriptedProvider(
        [
            ModelResponse(
                model_id="fake-model", stop_reason=StopReason.MAX_TOKENS, content="第一段"
            ),
            text_response("第二段"),
        ]
    )
    events = await collect(run_turn(make_request(), build_deps(model), CancelToken()))

    assert [type(event) for event in events] == [
        ModelResponseCompleted,
        ModelResponseCompleted,
        TurnCompleted,
    ]
    first, second = model.requests
    assert [message.role for message in second.messages] == [Role.USER, Role.ASSISTANT]
    assert second.messages[-1].content == "第一段"
    terminal = events[-1]
    assert isinstance(terminal, TurnCompleted)
    assert terminal.truncated is False
    assert terminal.iterations == 2


async def test_max_tokens_continuation_does_not_repeat_tool_calls() -> None:
    """续写发生在工具循环之后时，不会从头执行已经完成的工具。"""
    from karyvia.contracts import ModelResponse

    model = ScriptedProvider(
        [
            tool_response(tool_call("echo")),
            ModelResponse(
                model_id="fake-model", stop_reason=StopReason.MAX_TOKENS, content="半截"
            ),
            text_response("收尾"),
        ]
    )
    invoker = RecordingToolInvoker()
    events = await collect(
        run_turn(make_request(tools=[tool_spec("echo")]), build_deps(model, tools=invoker), CancelToken())
    )

    assert len(invoker.invocations) == 1
    assert len(model.requests) == 3
    assert [message.role for message in model.requests[2].messages] == [
        Role.USER,
        Role.ASSISTANT,
        Role.TOOL,
        Role.ASSISTANT,
    ]
    assert isinstance(events[-1], TurnCompleted)


async def test_end_turn_is_not_marked_truncated() -> None:
    """`END_TURN` 是正常结束，`truncated` 必须为 `False`。"""
    model = ScriptedProvider([text_response("完整答案")])
    events = await collect(run_turn(make_request(), build_deps(model), CancelToken()))

    terminal = events[-1]
    assert isinstance(terminal, TurnCompleted)
    assert terminal.truncated is False


# ======================================================================================
# I. 结构守卫（不变量 1：只通过 deps 与外界交互）
# ======================================================================================

#: `engine.py` 允许出现的模块级 import 根名。新增一项等于给 engine 开一条新的外部通道，
#: 必须走评审——这正是这条守卫存在的意义。`asyncio` 不在此列：并发调度在 `scheduling.py`。
ALLOWED_ENGINE_IMPORTS = frozenset(
    {"__future__", "collections", "dataclasses", "karyvia", "typing"}
)

#: 只要出现就说明 engine 在自己做 IO。
FORBIDDEN_ROOTS = frozenset({"os", "pathlib", "socket", "shutil", "subprocess", "sqlite3", "http"})


def module_import_roots(source: str) -> set[str]:
    """取一个模块的 import 根名。相对 import 记为 `.`。"""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            roots.add("." if node.level else (node.module or "").split(".")[0])
    return roots


def test_engine_imports_nothing_that_touches_the_outside_world() -> None:
    roots = module_import_roots(ENGINE_PATH.read_text(encoding="utf-8"))
    assert roots <= ALLOWED_ENGINE_IMPORTS | {"."}, f"engine.py 出现未登记的 import：{roots}"
    assert not roots & FORBIDDEN_ROOTS


def test_the_import_guard_actually_catches_a_violation() -> None:
    """守卫自证：注入一个会读文件的 engine，检查必须失败（照抄 `D01` 的注入范式）。"""
    roots = module_import_roots("import os\nfrom pathlib import Path\n")
    assert roots & FORBIDDEN_ROOTS
    assert not roots <= ALLOWED_ENGINE_IMPORTS


def test_engine_stays_within_its_line_budget() -> None:
    """§6.2 给 engine 的目标是 ≤400 行；`D01` 的守卫按 kernel 500 行卡，这里卡得更紧。"""
    lines = len(ENGINE_PATH.read_bytes().splitlines())
    assert lines <= 400, f"engine.py 已经 {lines} 行——超出的部分属于某个兄弟模块"
