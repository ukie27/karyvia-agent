"""翻译表：引擎事件 → `EventName` / `TurnOutcome` / `StreamState`。

职责：把的 9 个引擎事件与 4 个终态映射到 `contracts` 的事件名、出站流式状态与
`TurnOutcome`，并把逸出的异常折成带码的 `KaryviaError`。
不负责：发布事件、决定何时翻译、构造出站消息——那些在 `orchestrator.py`；本模块是纯函数
与常量表，不含 IO。

**单独成模块是因为它必须是唯一的一份**。同一个引擎事件在两处各翻译一次，事件流就会出现
两套口径，而的按序重放正建立在「一个事件名只对应一件事」之上。表以字面量写在
这里、以字面量断言在测试里，改表必须同时改两处。
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from karyvia.contracts import (
    CancelReason,
    Correlation,
    ErrorCategory,
    ErrorCode,
    EventName,
    KaryviaError,
    StreamState,
    TurnOutcome,
    TurnStatus,
)

from .cancel import CANCEL_REASON_CODES
from .events import (
    TerminalEvent,
    ToolCallCompleted,
    ToolDisposition,
    TurnCancelled,
    TurnCompleted,
    TurnFailed,
    TurnStoppedByLimit,
)

__all__ = [
    "TERMINAL_EVENT_NAMES",
    "TERMINAL_STREAM_STATES",
    "TOOL_EVENT_NAMES",
    "as_karyvia",
    "outcome_for_error",
    "outcome_from",
    "outcome_without_engine",
    "tool_event_name",
]

#: 未执行/已知结论的工具处置各自的事件名。`SKIPPED`（取消时未轮到）与 `BLOCKED`
#: （Hook 拦下）都归 `tool.call_blocked`：它们的共同事实是**没有执行**，而
#: `tool.call_failed` 说的是「执行了但没成」。冻结的 4 个 tool 事件名里没有第三种表达。
#: `EXECUTED` 不在表里——它按 `result.ok` 分成 completed / failed 两种，见
#: `tool_event_name()`。
TOOL_EVENT_NAMES: Final[dict[ToolDisposition, EventName]] = {
    ToolDisposition.BLOCKED: EventName.TOOL_CALL_BLOCKED,
    ToolDisposition.SKIPPED: EventName.TOOL_CALL_BLOCKED,
    ToolDisposition.UNKNOWN_TOOL: EventName.TOOL_CALL_FAILED,
}

#: 四个终态各有自己的事件名。`turn.stopped_by_limit` 是  为此补入的——用
#: `turn.completed` 承载会让「模型说完了」与「撞上预算」不可区分。
TERMINAL_EVENT_NAMES: Final[dict[TurnStatus, EventName]] = {
    TurnStatus.COMPLETED: EventName.TURN_COMPLETED,
    TurnStatus.FAILED: EventName.TURN_FAILED,
    TurnStatus.CANCELLED: EventName.TURN_CANCELLED,
    TurnStatus.STOPPED_BY_LIMIT: EventName.TURN_STOPPED_BY_LIMIT,
}

#: 终态在出站消息上的呈现。`STOPPED_BY_LIMIT` 映射到 `FINAL`：撞上预算的 turn 仍然给出
#: 了一份可呈现的答复（收尾请求），只是它不完整——「不完整」由随附的诊断说明承担，
#: 而不是让 Channel 把一段真实回答标成失败。
TERMINAL_STREAM_STATES: Final[dict[TurnStatus, StreamState]] = {
    TurnStatus.COMPLETED: StreamState.FINAL,
    TurnStatus.FAILED: StreamState.FAILED,
    TurnStatus.CANCELLED: StreamState.CANCELLED,
    TurnStatus.STOPPED_BY_LIMIT: StreamState.FINAL,
}

#: `CANCEL_REASON_CODES` 的逆查表。它是多对一（`TIMEOUT`/`BUDGET` 共用一个码），
#: `BUDGET` 因此胜出——真正需要区分这两者的调用点都持有令牌，能直接读 `token.reason`。
_REASON_BY_CODE: Final[dict[ErrorCode, CancelReason]] = {
    code: reason
    for reason, code in reversed(list(CANCEL_REASON_CODES.items()))
    if reason is not CancelReason.TIMEOUT
}


def tool_event_name(event: ToolCallCompleted) -> EventName:
    """一次工具调用完成后该发哪个事件。"""
    mapped = TOOL_EVENT_NAMES.get(event.disposition)
    if mapped is not None:
        return mapped
    return EventName.TOOL_CALL_COMPLETED if event.result.ok else EventName.TOOL_CALL_FAILED


def outcome_from(
    terminal: TerminalEvent,
    *,
    correlation: Correlation,
    started_at: datetime,
    finished_at: datetime,
) -> TurnOutcome:
    """把终态事件翻译成 `TurnOutcome`。四个终态一一对应，没有兜底分支。

    `match` 在严格模式下拿到穷尽性检查：将来 engine 多一个终态而这里忘了处理，是类型
    检查期报错而不是线上少一种终态（`events.py` 选封闭联合的理由）。
    """
    match terminal:
        case TurnCompleted():
            return TurnOutcome(
                correlation=correlation,
                status=TurnStatus.COMPLETED,
                started_at=started_at,
                finished_at=finished_at,
                iterations=terminal.iterations,
                tool_calls=terminal.tool_calls,
            )
        case TurnFailed():
            return TurnOutcome(
                correlation=correlation,
                status=TurnStatus.FAILED,
                started_at=started_at,
                finished_at=finished_at,
                iterations=terminal.iterations,
                tool_calls=terminal.tool_calls,
                error=terminal.error,
            )
        case TurnCancelled():
            return TurnOutcome(
                correlation=correlation,
                status=TurnStatus.CANCELLED,
                started_at=started_at,
                finished_at=finished_at,
                iterations=terminal.iterations,
                tool_calls=terminal.tool_calls,
                cancel_reason=terminal.reason,
            )
        case TurnStoppedByLimit():
            return TurnOutcome(
                correlation=correlation,
                status=TurnStatus.STOPPED_BY_LIMIT,
                started_at=started_at,
                finished_at=finished_at,
                iterations=terminal.iterations,
                tool_calls=terminal.tool_calls,
            )


def outcome_for_error(
    error: Exception,
    *,
    correlation: Correlation,
    started_at: datetime,
    finished_at: datetime,
    iterations: int = 0,
    tool_calls: int = 0,
) -> TurnOutcome:
    """把编排层逸出的异常翻译成终态。

    **取消不是失败**：`CancelToken.checkpoint()` 抛出的是 `CANCELLED` 类的 `KaryviaError`，
    把它当成 `FAILED` 会让「用户按了 Ctrl-C」在诊断与 `TurnOutcome` 里长得像「turn 崩了」，
    而  要求四个终态可区分。分类只查 `ErrorCategory`，不认具体错误码——
    `CANCEL_REASON_CODES` 是多对一的，reason 由 `_REASON_BY_CODE` 反查。
    """
    karyvia = as_karyvia(error)
    if karyvia.category is ErrorCategory.CANCELLED:
        return TurnOutcome(
            correlation=correlation,
            status=TurnStatus.CANCELLED,
            started_at=started_at,
            finished_at=finished_at,
            iterations=iterations,
            tool_calls=tool_calls,
            cancel_reason=_REASON_BY_CODE.get(karyvia.code, CancelReason.USER),
        )
    return TurnOutcome(
        correlation=correlation,
        status=TurnStatus.FAILED,
        started_at=started_at,
        finished_at=finished_at,
        iterations=iterations,
        tool_calls=tool_calls,
        error=karyvia,
    )


def outcome_without_engine(
    *,
    correlation: Correlation,
    started_at: datetime,
    finished_at: datetime,
    error: KaryviaError | None = None,
) -> TurnOutcome:
    """没走完 engine 的那几条路径的终态：命令类 turn、准入后被拒、持久化失败。

    走完 engine 的路径一律用 `outcome_from()`，逸出的异常用 `outcome_for_error()`——终态
    该是什么由引擎事件或异常分类说了算。这个函数只覆盖「根本没有引擎事件可查」的情形，
    因此 `iterations` / `tool_calls` 恒为 0。
    """
    return TurnOutcome(
        correlation=correlation,
        status=TurnStatus.FAILED if error is not None else TurnStatus.COMPLETED,
        started_at=started_at,
        finished_at=finished_at,
        error=error,
    )


def as_karyvia(error: Exception) -> KaryviaError:
    """把逸出的异常折成带码的错误。

    只留类型名不留消息：第三方实现的异常文本可能带着凭据（ dispatcher 有哨兵
    测试盯着同一条规则）。
    """
    if isinstance(error, KaryviaError):
        return error
    return KaryviaError(
        ErrorCode.KERNEL_UNEXPECTED,
        "turn 编排期出现未预期的异常。",
        detail={"exception": type(error).__name__},
    )
