"""模型请求到可压缩单元的纯投影。

职责：保护当前用户输入与系统指令，并把工具调用及其结果组成不可拆分单元。
不负责：调用压缩器、预算决策、摘要延续或 Session 持久化。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from nucleamind.contracts import (
    ErrorCode,
    ModelMessage,
    NucleaError,
    Role,
    TurnContextUnit,
    TurnContextUnitKind,
)

from .request_size import TokenAccounting, estimate_messages_tokens

__all__ = ["Projection", "project", "project_units", "protected_user_index"]

_CURRENT_USER_CHANGED = "before_model_request 删除或改写了当前用户输入。"
_ORPHAN_TOOL_RESULT = "工具结果缺少紧邻的 assistant 调用声明。"
_TOOL_EXCHANGE_MISMATCH = "assistant 工具调用与 tool 结果不完整匹配。"


@dataclass(frozen=True, slots=True)
class Span:
    unit: TurnContextUnit
    start: int
    stop: int


@dataclass(frozen=True, slots=True)
class Projection:
    messages: tuple[ModelMessage, ...]
    spans: tuple[Span, ...]

    @property
    def units(self) -> tuple[TurnContextUnit, ...]:
        return tuple(span.unit for span in self.spans)


def project_units(
    messages: Sequence[ModelMessage], protected_user: ModelMessage
) -> tuple[TurnContextUnit, ...]:
    """公开纯投影入口；结构非法时立即失败，不把修复责任交给插件。"""
    return project(tuple(messages), protected_user, None).units


def project(
    messages: tuple[ModelMessage, ...],
    protected_user: ModelMessage,
    accounting: TokenAccounting | None,
) -> Projection:
    protected_index = protected_user_index(messages, protected_user)
    spans: list[Span] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.role is Role.SYSTEM or index == protected_index:
            index += 1
            continue
        if message.tool_calls:
            stop = index + 1 + len(message.tool_calls)
            exchange = messages[index:stop]
            _validate_exchange(exchange)
            _append_span(
                spans, exchange, index, stop, TurnContextUnitKind.TOOL_EXCHANGE, accounting
            )
            index = stop
            continue
        if message.role is Role.TOOL:
            raise _structure_error(_ORPHAN_TOOL_RESULT)
        kind = (
            TurnContextUnitKind.CONTINUATION
            if index > protected_index and message.role is Role.ASSISTANT
            else TurnContextUnitKind.BASE
        )
        _append_span(spans, (message,), index, index + 1, kind, accounting)
        index += 1
    return Projection(messages, tuple(spans))


def protected_user_index(messages: tuple[ModelMessage, ...], protected_user: ModelMessage) -> int:
    matches = [index for index, message in enumerate(messages) if message == protected_user]
    if not matches:
        raise _structure_error(_CURRENT_USER_CHANGED)
    return matches[-1]


def _validate_exchange(messages: tuple[ModelMessage, ...]) -> None:
    assistant = messages[0]
    results = messages[1:]
    expected = [call.call_id for call in assistant.tool_calls]
    actual = [message.tool_call_id for message in results if message.role is Role.TOOL]
    if (
        len(results) != len(expected)
        or len(actual) != len(expected)
        or set(actual) != set(expected)
    ):
        raise _structure_error(_TOOL_EXCHANGE_MISMATCH)


def _append_span(
    spans: list[Span],
    messages: tuple[ModelMessage, ...],
    start: int,
    stop: int,
    kind: TurnContextUnitKind,
    accounting: TokenAccounting | None,
) -> None:
    unit = TurnContextUnit(
        unit_id=f"unit-{len(spans) + 1}",
        kind=kind,
        messages=messages,
        estimated_tokens=(
            accounting.estimate_messages(messages)
            if accounting is not None
            else estimate_messages_tokens(messages)
        ),
    )
    spans.append(Span(unit, start, stop))


def _structure_error(message: str) -> NucleaError:
    return NucleaError(ErrorCode.KERNEL_INVARIANT_VIOLATED, message)
