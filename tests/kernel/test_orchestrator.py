"""`TurnOrchestrator` 的行为测试（`D14` 验收：技术方案 §10.2 的 14 步、§10.3）。

| 组 | 验收内容 |
| --- | --- |
| A 事件序列 | 模型类 / 命令类 turn 各一条完整可追踪序列（`KER-010`、`OBS-002`） |
| B 准入 | 去重不产生第二次副作用（`EDG-201`）；队列满即拒（`EDG-202`）；MERGE 归一个 turn |
| C 分流 | 命令已处理不进模型、改写输入继续、被拒可诊断且会话仍可用（`CMD-003`） |
| D 终态 | 四个终态各自的事件、出站状态与 `TurnOutcome` |
| E 持久化 | 空 assistant / 孤儿 tool / 二次截断三条基线决定；写失败 → FAILED（`SES-003`） |
| F 取消 | 已产生内容留存并标 `interrupted`、未执行工具 `side_effect=NONE`、会话可继续 |
| G Hook | `turn_start` 拒绝、`before_model_request` 只分发一次/轮、观察者失败不影响 turn |
| H 预算 | `STOPPED_BY_LIMIT` 后发一次不带 tools 的收尾请求 |
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

from nucleamind.contracts import (
    AttachmentRef,
    AttachmentSource,
    CancelReason,
    CommandResult,
    CompactionResult,
    Concurrency,
    Correlation,
    Disposition,
    ErrorCode,
    EventName,
    HookAction,
    HookName,
    HookOutcome,
    NucleaError,
    Role,
    SessionKey,
    SessionMessage,
    SideEffect,
    StreamState,
    ToolResult,
    TrustLevel,
    TurnId,
    TurnStatus,
)
from nucleamind.contracts.message import MAX_ATTACHMENTS
from nucleamind.kernel.routing import ConcurrencyPolicy, SessionScheduler
from nucleamind.kernel.turn import CompactionPolicy, RetryPolicy, TurnLimits, TurnReceipt
from nucleamind.kernel.turn.transcript import Transcript, TurnState

from ._engine_support import (
    RecordingHookDispatcher,
    RecordingToolInvoker,
    ScriptedProvider,
    chunks_for,
    ok_result,
    text_response,
    tool_call,
    tool_response,
    tool_spec,
)
from ._orchestrator_support import (
    INSTANCE,
    FailingStore,
    FakeSessionStore,
    ScriptedCommand,
    binding,
    build,
    fragment,
    inbound,
    make_command,
)
from ._orchestrator_support import FakeContextProvider as Provider

NOW = datetime(2026, 8, 12, tzinfo=UTC)


def names(harness) -> list[str]:  # noqa: ANN001
    return [event.name.value for event in harness.events.events]


class ScriptedCompactor:
    def __init__(
        self,
        result: CompactionResult | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    async def compact(self, request, cancel):  # noqa: ANN001, ANN202
        del request, cancel
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def compaction_policy(compactor: ScriptedCompactor) -> CompactionPolicy:
    from nucleamind.contracts import Builtin

    return CompactionPolicy(compactor=compactor, name="summary", owner=Builtin())


def old_message(index: int, role: Role, content: str) -> SessionMessage:
    return SessionMessage(
        message_id=f"old-{index}",
        role=role,
        content=content,
        created_at=NOW,
    )


# ------------------------------------------------------------------ A 事件序列


async def test_a_model_turn_with_a_tool_call_emits_the_whole_sequence() -> None:
    harness = build(
        ScriptedProvider([tool_response(tool_call("fs.read")), text_response("一共 12 个")]),
        tool_specs=[tool_spec("fs.read")],
    )

    receipt = await harness.send()

    assert names(harness) == [
        "turn.started",
        "session.started",
        "model.request_started",
        "model.response_received",
        "tool.call_started",
        "tool.call_completed",
        "model.request_started",
        "model.response_received",
        "turn.completed",
    ]
    assert receipt.outcome is not None
    assert receipt.outcome.status is TurnStatus.COMPLETED
    assert receipt.content == "一共 12 个"


async def test_every_turn_event_carries_the_same_correlation() -> None:
    harness = build(ScriptedProvider([text_response("好")]))

    receipt = await harness.send()

    turn_ids = {
        event.correlation.turn_id
        for event in harness.events.events
        if event.correlation is not None
    }
    assert turn_ids == {receipt.turn_id}


async def test_a_command_turn_still_gets_a_full_event_stream() -> None:
    """`KER-010`：命令即使不进模型也发 turn 事件，否则事件流里会出现无头无尾的 turn。"""
    handler = ScriptedCommand(
        CommandResult(disposition=Disposition.COMMAND_HANDLED, content="可用命令：/help")
    )
    name, entry = make_command("help", handler)
    harness = build(ScriptedProvider([]), commands={name: entry})

    receipt = await harness.send(inbound("/help"))

    assert names(harness) == ["turn.started", "turn.completed"]
    assert receipt.content == "可用命令：/help"
    assert harness.provider.call_count == 0
    assert harness.store.appends == []  # 命令输出不进会话历史


async def test_streaming_deltas_become_outbound_messages() -> None:
    harness = build(
        ScriptedProvider(stream_chunks=chunks_for(text_response("答案"), text_pieces=["答", "案"])),
        stream=True,
    )

    await harness.send()

    assert [(m.stream_state, m.content) for m in harness.delivered] == [
        (StreamState.DELTA, "答"),
        (StreamState.DELTA, "案"),
        (StreamState.FINAL, "答案"),
    ]


# ------------------------------------------------------------------ B 准入


async def test_a_duplicate_message_never_runs_a_second_time() -> None:
    harness = build(ScriptedProvider([text_response("一"), text_response("二")]))
    message = inbound(message_id="same")

    first = await harness.send(message)
    second = await harness.send(message)

    assert second.admitted is False
    assert second.duplicate_of == first.turn_id
    assert harness.provider.call_count == 1  # `EDG-201`：不产生第二次副作用
    assert harness.events.of(EventName.TURN_REJECTED)[0].payload["reason"] == "duplicate"


async def test_a_full_queue_is_rejected_with_a_diagnosable_error() -> None:
    harness = build(ScriptedProvider([], default=text_response("好")))
    harness.deps.scheduler._policy = ConcurrencyPolicy.REJECT  # noqa: SLF001 - 直接换策略
    gate = asyncio.Event()

    async def slow(request, cancel):  # noqa: ANN001, ANN202
        await gate.wait()
        return text_response("好")

    harness.provider.complete = slow  # type: ignore[method-assign]

    first = asyncio.ensure_future(harness.send(inbound(message_id="m1")))
    await asyncio.sleep(0)
    second = await harness.send(inbound(message_id="m2"))
    gate.set()
    await first

    assert second.admitted is False
    assert second.error is not None
    assert second.error.code is ErrorCode.INPUT_SESSION_BUSY
    retry = await harness.send(inbound(message_id="m2"))
    assert retry.admitted is True


async def test_merged_messages_share_one_turn_and_are_recorded_in_the_payload() -> None:
    harness = build(ScriptedProvider([], default=text_response("好")))
    harness.deps.scheduler._policy = ConcurrencyPolicy.MERGE  # noqa: SLF001
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def slow(request, cancel):  # noqa: ANN001, ANN202
        entered.set()
        await gate.wait()
        return text_response("好")

    harness.provider.complete = slow  # type: ignore[method-assign]

    first = asyncio.ensure_future(harness.send(inbound("第一句", message_id="m1")))
    await entered.wait()
    second = asyncio.ensure_future(harness.send(inbound("第二句", message_id="m2")))
    third = asyncio.ensure_future(harness.send(inbound("第三句", message_id="m3")))
    await asyncio.sleep(0)
    gate.set()
    receipts = await asyncio.gather(first, second, third)

    started = harness.events.of(EventName.TURN_STARTED)
    # 一次执行 = 一条 turn 事件流，被吸收的消息只在载荷里留痕。
    assert len(started) == 2  # 第一条自己一批，后两条合并成第二批
    assert started[1].payload["merged_from"] == ["m3"]
    assert {receipt.turn_id for receipt in receipts[1:]} == {receipts[1].turn_id}
    duplicate = await harness.send(inbound("第三句", message_id="m3"))
    assert duplicate.duplicate_of == receipts[1].turn_id


# ------------------------------------------------------------------ C 分流


async def test_a_command_can_rewrite_the_input_and_the_turn_continues() -> None:
    handler = ScriptedCommand(
        CommandResult(
            disposition=Disposition.COMMAND_CONTINUE, rewritten_input="请用中文回答：你好"
        )
    )
    name, entry = make_command("zh", handler, takes_text=True)
    harness = build(ScriptedProvider([text_response("你好")]), commands={name: entry})

    await harness.send(inbound("/zh 你好"))

    assert harness.provider.requests[0].messages[-1].content == "请用中文回答：你好"


async def test_inbound_attachment_reaches_model_and_session() -> None:
    attachment = AttachmentRef(
        source=AttachmentSource.URL,
        locator="https://files.example/report.pdf",
        media_type="application/pdf",
        size_bytes=42,
        filename="report.pdf",
    )
    harness = build(ScriptedProvider([text_response("收到")]))

    await harness.send(inbound("请查看", attachments=(attachment,)))

    model_input = harness.provider.requests[0].messages[-1].content
    assert "请查看" in model_input
    assert '"locator": "https://files.example/report.pdf"' in model_input
    _, records = harness.store.appends[0]
    assert records[0].attachments == (attachment,)


async def test_command_fragments_reach_the_context() -> None:
    handler = ScriptedCommand(
        CommandResult(
            disposition=Disposition.COMMAND_CONTINUE,
            rewritten_input="写代码",
            fragments=(fragment("builtin:skill", content="技能提示", trust=TrustLevel.SYSTEM),),
        )
    )
    name, entry = make_command("skill", handler, takes_text=True)
    harness = build(ScriptedProvider([text_response("好")]), commands={name: entry})

    await harness.send(inbound("/skill 写代码"))

    messages = harness.provider.requests[0].messages
    assert messages[0].role is Role.SYSTEM
    assert messages[0].content == "技能提示"


async def test_a_rejected_command_keeps_the_session_usable() -> None:
    handler = ScriptedCommand(RuntimeError("命令炸了"))
    name, entry = make_command("boom", handler)
    harness = build(
        ScriptedProvider([text_response("后续正常")]), commands={name: entry}
    )

    rejected = await harness.send(inbound("/boom", message_id="m1"))
    followup = await harness.send(inbound("普通输入", message_id="m2"))

    assert rejected.outcome is not None
    assert rejected.outcome.status is TurnStatus.FAILED
    assert harness.events.of(EventName.TURN_REJECTED)[0].payload["reason"] == "command"
    assert followup.content == "后续正常"  # 会话仍然可用（`CMD-003`）


# ------------------------------------------------------------------ D 终态


async def test_a_failing_model_produces_a_failed_turn_and_a_failed_frame() -> None:
    harness = build(
        ScriptedProvider([NucleaError(ErrorCode.EXTERNAL_MODEL_PROVIDER, "供应商 500")])
    )

    receipt = await harness.send()

    assert receipt.outcome is not None
    assert receipt.outcome.status is TurnStatus.FAILED
    assert names(harness)[-1] == "turn.failed"
    assert harness.delivered[-1].stream_state is StreamState.FAILED
    assert harness.delivered[-1].is_complete_answer is False


async def test_hitting_the_iteration_limit_stops_the_turn_by_limit() -> None:
    harness = build(
        ScriptedProvider([], default=tool_response(tool_call("fs.read"))),
        tool_specs=[tool_spec("fs.read")],
        limits=TurnLimits(max_iterations=2),
    )

    receipt = await harness.send()

    assert receipt.outcome is not None
    assert receipt.outcome.status is TurnStatus.STOPPED_BY_LIMIT
    assert "turn.stopped_by_limit" in names(harness)
    assert "turn.completed" not in names(harness)


async def test_a_blocked_tool_call_reports_call_blocked() -> None:
    hooks = RecordingHookDispatcher(
        {HookName.BEFORE_TOOL_CALL: HookOutcome(HookAction.BLOCK, reason="不许读盘")}
    )
    harness = build(
        ScriptedProvider([tool_response(tool_call("fs.read")), text_response("那算了")]),
        tool_specs=[tool_spec("fs.read")],
        hooks=hooks,
    )

    await harness.send()

    assert "tool.call_blocked" in names(harness)
    assert "tool.call_completed" not in names(harness)


async def test_a_failing_tool_reports_call_failed_but_does_not_fail_the_turn() -> None:
    failed = ToolResult(
        call_id="x", ok=False, content="没这个文件", truncated=False,
        side_effect=SideEffect.NONE,
        error=NucleaError(ErrorCode.INPUT_MALFORMED, "路径不存在"),
    )
    harness = build(
        ScriptedProvider([tool_response(tool_call("fs.read")), text_response("换个路径吧")]),
        tool_specs=[tool_spec("fs.read")],
        tools=RecordingToolInvoker(results={"fs.read": failed}),
    )

    receipt = await harness.send()

    assert "tool.call_failed" in names(harness)
    assert receipt.outcome is not None
    assert receipt.outcome.status is TurnStatus.COMPLETED


# ------------------------------------------------------------------ E 持久化


async def test_the_transcript_keeps_input_answer_and_tool_output() -> None:
    harness = build(
        ScriptedProvider([tool_response(tool_call("fs.read")), text_response("答案")]),
        tool_specs=[tool_spec("fs.read")],
        tools=RecordingToolInvoker(results={"fs.read": ok_result("12")}),
    )

    await harness.send()

    _, records = harness.store.appends[0]
    assert [(r.role, r.content) for r in records] == [
        (Role.USER, "统计一下文件数"),
        (Role.TOOL, "12"),
        (Role.ASSISTANT, "答案"),
    ]


async def test_empty_assistant_messages_never_enter_history() -> None:
    """基线 `test_empty_assistant_messages_never_enter_history` 的新层对应物。"""
    harness = build(
        ScriptedProvider([tool_response(tool_call("fs.read")), text_response("答案")]),
        tool_specs=[tool_spec("fs.read")],
    )

    await harness.send()

    _, records = harness.store.appends[0]
    # 第一轮的 assistant 只有 tool_calls、没有正文，它不进历史。
    assert [r.role for r in records].count(Role.ASSISTANT) == 1


async def test_orphan_tool_results_are_dropped_on_save() -> None:
    """没有对应调用声明的 tool 记录会让后续请求在 Provider 侧被拒。"""
    from nucleamind.kernel.turn import Transcript

    transcript = Transcript(turn_id="t1", created_at=NOW, limits=TurnLimits())  # type: ignore[arg-type]
    transcript.add_tool_result(ok_result("孤儿", call_id="never-declared"))

    assert transcript.records() == ()


async def test_tool_results_are_truncated_again_at_the_persistence_boundary() -> None:
    harness = build(
        ScriptedProvider([tool_response(tool_call("fs.read")), text_response("好")]),
        tool_specs=[tool_spec("fs.read")],
        tools=RecordingToolInvoker(results={"fs.read": ok_result("x" * 500)}),
        limits=TurnLimits(tool_result_max_bytes=50),
    )

    await harness.send()

    _, records = harness.store.appends[0]
    tool_record = next(r for r in records if r.role is Role.TOOL)
    assert len(tool_record.content.encode()) <= 50


async def test_a_persistence_failure_fails_the_turn_instead_of_pretending() -> None:
    harness = build(ScriptedProvider([text_response("答完了")]), store=FailingStore())

    receipt = await harness.send()

    assert receipt.outcome is not None
    assert receipt.outcome.status is TurnStatus.FAILED  # `SES-003`
    assert receipt.outcome.error is not None
    assert receipt.outcome.error.code is ErrorCode.PERSISTENCE_WRITE_FAILED
    assert names(harness)[-1] == "turn.failed"


async def test_an_existing_session_reports_loaded_not_started() -> None:
    store = FakeSessionStore()
    harness = build(ScriptedProvider([], default=text_response("好")), store=store)

    await harness.send(inbound(message_id="m1"))
    await harness.send(inbound(message_id="m2"))

    assert names(harness).count("session.started") == 1
    assert names(harness).count("session.loaded") == 1


async def test_trimmed_history_is_compacted_reloaded_and_reassembled_once() -> None:
    store = FakeSessionStore(
        [
            old_message(1, Role.USER, "旧问题" * 30),
            old_message(2, Role.ASSISTANT, "旧回答" * 30),
        ]
    )
    provider = Provider()
    compactor = ScriptedCompactor(CompactionResult(through=2, content="前情摘要"))
    harness = build(
        ScriptedProvider([text_response("新回答")]),
        store=store,
        context_providers=[binding(provider)],
        compactor=compaction_policy(compactor),
        limits=TurnLimits(context_max_tokens=20),
    )

    receipt = await harness.send()

    assert receipt.outcome is not None
    assert receipt.outcome.status is TurnStatus.COMPLETED
    assert compactor.calls == 1
    assert provider.calls == 2
    assert len(store.compactions) == 1
    assert names(harness).count("session.compacted") == 1
    event = harness.events.of(EventName.SESSION_COMPACTED)[0]
    assert event.payload["through"] == 2
    assert harness.provider.requests[0].messages[0].content == "前情摘要"


async def test_second_assembly_never_triggers_another_compaction() -> None:
    store = FakeSessionStore(
        [
            old_message(1, Role.USER, "旧问题" * 30),
            old_message(2, Role.ASSISTANT, "旧回答" * 30),
        ]
    )
    compactor = ScriptedCompactor(
        CompactionResult(through=2, content="很长的摘要" * 30)
    )
    harness = build(
        ScriptedProvider([text_response("新回答")]),
        store=store,
        compactor=compaction_policy(compactor),
        limits=TurnLimits(context_max_tokens=15),
    )

    receipt = await harness.send()

    assert receipt.outcome is not None
    assert receipt.outcome.status is TurnStatus.COMPLETED
    assert compactor.calls == 1
    assert len(store.compactions) == 1


async def test_compactor_failure_is_reported_but_turn_continues() -> None:
    store = FakeSessionStore([old_message(1, Role.USER, "旧问题" * 30)])
    compactor = ScriptedCompactor(error=RuntimeError("boom"))
    harness = build(
        ScriptedProvider([text_response("仍然回答")]),
        store=store,
        compactor=compaction_policy(compactor),
        limits=TurnLimits(context_max_tokens=15),
    )

    receipt = await harness.send()

    assert receipt.outcome is not None
    assert receipt.outcome.status is TurnStatus.COMPLETED
    assert receipt.content == "仍然回答"
    assert compactor.calls == 1
    assert store.compactions == []
    assert names(harness).count("plugin.failed") == 1


# ------------------------------------------------------------------ F 取消


async def test_cancelling_mid_stream_keeps_what_was_produced() -> None:
    harness = build(ScriptedProvider([]), stream=True)

    def stream(request, cancel):  # noqa: ANN001, ANN202 - 契约要求普通 def 返回迭代器
        return _cancelling_stream(harness)

    harness.provider.stream = stream  # type: ignore[method-assign]
    receipt = await harness.send()

    assert receipt.outcome is not None
    assert receipt.outcome.status is TurnStatus.CANCELLED
    assert receipt.outcome.cancel_reason is CancelReason.USER
    _, records = harness.store.appends[0]
    saved = [r for r in records if r.role is Role.ASSISTANT]
    assert saved and saved[0].interrupted is True
    assert saved[0].content == "已经说了一半"
    assert harness.delivered[-1].stream_state is StreamState.CANCELLED


async def test_shutdown_cancellation_finishes_the_turn_and_persists_partial_content() -> None:
    harness = build(ScriptedProvider([]), stream=True)
    partial_sent = asyncio.Event()

    def stream(request, cancel):  # noqa: ANN001, ANN202 - 契约要求普通 def
        del request

        async def gen():  # noqa: ANN202
            from nucleamind.contracts import ChunkKind, ModelChunk

            yield ModelChunk(kind=ChunkKind.TEXT, text="停机前的半句")
            partial_sent.set()
            while not cancel.requested:
                await asyncio.sleep(0)
            cancel.raise_if_requested()

        return gen()

    harness.provider.stream = stream  # type: ignore[method-assign]
    running = asyncio.create_task(harness.send())
    await asyncio.wait_for(partial_sent.wait(), timeout=1)

    forced = await harness.orchestrator.finish_shutdown(timeout_ms=1_000)
    receipt = await asyncio.wait_for(running, timeout=1)

    assert forced is None
    assert receipt.outcome is not None
    assert receipt.outcome.status is TurnStatus.CANCELLED
    assert receipt.outcome.cancel_reason is CancelReason.SHUTDOWN
    saved = [record for _, records in harness.store.appends for record in records]
    assistant = [record for record in saved if record.role is Role.ASSISTANT]
    assert assistant and assistant[0].content == "停机前的半句"
    assert assistant[0].interrupted is True
    assert names(harness).count("turn.cancelled") == 1


async def test_shutdown_closes_new_turn_admission() -> None:
    harness = build(ScriptedProvider([text_response("不应调用")]))
    harness.orchestrator.begin_shutdown()

    receipt = await harness.send()

    assert receipt.admitted is False
    assert receipt.error is not None
    assert receipt.error.code is ErrorCode.CANCELLED_BY_SHUTDOWN
    assert harness.provider.call_count == 0
    assert names(harness) == ["turn.rejected"]


async def test_shutdown_also_drains_submissions_waiting_for_the_session() -> None:
    harness = build(ScriptedProvider([], default=text_response("不应调用")))
    entered = asyncio.Event()
    calls = 0

    async def slow(request, cancel):  # noqa: ANN001, ANN202
        nonlocal calls
        del request
        calls += 1
        entered.set()
        while not cancel.requested:
            await asyncio.sleep(0)
        cancel.raise_if_requested()

    harness.provider.complete = slow  # type: ignore[method-assign]
    first = asyncio.create_task(harness.send(inbound(message_id="active")))
    await asyncio.wait_for(entered.wait(), timeout=1)
    waiting = asyncio.create_task(harness.send(inbound(message_id="waiting")))
    await asyncio.sleep(0)

    forced = await harness.orchestrator.finish_shutdown(timeout_ms=1_000)
    active_receipt, waiting_receipt = await asyncio.gather(first, waiting)

    assert forced is None
    assert active_receipt.outcome is not None
    assert active_receipt.outcome.cancel_reason is CancelReason.SHUTDOWN
    assert waiting_receipt.admitted is False
    assert waiting_receipt.error is not None
    assert waiting_receipt.error.code is ErrorCode.CANCELLED_BY_SHUTDOWN
    assert calls == 1


async def test_shutdown_force_cancels_an_uncooperative_submission_after_grace() -> None:
    harness = build(ScriptedProvider([]))
    entered = asyncio.Event()

    async def stuck(request, cancel):  # noqa: ANN001, ANN202
        del request, cancel
        entered.set()
        await asyncio.Event().wait()

    harness.provider.complete = stuck  # type: ignore[method-assign]
    running = asyncio.create_task(harness.send())
    await asyncio.wait_for(entered.wait(), timeout=1)
    live = harness.orchestrator.live_turns

    forced = await harness.orchestrator.finish_shutdown(timeout_ms=1)
    result = await asyncio.gather(running, return_exceptions=True)

    assert forced == live
    assert len(forced) == 1
    assert isinstance(result[0], asyncio.CancelledError)


def _cancelling_stream(harness):  # noqa: ANN001, ANN202
    """产出两个分片后请求取消——「已产生的内容必须留存」才有东西可断言。"""

    async def gen():  # noqa: ANN202
        from nucleamind.contracts import ChunkKind, ModelChunk

        yield ModelChunk(kind=ChunkKind.TEXT, text="已经")
        yield ModelChunk(kind=ChunkKind.TEXT, text="说了一半")
        harness.orchestrator.cancel(harness.orchestrator.live_turns[0])
        yield ModelChunk(kind=ChunkKind.TEXT, text="不该出现")

    return gen()


async def test_tools_not_reached_when_cancelled_report_no_side_effect() -> None:
    """取消时尚未轮到的工具必须是 `side_effect=NONE`——否则用户会以为它可能做了什么。"""
    harness = build(
        ScriptedProvider(
            [tool_response(tool_call("fs.read", call_id="a"), tool_call("fs.write", call_id="b"))]
        ),
        tool_specs=[
            tool_spec("fs.read", concurrency=Concurrency.EXCLUSIVE),
            tool_spec("fs.write", concurrency=Concurrency.EXCLUSIVE),
        ],
        tools=RecordingToolInvoker(results={"fs.read": ok_result("读到了")}),
    )
    invoker = harness.deps.tools

    original = invoker.invoke

    async def invoke_then_cancel(invocation, cancel):  # noqa: ANN001, ANN202
        result = await original(invocation, cancel)
        harness.orchestrator.cancel(harness.orchestrator.live_turns[0])
        return result

    invoker.invoke = invoke_then_cancel  # type: ignore[method-assign]

    receipt = await harness.send()

    assert receipt.outcome is not None
    assert receipt.outcome.status is TurnStatus.CANCELLED
    blocked = harness.events.of(EventName.TOOL_CALL_BLOCKED)
    assert [event.payload["side_effect"] for event in blocked] == [SideEffect.NONE.value]


async def test_a_cancelled_session_still_accepts_the_next_input() -> None:
    harness = build(ScriptedProvider([], default=text_response("后续正常")))
    calls = {"n": 0}
    original = harness.deps.model.complete

    async def complete(request, cancel):  # noqa: ANN001, ANN202
        calls["n"] += 1
        if calls["n"] == 1:
            harness.orchestrator.cancel(harness.orchestrator.live_turns[0])
        return await original(request, cancel)

    harness.provider.complete = complete  # type: ignore[method-assign]

    first = await harness.send(inbound(message_id="m1"))
    second = await harness.send(inbound(message_id="m2"))

    assert first.outcome is not None and first.outcome.status is TurnStatus.CANCELLED
    assert second.outcome is not None and second.outcome.status is TurnStatus.COMPLETED
    assert second.content == "后续正常"


def test_cancelling_an_unknown_turn_returns_false() -> None:
    harness = build(ScriptedProvider([]))
    assert harness.orchestrator.cancel("no-such-turn") is False  # type: ignore[arg-type]


# ------------------------------------------------------------------ G Hook


async def test_turn_start_rejection_stops_the_turn_with_a_permission_error() -> None:
    hooks = RecordingHookDispatcher(
        {HookName.TURN_START: HookOutcome(HookAction.REJECT, reason="实例维护中")}
    )
    harness = build(ScriptedProvider([]), hooks=hooks)

    receipt = await harness.send()

    assert receipt.outcome is not None
    assert receipt.outcome.status is TurnStatus.FAILED
    assert receipt.outcome.error is not None
    assert receipt.outcome.error.code is ErrorCode.PERMISSION_TURN_REJECTED
    assert harness.provider.call_count == 0
    assert names(harness) == ["turn.started", "turn.rejected", "turn.failed"]


async def test_before_model_request_is_dispatched_once_per_iteration() -> None:
    """`D09` 已在 engine 里每轮分发一次，`D14` 不得再分发一次（§6.2.1）。"""
    hooks = RecordingHookDispatcher()
    harness = build(
        ScriptedProvider([tool_response(tool_call("fs.read")), text_response("好")]),
        tool_specs=[tool_spec("fs.read")],
        hooks=hooks,
    )

    receipt = await harness.send()

    assert receipt.outcome is not None
    assert hooks.count(HookName.BEFORE_MODEL_REQUEST) == receipt.outcome.iterations == 2


async def test_turn_end_receives_the_final_outcome() -> None:
    hooks = RecordingHookDispatcher()
    harness = build(ScriptedProvider([text_response("好")]), hooks=hooks)

    await harness.send()

    contexts = hooks.contexts_for(HookName.TURN_END)
    assert len(contexts) == 1
    assert contexts[0].outcome is not None
    assert contexts[0].outcome.status is TurnStatus.COMPLETED


async def test_a_context_provider_failure_only_records_an_event() -> None:
    harness = build(
        ScriptedProvider([text_response("好")]),
        context_providers=[binding(Provider(error=RuntimeError("挂了")), name="memory")],
    )

    receipt = await harness.send()

    assert receipt.outcome is not None
    assert receipt.outcome.status is TurnStatus.COMPLETED  # `NFR-204`
    assert "plugin.failed" in names(harness)


# ------------------------------------------------------------------ H 预算


async def test_a_budget_stop_still_gives_the_user_something_to_read() -> None:
    """基线 `test_budget_exhaustion_is_pushed_through_the_stream` 的新层对应物。"""
    harness = build(
        ScriptedProvider(
            [tool_response(tool_call("fs.read")), tool_response(tool_call("fs.read"))],
            default=text_response("我尽力了，先说到这里"),
        ),
        tool_specs=[tool_spec("fs.read")],
        limits=TurnLimits(max_iterations=2),
    )

    receipt = await harness.send()

    assert receipt.outcome is not None
    assert receipt.outcome.status is TurnStatus.STOPPED_BY_LIMIT
    assert receipt.content == "我尽力了，先说到这里"
    # 收尾请求不带 tools：再给一次工具机会只会再撞一次上限。
    assert harness.provider.requests[-1].tools == ()
    assert harness.delivered[-1].stream_state is StreamState.FINAL


async def test_a_failing_wrap_up_falls_back_to_the_breach_description() -> None:
    harness = build(
        ScriptedProvider(
            [tool_response(tool_call("fs.read"))],
            default=NucleaError(ErrorCode.EXTERNAL_MODEL_PROVIDER, "又挂了"),
        ),
        tool_specs=[tool_spec("fs.read")],
        limits=TurnLimits(max_iterations=1),
    )

    receipt = await harness.send()

    assert receipt.outcome is not None
    assert receipt.outcome.status is TurnStatus.STOPPED_BY_LIMIT
    assert "max_iterations" in receipt.content
    assert "plugin.failed" in names(harness)


async def test_reasoning_deltas_are_marked_and_never_become_the_answer() -> None:
    """推理不是答案：它照发给 UI，但不进历史、不构成最终正文。"""
    from nucleamind.contracts import ChunkKind, ModelChunk, StopReason

    chunks = [
        ModelChunk(kind=ChunkKind.REASONING, text="先想一下"),
        ModelChunk(kind=ChunkKind.TEXT, text="答案"),
        ModelChunk(kind=ChunkKind.DONE, stop_reason=StopReason.END_TURN),
    ]
    harness = build(ScriptedProvider(stream_chunks=chunks), stream=True)

    receipt = await harness.send()

    reasoning = [m for m in harness.delivered if m.metadata.get("reasoning")]
    assert [m.content for m in reasoning] == ["先想一下"]
    assert receipt.content == "答案"
    _, records = harness.store.appends[0]
    assert "先想一下" not in "".join(r.content for r in records)


async def test_the_watchdog_cancels_a_turn_that_never_returns() -> None:
    """engine 只在迭代边界查预算，一个挂住的模型调用需要外部叫停（§6.4）。"""
    harness = build(ScriptedProvider([]), limits=TurnLimits(turn_timeout_ms=20))

    async def hang(request, cancel):  # noqa: ANN001, ANN202
        await cancel.wait()
        raise NucleaError(ErrorCode.CANCELLED_BY_BUDGET, "超时了")

    harness.provider.complete = hang  # type: ignore[method-assign]

    async with asyncio.timeout(2):
        receipt = await harness.send()

    assert receipt.outcome is not None
    assert receipt.outcome.status is TurnStatus.CANCELLED
    # 令牌才是取消原因的权威：供应商折出来的错误码是多对一的，而 `turn_timeout_ms`
    # 的原因是 `TIMEOUT`（`LIMIT_OUTCOMES`），不是笼统的 `BUDGET`。
    assert receipt.outcome.cancel_reason is CancelReason.TIMEOUT


def test_the_receipt_type_is_what_the_scheduler_carries() -> None:
    """装配时 `SessionScheduler` 的类型参数必须是 `TurnReceipt`，否则 `run` 的返回值无从收窄。"""
    scheduler: SessionScheduler[TurnReceipt] = SessionScheduler()
    assert scheduler.policy is ConcurrencyPolicy.QUEUE


# ------------------------------------------------------------------ J 出站附件（`D47`）


def _png(name: str) -> AttachmentRef:
    return AttachmentRef(
        source=AttachmentSource.WORKSPACE,
        locator=f"artifacts/images/{name}",
        media_type="image/png",
        size_bytes=8,
    )


def _with_attachments(*attachments: AttachmentRef) -> ToolResult:
    return replace(ok_result("画好了"), attachments=attachments)


async def test_tool_attachments_ride_on_the_final_frame() -> None:
    """工具产出的附件挂在终帧上，中间帧一个都不带。

    挂在中间帧上等于让 Channel 收到 N 份同样的附件——出站分片是同一段正文的切块。
    """
    harness = build(
        ScriptedProvider([tool_response(tool_call("artifact.create")), text_response("给你")]),
        tool_specs=[tool_spec("artifact.create")],
        tools=RecordingToolInvoker(results={"artifact.create": _with_attachments(_png("a.png"))}),
    )

    receipt = await harness.send()

    final = receipt.messages[-1]
    assert final.stream_state is StreamState.FINAL
    assert [item.locator for item in final.attachments] == ["artifacts/images/a.png"]
    assert all(not m.attachments for m in receipt.messages[:-1])
    _, records = harness.store.appends[0]
    assert records[-1].role is Role.ASSISTANT
    assert records[-1].attachments == (_png("a.png"),)


async def test_the_same_attachment_twice_is_delivered_once() -> None:
    """内容寻址的落点让重复生成落在同一个 locator 上；用户不该收到两份。"""
    harness = build(
        ScriptedProvider(
            [
                tool_response(tool_call("artifact.create", call_id="c1")),
                tool_response(tool_call("artifact.create", call_id="c2")),
                text_response("给你"),
            ]
        ),
        tool_specs=[tool_spec("artifact.create")],
        tools=RecordingToolInvoker(results={"artifact.create": _with_attachments(_png("a.png"))}),
    )

    receipt = await harness.send()

    assert len(receipt.messages[-1].attachments) == 1


async def test_attachments_beyond_the_cap_are_reported_not_silently_dropped() -> None:
    """超上界不让终帧构造失败，但要说出来——`MAX_ATTACHMENTS` 是出站消息的硬上界。"""
    state = TurnState(
        correlation=Correlation(
            instance_id=INSTANCE, session_key=SessionKey("cli", "local"), turn_id=TurnId("t1")
        ),
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        transcript=Transcript(
            turn_id=TurnId("t1"),
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            limits=TurnLimits(),
        ),
    )

    state.collect_attachments(_with_attachments(*[_png(f"{i}.png") for i in range(MAX_ATTACHMENTS)]))
    state.collect_attachments(_with_attachments(_png("extra.png")))

    assert len(state.attachments) == MAX_ATTACHMENTS
    assert state.dropped_attachments == 1


async def test_a_final_frame_with_only_attachments_is_still_sent() -> None:
    """正文为空但有附件时照发：契约的「内容与附件不能同时为空」本来就是二选一。"""
    harness = build(
        ScriptedProvider([tool_response(tool_call("artifact.create")), text_response("")]),
        tool_specs=[tool_spec("artifact.create")],
        tools=RecordingToolInvoker(results={"artifact.create": _with_attachments(_png("a.png"))}),
    )

    receipt = await harness.send()

    assert receipt.messages[-1].content == ""
    assert len(receipt.messages[-1].attachments) == 1
    _, records = harness.store.appends[0]
    assert records[-1].content == ""
    assert records[-1].attachments == (_png("a.png"),)


# ------------------------------------------------------------------ K 重试（`D48`）

RETRY_FAST = RetryPolicy(max_attempts=3, base_delay_ms=1, max_delay_ms=1)


def _flaky() -> NucleaError:
    """一条如实标了 `retryable=True` 的上游故障，形状同 `model_openai/faults.py`。"""
    return NucleaError(ErrorCode.EXTERNAL_MODEL_PROVIDER, "模型供应商限流。", retryable=True)


async def test_a_transient_model_failure_no_longer_kills_the_turn() -> None:
    """`D48` 之前：一次 429 / 503 直接把整条 turn 打成 `FAILED`，用户要自己重发。

    重试**不**重新分发 `before_model_request`，因此 `model.request_started` 的条数仍然
    等于迭代数（engine 的那条不变量），重试只多出一条 `model.request_failed`。
    """
    harness = build(
        ScriptedProvider([_flaky(), text_response("一共 12 个")]), retry=RETRY_FAST
    )

    receipt = await harness.send()

    assert names(harness) == [
        "turn.started",
        "session.started",
        "model.request_started",
        "model.request_failed",
        "model.response_received",
        "turn.completed",
    ]
    assert receipt.content == "一共 12 个"


async def test_a_retry_does_not_re_execute_tools_that_already_ran() -> None:
    """**这条是「包一层 ModelProvider」而不是「重跑 run_turn」的理由。**

    在 orchestrator 重调一次 `run_turn` 会从第一轮重来，那个 `fs.read` 会被执行两次——
    一条 `rm` 或一次转账不会因为模型那边抖了一下就该做两遍。
    """
    invoker = RecordingToolInvoker()
    harness = build(
        ScriptedProvider(
            [tool_response(tool_call("fs.read")), _flaky(), text_response("好了")]
        ),
        tool_specs=[tool_spec("fs.read")],
        tools=invoker,
        retry=RETRY_FAST,
    )

    receipt = await harness.send()

    assert receipt.outcome is not None
    assert receipt.outcome.status is TurnStatus.COMPLETED
    assert [name for name, _ in invoker.invocations_by_name] == ["fs.read"]
    assert receipt.outcome.tool_calls == 1


async def test_a_persistent_failure_still_fails_the_turn_with_the_real_reason() -> None:
    """重试不是掩盖：三次都不行时，用户看到的仍是供应商那句话。"""
    harness = build(
        ScriptedProvider([_flaky(), _flaky(), _flaky()]), retry=RETRY_FAST
    )

    receipt = await harness.send()

    assert receipt.outcome is not None
    assert receipt.outcome.status is TurnStatus.FAILED
    assert receipt.content == "模型供应商限流。"
    assert [event.name.value for event in harness.events.events].count(
        "model.request_failed"
    ) == 3


async def test_a_silent_model_becomes_a_failure_with_a_sentence() -> None:
    """`D48` 之前：终帧空正文被 `emit_outbound` 丢掉，用户**一条消息都收不到**。"""
    harness = build(
        ScriptedProvider([text_response(""), text_response(""), text_response("")]),
        retry=RETRY_FAST,
    )

    receipt = await harness.send()

    assert receipt.outcome is not None
    assert receipt.outcome.status is TurnStatus.FAILED
    assert receipt.content == "模型连续 3 次返回空回答。"
    assert receipt.messages[-1].content == "模型连续 3 次返回空回答。"


# ------------------------------------------------------------------ L 截断（`EDG-304`）


async def test_max_tokens_turn_has_cancelled_stream_state() -> None:
    """续写上限耗尽的答案出站时 `stream_state=CANCELLED`，Channel 因此会附加标记（`EDG-304`）。

    `TurnStatus` 仍然是 `COMPLETED`——模型没报错、turn 也没被取消——只是出站消息的
    呈现状态要说清楚这不是一个完整答案。
    """
    from nucleamind.contracts import ModelResponse, StopReason

    truncated_response = ModelResponse(
        model_id="fake-model", stop_reason=StopReason.MAX_TOKENS, content="被截断的答案"
    )
    harness = build(ScriptedProvider([], default=truncated_response))

    receipt = await harness.send()

    assert receipt.outcome is not None
    assert receipt.outcome.status is TurnStatus.COMPLETED
    assert receipt.messages[-1].stream_state is StreamState.CANCELLED
    assert receipt.content == "被截断的答案" * 4


async def test_max_tokens_continuation_aggregates_answer_and_persists_once() -> None:
    """多段 `MAX_TOKENS` 响应聚合成一条会话 assistant 记录。"""
    from nucleamind.contracts import ModelResponse, StopReason

    harness = build(
        ScriptedProvider(
            [
                ModelResponse(
                    model_id="fake-model", stop_reason=StopReason.MAX_TOKENS, content="第一段"
                ),
                text_response("第二段"),
            ]
        )
    )

    receipt = await harness.send()

    assert receipt.outcome is not None
    assert receipt.outcome.status is TurnStatus.COMPLETED
    assert receipt.content == "第一段第二段"
    assert receipt.messages[-1].stream_state is StreamState.FINAL
    assert [record.content for record in harness.store.appends[-1][1]][-1:] == ["第一段第二段"]


async def test_end_turn_response_keeps_final_stream_state() -> None:
    """正常结束的 turn 出站消息仍是 `FINAL`，不受截断逻辑影响。"""
    harness = build(ScriptedProvider([text_response("完整答案")]))

    receipt = await harness.send()

    assert receipt.outcome is not None
    assert receipt.outcome.status is TurnStatus.COMPLETED
    assert receipt.messages[-1].stream_state is StreamState.FINAL
