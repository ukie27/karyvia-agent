"""上下文压缩契约：插件输入与输出的纯数据形状。

职责：定义持久化 `ContextCompactor` 与临时 `TurnContextCompactor` 的请求、单元与结果。
不负责：决定何时压缩、校验或持久化结果、选择具体插件实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .ids import Correlation
from .model import ModelMessage, ModelRequest
from .session import SessionSnapshot

__all__ = [
    "CompactionRequest",
    "CompactionResult",
    "TurnCompactionRequest",
    "TurnCompactionResult",
    "TurnContextUnit",
    "TurnContextUnitKind",
]


@dataclass(frozen=True, slots=True)
class CompactionRequest:
    """一次持久化上下文压缩请求。"""

    snapshot: SessionSnapshot
    target_tokens: int
    correlation: Correlation
    user_input: str


@dataclass(frozen=True, slots=True)
class CompactionResult:
    """插件建议的压缩水位与摘要正文。"""

    through: int
    content: str


class TurnContextUnitKind(StrEnum):
    """当前 Turn 内可被整体替换的消息单元。"""

    BASE = "base"
    TOOL_EXCHANGE = "tool_exchange"
    CONTINUATION = "continuation"


@dataclass(frozen=True, slots=True)
class TurnContextUnit:
    """不可拆分的临时上下文单元；工具调用及其全部结果共用一个单元。"""

    unit_id: str
    kind: TurnContextUnitKind
    messages: tuple[ModelMessage, ...]
    estimated_tokens: int


@dataclass(frozen=True, slots=True)
class TurnCompactionRequest:
    """一次模型请求发送前的临时上下文压缩请求。"""

    request: ModelRequest
    units: tuple[TurnContextUnit, ...]
    target_tokens: int
    estimated_tokens: int
    correlation: Correlation


@dataclass(frozen=True, slots=True)
class TurnCompactionResult:
    """用一段摘要替换可压缩单元连续前缀的建议。"""

    through_units: int
    summary: str
