"""职责：Turn 准入、执行组装与事件/持久化收口，也是 Turn 事件的唯一发布点。
不负责：模型—工具循环、Hook 实现、压缩策略、工具执行与排队策略。
准入顺序是去重 → Session 并发 → 分流。MERGE 每批一个 Turn；命令 Turn 有事件但不写历史。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Sequence
from dataclasses import replace

from karyvia.contracts import (
    CancelReason,
    Correlation,
    Disposition,
    ErrorCode,
    EventName,
    HookAction,
    HookContext,
    HookName,
    InboundMessage,
    KaryviaError,
    ModelRequest,
    OutboundMessage,
    SessionKey,
    StreamState,
    TurnId,
    TurnOutcome,
)
from karyvia.kernel.routing import SubmitOutcome, SubmitStatus

from .cancel import CancelToken, Checkpoint
from .compaction import SessionCompactionTracker
from .context_builder import assemble
from .engine import run_turn
from .events import (
    ModelReasoningDelta,
    ModelResponseCompleted,
    ModelTextDelta,
    TerminalEvent,
    ToolCallCompleted,
    ToolCallStarted,
    TurnCompleted,
    TurnEvent,
    TurnStoppedByLimit,
)
from .finalization import finish_turn
from .limits import BudgetLedger
from .message_projection import render_message_content
from .orchestration import OrchestratorDeps, TurnReceipt, emit_outbound, engine_deps
from .tracker import TurnTracker
from .transcript import ModelInput, Transcript, TurnState
from .translation import (
    as_karyvia,
    outcome_for_error,
    outcome_from,
    outcome_without_engine,
    tool_event_name,
)

__all__ = ["TurnOrchestrator"]


class TurnOrchestrator:
    """§10.2 的执行者。一个实例一个，`handle()` 可被并发调用。"""

    def __init__(self, deps: OrchestratorDeps) -> None:
        self._deps = deps
        self._turns = TurnTracker[TurnReceipt]()

    @property
    def live_turns(self) -> tuple[TurnId, ...]:
        """当前在跑的 turn，`/cancel` 与实例停止时用。"""
        return self._turns.live_turns

    def cancel(self, turn_id: TurnId, reason: CancelReason = CancelReason.USER) -> bool:
        """请求取消在跑的 turn；幂等，第一次原因胜出。"""
        return self._turns.cancel(turn_id, reason)

    def begin_shutdown(self) -> None:
        """关闭新准入，并向此刻全部在途 Turn 请求业务取消。"""
        self._turns.begin_shutdown()

    async def finish_shutdown(self, *, timeout_ms: int) -> tuple[TurnId, ...] | None:
        """等待业务取消正常收口；超时后强制取消仍不返回的执行任务。"""
        return await self._turns.finish_shutdown(timeout_ms=timeout_ms)

    async def handle(self, message: InboundMessage) -> TurnReceipt:
        """处理入站消息；业务异常折进回执，`BaseException` 穿透。"""
        return await self.submit(message)

    def submit(self, message: InboundMessage) -> asyncio.Future[TurnReceipt]:
        """同步完成准入登记，返回受停机管理的处理任务。
        Session 票据在返回前已经登记，并发等待不会改变 FIFO。
        """
        if not self._turns.accepting:
            return self._turns.completed(self._reject_stopping(message))
        submission = self._admit(message)
        self._turns.track_submission(submission)
        return submission

    def _admit(self, message: InboundMessage) -> asyncio.Future[TurnReceipt]:
        """按去重 → Session 调度同步登记，并返回最终回执。"""
        deps = self._deps
        key = message.session_key(deps.scope)
        turn_id = TurnId(uuid.uuid4().hex)
        hit = deps.dedup.remember(message.channel_id, message.message_id, turn_id)
        if hit is not None:
            deps.bus.publish(
                EventName.TURN_REJECTED,
                payload={
                    "reason": "duplicate",
                    "message_id": message.message_id,
                    "duplicate_of": hit.turn_id,
                },
            )
            return self._turns.completed(
                TurnReceipt(turn_id=hit.turn_id, admitted=False, duplicate_of=hit.turn_id)
            )

        async def run(batch: tuple[InboundMessage, ...]) -> TurnReceipt:
            return await self._run(batch, key, turn_id)

        submitted = deps.scheduler.submit(key, message, run, turn_id=turn_id)
        return asyncio.create_task(
            self._finish_submission(message, turn_id, submitted),
            name=f"turn-submission:{message.message_id}",
        )

    async def _finish_submission(
        self,
        message: InboundMessage,
        turn_id: TurnId,
        submission: Awaitable[SubmitOutcome[TurnReceipt]],
    ) -> TurnReceipt:
        """把调度结果收窄成回执，并清理未执行消息的去重占位。"""
        submitted = await submission
        if submitted.status is SubmitStatus.REJECTED or submitted.result is None:
            self._deps.dedup.discard(message.channel_id, message.message_id, turn_id)
            error = submitted.error or KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED, "调度器既没有结果也没有拒绝原因。"
            )
            self._deps.bus.publish(
                EventName.TURN_REJECTED,
                payload={"reason": "session_busy", "message_id": message.message_id},
                error=error,
            )
            return TurnReceipt(turn_id=turn_id, admitted=False, error=error)
        receipt = submitted.result
        if not receipt.admitted:
            self._deps.dedup.discard(message.channel_id, message.message_id, turn_id)
        return receipt

    def _reject_stopping(self, message: InboundMessage) -> TurnReceipt:
        """拒绝停机后到达或在 Session 队列中尚未开始的消息。"""
        turn_id = TurnId(uuid.uuid4().hex)
        error = KaryviaError(
            ErrorCode.CANCELLED_BY_SHUTDOWN,
            "实例正在停止，不再接收新的 Turn。",
        )
        self._deps.bus.publish(
            EventName.TURN_REJECTED,
            payload={"reason": "instance_stopping", "message_id": message.message_id},
            error=error,
        )
        return TurnReceipt(turn_id=turn_id, admitted=False, error=error)

    async def _run(
        self, batch: Sequence[InboundMessage], key: SessionKey, turn_id: TurnId
    ) -> TurnReceipt:
        """持有 session 槽位期间的全部工作。会话写入只发生在这里（单一写者）。"""
        if not self._turns.accepting:
            return self._reject_stopping(batch[0])
        deps = self._deps
        for message in batch:
            deps.dedup.rebind(message.channel_id, message.message_id, turn_id)
        correlation = Correlation(instance_id=deps.instance_id, session_key=key, turn_id=turn_id)
        started_at = deps.clock()
        state = TurnState(
            correlation=correlation,
            started_at=started_at,
            transcript=Transcript(turn_id=turn_id, created_at=started_at, limits=deps.limits),
        )
        token = CancelToken()
        self._turns.activate(turn_id, token)
        try:
            deps.bus.publish(
                EventName.TURN_STARTED,
                correlation=correlation,
                payload={
                    "message_id": batch[0].message_id,
                    "channel_id": batch[0].channel_id,
                    "merged_from": [item.message_id for item in batch[1:]],
                },
            )
            return await self._execute(batch, key, state, token)
        # 业务取消由检查点抛普通异常并折为 CANCELLED；任务级 CancelledError 必须穿透。
        except Exception as error:
            return await finish_turn(
                deps,
                state,
                outcome_for_error(
                    error,
                    correlation=correlation,
                    started_at=started_at,
                    finished_at=deps.clock(),
                    iterations=state.iterations,
                    tool_calls=state.tool_calls,
                ),
            )
        finally:
            self._turns.finish(turn_id)

    async def _execute(
        self,
        batch: Sequence[InboundMessage],
        key: SessionKey,
        state: TurnState,
        token: CancelToken,
    ) -> TurnReceipt:
        """turn_start → 分流 → 会话 → context → engine → 终态。"""
        deps = self._deps
        rejected = await self._turn_start(batch[0], state)
        if rejected is not None:
            return rejected
        routed = await self._route(batch, state, token)
        if routed is not None:
            return routed
        if not state.model_inputs:
            # 命令类 turn：不进模型，但事件流与终态一个都不少（`KER-010`）。
            return await finish_turn(deps, state, self._outcome(state))

        snapshot = await deps.sessions.load(key)
        deps.bus.publish(
            EventName.SESSION_LOADED if snapshot.messages else EventName.SESSION_STARTED,
            correlation=state.correlation,
            payload={"messages": len(snapshot.messages)},
        )

        token.checkpoint(Checkpoint.BEFORE_CONTEXT)  # 检查点 1
        user_input = "\n\n".join(
            render_message_content(item.content, item.attachments) for item in state.model_inputs
        )
        context = await assemble(
            snapshot=snapshot,
            user_input=user_input,
            correlation=state.correlation,
            cancel=token,
            bindings=deps.context_providers,
            extra_fragments=state.fragments,
            memory=deps.memory,
            hooks=deps.hooks,
            accounting=deps.token_accounting,
            now=deps.clock(),
            provider_timeout_ms=deps.context_provider_timeout_ms,
            on_failure=lambda error: self._report(state, error),
        )
        request = ModelRequest(
            model_id=deps.model_id,
            messages=context.messages,
            correlation=state.correlation,
            tools=deps.tool_specs,
            stream=deps.stream,
            timeout_ms=deps.limits.turn_timeout_ms,
        )
        state.session_compaction = SessionCompactionTracker(context.session_history)
        state.transcript.add_inputs(batch)
        terminal = await self._drive(request, state, token)
        if isinstance(terminal, TurnCompleted) and terminal.truncated:
            state.truncated = True
        if isinstance(terminal, TurnStoppedByLimit):
            await self._wrap_up(request, state, token, terminal)
        return await finish_turn(
            deps,
            state,
            outcome_from(
                terminal,
                correlation=state.correlation,
                started_at=state.started_at,
                finished_at=deps.clock(),
            ),
        )

    async def _turn_start(self, message: InboundMessage, state: TurnState) -> TurnReceipt | None:
        """`turn_start` 拦截器。`REJECT` 即本 turn 到此为止（§6.6）。"""
        outcome = await self._deps.hooks.dispatch(
            HookContext(HookName.TURN_START, correlation=state.correlation, message=message)
        )
        if outcome.action is not HookAction.REJECT:
            return None
        error = KaryviaError(
            ErrorCode.PERMISSION_TURN_REJECTED,
            outcome.reason,
            detail={"hook": HookName.TURN_START.value},
        )
        self._deps.bus.publish(
            EventName.TURN_REJECTED,
            correlation=state.correlation,
            payload={"reason": "hook"},
            error=error,
        )
        return await finish_turn(self._deps, state, self._outcome(state, error))

    async def _route(
        self, batch: Sequence[InboundMessage], state: TurnState, token: CancelToken
    ) -> TurnReceipt | None:
        """逐条分流；MERGE 中任一消息被拒都会终止整批。"""
        for message in batch:
            outcome = await self._deps.dispatcher.dispatch(message, state.correlation, token)
            if outcome.disposition is Disposition.REJECTED:
                error = outcome.error or KaryviaError(
                    ErrorCode.KERNEL_INVARIANT_VIOLATED, "分流拒绝了输入却没有给出原因。"
                )
                self._deps.bus.publish(
                    EventName.TURN_REJECTED,
                    correlation=state.correlation,
                    payload={"reason": "command", "message_id": message.message_id},
                    error=error,
                )
                return await finish_turn(self._deps, state, self._outcome(state, error))
            if outcome.result is not None:
                state.fragments.extend(outcome.result.fragments)
                if outcome.result.content:
                    state.text.append(outcome.result.content)
            if outcome.model_input is not None:
                state.model_inputs.append(
                    ModelInput(outcome.model_input, outcome.model_attachments)
                )
        return None

    async def _drive(
        self, request: ModelRequest, state: TurnState, token: CancelToken
    ) -> TerminalEvent:
        """驱动 engine 并翻译事件流，返回终态事件。"""
        deps = self._deps
        state.ledger = BudgetLedger(deps.limits)
        engine = engine_deps(deps, state.ledger, request, state.session_compaction)
        watchdog = asyncio.ensure_future(self._watchdog(token, deps.limits.turn_timeout_ms))
        terminal: TurnEvent | None = None
        try:
            async for event in run_turn(request, engine, token, ledger=state.ledger):
                terminal = event
                await self._on_event(event, state, token)
        finally:
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
        if not isinstance(terminal, TerminalEvent):
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "引擎事件流没有以终态事件结尾。",
                detail={"event": type(terminal).__name__},
            )
        return terminal

    async def _on_event(self, event: TurnEvent, state: TurnState, token: CancelToken) -> None:
        """一个引擎事件的全部去向：出站、事件、待持久化内容。"""
        deps = self._deps
        match event:
            case ModelTextDelta():
                state.text.append(event.text)
                state.pending.append(event.text)
                await self._emit(state, event.text, StreamState.DELTA)
            case ModelReasoningDelta():
                await self._emit(state, event.text, StreamState.DELTA, reasoning=True)
            case ModelResponseCompleted():
                token.checkpoint(Checkpoint.AFTER_MODEL_RESPONSE)  # 检查点 4
                deps.bus.publish(
                    EventName.MODEL_RESPONSE_RECEIVED,
                    correlation=state.correlation,
                    payload={
                        "iteration": event.iteration,
                        "stop_reason": event.response.stop_reason.value,
                        "tool_calls": len(event.response.tool_calls),
                        # 用量的唯一公开出口；复数键名让脱敏整词规则原样放行。
                        "input_tokens": event.response.usage.input_tokens,
                        "output_tokens": event.response.usage.output_tokens,
                    },
                )
                # 清零已完整记录的正文，只让响应之后的半句在取消时落库。
                state.pending.clear()
                state.transcript.declare(event.response.tool_calls)
                if event.response.tool_calls:
                    state.transcript.add_assistant(event.response.content)
                else:
                    # 多次续写属于同一份回答；终帧与会话历史保留拼接后的正文。
                    state.final += event.response.content
            case ToolCallStarted():
                deps.bus.publish(
                    EventName.TOOL_CALL_STARTED,
                    correlation=state.correlation,
                    payload={"tool": event.call.name, "call_id": event.call.call_id},
                )
            case ToolCallCompleted():
                state.transcript.add_tool_result(event.result)
                state.collect_attachments(event.result)
                deps.bus.publish(
                    tool_event_name(event),
                    correlation=state.correlation,
                    payload={
                        "tool": event.call.name,
                        "call_id": event.call.call_id,
                        "disposition": event.disposition.value,
                        "side_effect": event.result.side_effect.value,
                        "truncated": event.result.truncated,
                    },
                    error=event.result.error,
                )
            case _:
                pass  # 终态事件由 `_drive` 的返回值承担，不在这里重复处理

    async def _watchdog(self, token: CancelToken, timeout_ms: int) -> None:
        """turn 总超时（§6.4）。engine 只在迭代边界查预算，挂住的模型调用要外部叫停。"""
        await asyncio.sleep(timeout_ms / 1000)
        token.request(CancelReason.TIMEOUT)

    async def _wrap_up(
        self,
        request: ModelRequest,
        state: TurnState,
        token: CancelToken,
        terminal: TurnStoppedByLimit,
    ) -> None:
        """预算用尽后发一次无工具收尾；失败只记诊断，不改变终态。"""
        try:
            response = await self._deps.model.complete(
                replace(request, tools=(), stream=False), token
            )
        except Exception as error:
            self._report(state, as_karyvia(error))
            response = None
        if response is not None and response.content:
            state.final = response.content
        if not state.final and not state.text:
            state.final = terminal.breach.describe()

    async def _emit(
        self,
        state: TurnState,
        content: str,
        stream_state: StreamState,
        *,
        reasoning: bool = False,
        final: bool = False,
    ) -> OutboundMessage | None:
        """`emit_outbound` 的唯一调用点。存在的理由只有一个：注入 `deps.deliver`。"""
        return await emit_outbound(
            state, content, stream_state, self._deps.deliver, reasoning=reasoning, final=final
        )

    def _report(self, state: TurnState, error: KaryviaError) -> None:
        """非致命失败的统一去向：一条 `plugin.failed`，turn 继续（`NFR-204`）。"""
        self._deps.bus.publish(
            EventName.PLUGIN_FAILED, correlation=state.correlation, error=error
        )

    def _outcome(self, state: TurnState, error: KaryviaError | None = None) -> TurnOutcome:
        return outcome_without_engine(
            correlation=state.correlation,
            started_at=state.started_at,
            finished_at=self._deps.clock(),
            error=error,
        )
