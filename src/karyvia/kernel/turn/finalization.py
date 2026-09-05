"""Turn 终态收口：持久化、事件、Hook 与最终出站消息（技术方案 §10.2）。

职责：把已经确定的 `TurnOutcome` 与 `TurnState` 收口为唯一终态事件和 `TurnReceipt`。
不负责：驱动模型循环、决定终态、处理中间流式分片或编排准入。
"""

from __future__ import annotations

from karyvia.contracts import (
    EventName,
    HookContext,
    HookName,
    Role,
    SessionMessage,
    StreamState,
    TurnOutcome,
    TurnStatus,
)

from .orchestration import OrchestratorDeps, TurnReceipt, emit_outbound
from .transcript import TurnState
from .translation import (
    TERMINAL_EVENT_NAMES,
    TERMINAL_STREAM_STATES,
    as_karyvia,
    outcome_without_engine,
)

__all__ = ["finish_turn"]


async def finish_turn(
    deps: OrchestratorDeps,
    state: TurnState,
    outcome: TurnOutcome,
) -> TurnReceipt:
    """持久化 → 终态事件 → `turn_end` → 出站终帧。"""
    interrupted = outcome.status is TurnStatus.CANCELLED
    if state.final:
        state.transcript.add_assistant(
            state.final,
            interrupted=interrupted,
            attachments=state.attachments,
        )
    elif state.pending:
        # 被打断的半句必须留存并标记为不完整（`KER-007`、`EDG-304`）。
        state.transcript.add_assistant(
            "".join(state.pending),
            interrupted=True,
            attachments=state.attachments,
        )
    elif state.attachments:
        state.transcript.add_assistant("", attachments=state.attachments)
    elif interrupted:
        state.transcript.mark_interrupted()

    records = state.transcript.records()
    if records:
        try:
            await deps.sessions.append(state.correlation.session_key, records)
            pending = (
                state.session_compaction.pending if state.session_compaction is not None else None
            )
            if pending is not None:
                await deps.sessions.compact(
                    state.correlation.session_key,
                    pending.through,
                    SessionMessage(
                        message_id=f"compaction-{state.correlation.turn_id}",
                        role=Role.USER,
                        content=pending.summary.content,
                        created_at=deps.clock(),
                        turn_id=state.correlation.turn_id,
                        metadata={
                            "compactor": deps.turn_compactor.name,
                            "provider": str(deps.turn_compactor.owner),
                        },
                    ),
                )
                deps.bus.publish(
                    EventName.SESSION_COMPACTED,
                    correlation=state.correlation,
                    payload={
                        "through": pending.through,
                        "compactor": deps.turn_compactor.name,
                    },
                )
        # `SES-003`：写失败不得伪装成功，即使模型那边已经答完了。
        except Exception as error:
            outcome = outcome_without_engine(
                correlation=state.correlation,
                started_at=state.started_at,
                finished_at=deps.clock(),
                error=as_karyvia(error),
            )

    deps.bus.publish(
        TERMINAL_EVENT_NAMES[outcome.status],
        correlation=state.correlation,
        payload={
            "iterations": outcome.iterations,
            "tool_calls": outcome.tool_calls,
            "cancel_reason": outcome.cancel_reason.value if outcome.cancel_reason else None,
        },
        error=outcome.error,
    )
    await deps.hooks.dispatch(
        HookContext(HookName.TURN_END, correlation=state.correlation, outcome=outcome)
    )

    content = state.final or "\n\n".join(item for item in state.text if item)
    if not content and outcome.error is not None:
        content = outcome.error.user_message
    # 截断答案用 `CANCELLED` 让 Channel 侧触发标记（`EDG-304`）；Turn 仍为 COMPLETED。
    terminal_state = (
        StreamState.CANCELLED
        if state.truncated
        else TERMINAL_STREAM_STATES[outcome.status]
    )
    final = await emit_outbound(
        state,
        content,
        terminal_state,
        deps.deliver,
        final=True,
    )
    return TurnReceipt(
        turn_id=state.correlation.turn_id,
        admitted=True,
        outcome=outcome,
        error=outcome.error,
        content=content,
        messages=(*state.emitted, *((final,) if final is not None else ())),
    )
