"""默认的确定性 Turn Context Compactor。

职责：从最旧单元开始选择连续前缀，并生成有界、可审计的文本摘要。不负责请求大小的最终
判定、工具结构校验、Session 持久化或模型调用；这些分别属于 Kernel 机制和未来可替换策略。
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from nucleamind.contracts import (
    CancelSignal,
    CompactionModel,
    ErrorCode,
    NucleaError,
    Role,
    TurnCompactionRequest,
    TurnCompactionResult,
    TurnContextUnit,
    TurnContextUnitKind,
)

__all__ = ["BasicTurnContextCompactor"]

_EXCERPT_CHARS = 160
_SUMMARY_OVERHEAD_TOKENS = 32


class BasicTurnContextCompactor:
    """无网络、无状态的默认压缩策略。"""

    async def compact(
        self,
        request: TurnCompactionRequest,
        model: CompactionModel,
        cancel: CancelSignal,
    ) -> TurnCompactionResult:
        cancel.raise_if_requested()
        covered_tokens = 0
        for through, unit in enumerate(request.units, start=1):
            covered_tokens += unit.estimated_tokens
            summary = _summary(request.units[:through])
            if (
                request.estimated_tokens
                - covered_tokens
                + _summary_tokens(summary)
                <= request.target_tokens
            ):
                return TurnCompactionResult(through_units=through, summary=summary)

        minimal = f"已压缩当前 Turn 中最早的 {len(request.units)} 个上下文单元。"
        fixed_tokens = request.estimated_tokens - sum(
            unit.estimated_tokens for unit in request.units
        )
        if fixed_tokens + _summary_tokens(minimal) <= request.target_tokens:
            return TurnCompactionResult(
                through_units=len(request.units),
                summary=minimal,
            )
        raise NucleaError(
            ErrorCode.INPUT_TOO_LARGE,
            "固定请求内容超过上下文预算，默认压缩策略无法继续缩减。",
            detail={
                "estimated_tokens": request.estimated_tokens,
                "target_tokens": request.target_tokens,
            },
        )


def _summary(units: Sequence[TurnContextUnit]) -> str:
    lines = [f"已压缩当前 Turn 中最早的 {len(units)} 个上下文单元："]
    lines.extend(_unit_line(unit) for unit in units)
    return "\n".join(lines)


def _unit_line(unit: TurnContextUnit) -> str:
    if unit.kind is TurnContextUnitKind.TOOL_EXCHANGE:
        assistant = unit.messages[0]
        names = ", ".join(call.name for call in assistant.tool_calls)
        results = " | ".join(
            _excerpt(message.content)
            for message in unit.messages[1:]
            if message.role is Role.TOOL and message.content
        )
        suffix = f"；结果摘录：{results}" if results else ""
        return f"- 工具往返：{names or '未知工具'}{suffix}"
    message = unit.messages[-1]
    label = "续写" if unit.kind is TurnContextUnitKind.CONTINUATION else message.role.value
    return f"- {label}：{_excerpt(message.content)}"


def _excerpt(content: str) -> str:
    compact = " ".join(content.split())
    if len(compact) <= _EXCERPT_CHARS:
        return compact or "（无正文）"
    return f"{compact[:_EXCERPT_CHARS]}…"


def _summary_tokens(summary: str) -> int:
    return math.ceil(len(summary) / 3) + _SUMMARY_OVERHEAD_TOKENS
