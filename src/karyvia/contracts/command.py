"""命令契约：声明、调用与结果。

职责：定义命令声明 `CommandSpec`、一次命令调用 `CommandInvocation`、输入分流的四态
`Disposition` 与命令处理结果 `CommandResult`。
不负责：解析命令前缀与参数、匹配命令名、执行 handler、捕获异常——那些在
`kernel/routing/`与 `builtins/commands_core/`；本模块不含任何 IO。

命令与 Tool 是并列的一等能力，因此它的三件套单独成模块（对标 `tool.py`），
而不是塞进 `capability.py`——后者管的是「能力如何被标识」，不是某一类能力的载荷。

`Disposition` 的四个取值是命令分流的完整状态集，`kernel/routing/dispatcher.py` 直接复用
这一个枚举。分流决策与命令结果共用一套取值，是为了避免出现「dispatcher 认为继续、
handler 认为已处理」这种只能靠映射表调和的分裂。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from .context import ContextFragment
from .errors import ErrorCode, KaryviaError
from .ids import Correlation, validate_identifier
from .message import InboundMessage
from .metadata import EMPTY_METADATA, normalize_metadata

if TYPE_CHECKING:  # pragma: no cover - 仅为注解，运行时不导入，避免与包根成环。
    from . import JsonValue

__all__ = [
    "CommandInvocation",
    "CommandParam",
    "CommandResult",
    "CommandSpec",
    "Disposition",
]

#: 命令名与别名的形状：小写、中划线分隔，**不含前缀字符**。前缀（默认 `/`）是路由的
#: 配置项而不是命令身份的一部分，把它写进名字会让改前缀变成改全部命令声明。
_COMMAND_NAME_PATTERN: Final = re.compile(r"^[a-z][a-z0-9-]*$")

#: 参数名的形状。它会出现在帮助文本里，因此同样限制为可读的小写标识。
_PARAM_NAME_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_-]*$")


@dataclass(frozen=True, slots=True)
class CommandParam:
    """一个命令参数的声明（「参数形式」）。

    只描述位置参数：命令是给人敲的，不是给模型调的（那是 Tool 的活），
    引入具名选项与类型系统只会让 `/help` 的输出比命令本身还长。
    """

    name: str
    description: str
    required: bool = False
    repeated: bool = False

    def __post_init__(self) -> None:
        validate_identifier("command_param.name", self.name)
        if not _PARAM_NAME_PATTERN.match(self.name):
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                "命令参数名形状非法（应为小写标识）。",
                detail={"name": self.name},
            )
        if not self.description:
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                "命令参数必须有说明——它会直接出现在 /help 里。",
                detail={"name": self.name},
            )


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """命令声明：名称、参数形式、说明和操作者要求。"""

    name: str
    description: str
    parameters: tuple[CommandParam, ...] = ()
    operator_only: bool = False
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self._validate_names()
        if not self.description:
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                "命令必须有说明——它是 /help 的唯一内容来源。",
                detail={"name": self.name},
            )
        self._validate_parameters()

    def _validate_names(self) -> None:
        for field, value in (("name", self.name), *(("alias", alias) for alias in self.aliases)):
            validate_identifier(f"command.{field}", value)
            if not _COMMAND_NAME_PATTERN.match(value):
                raise KaryviaError(
                    ErrorCode.INPUT_MALFORMED,
                    "命令名形状非法（应为小写、中划线分隔且不含前缀字符）。",
                    detail={"name": self.name, field: value},
                )
        all_names = (self.name, *self.aliases)
        if len(set(all_names)) != len(all_names):
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                "命令别名不得与命令名或其他别名重复。",
                detail={"name": self.name, "aliases": list(self.aliases)},
            )

    def _validate_parameters(self) -> None:
        names = [param.name for param in self.parameters]
        if len(set(names)) != len(names):
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                "命令参数名必须唯一。",
                detail={"name": self.name, "parameters": names},
            )
        seen_optional = False
        for index, param in enumerate(self.parameters):
            if param.required and seen_optional:
                raise KaryviaError(
                    ErrorCode.INPUT_MALFORMED,
                    "必填参数不得排在可选参数之后——位置参数无法跳过。",
                    detail={"name": self.name, "parameter": param.name},
                )
            seen_optional = seen_optional or not param.required
            if param.repeated and index != len(self.parameters) - 1:
                raise KaryviaError(
                    ErrorCode.INPUT_MALFORMED,
                    "可重复参数必须是最后一个，否则参数边界不可判定。",
                    detail={"name": self.name, "parameter": param.name},
                )

    @property
    def all_names(self) -> tuple[str, ...]:
        """命令名与全部别名。 的启动期冲突检查按这个集合比对。"""
        return (self.name, *self.aliases)


@dataclass(frozen=True, slots=True)
class CommandInvocation:
    """一次命令调用。

    `message` 原样带上，handler 因此能拿到 `sender.is_operator`、附件与平台元数据，
    不需要 Kernel 预先把这些字段一个个转抄进来。`correlation` 是必填的：命令即使不进
    模型也分配 `turn_id` 并发布 turn 事件，可观测性才统一。
    """

    name: str
    args: tuple[str, ...]
    raw_text: str
    message: InboundMessage
    correlation: Correlation

    def __post_init__(self) -> None:
        validate_identifier("command_invocation.name", self.name)
        if not _COMMAND_NAME_PATTERN.match(self.name):
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                "命令名形状非法（应为小写、中划线分隔且不含前缀字符）。",
                detail={"name": self.name},
            )


class Disposition(StrEnum):
    """输入分流的四态。"""

    COMMAND_HANDLED = "command_handled"
    """命令已产生输出，本次输入不进模型。"""

    COMMAND_CONTINUE = "command_continue"
    """命令改写了输入，继续进模型。"""

    MODEL_TURN = "model_turn"
    """未命中任何命令。这是 dispatcher 的结论，handler 永远不会返回它。"""

    REJECTED = "rejected"
    """校验失败或命令执行失败。会话保持可用，进程不退出。"""


@dataclass(frozen=True, slots=True)
class CommandResult:
    """命令处理结果。

    `fragments` 让命令可以向本次 turn 注入上下文（Skill、Prompt 片段的落点）。它是
    `ContextFragment` 而不是裸文本，因此自动受 `trust`、`priority`、预算与敏感级别约束
    ——、 要的「不得凭借文本形式绕过限制」在类型上就已经成立。
    """

    disposition: Disposition
    content: str = ""
    rewritten_input: str | None = None
    fragments: tuple[ContextFragment, ...] = ()
    error: KaryviaError | None = None
    metadata: Mapping[str, JsonValue] = EMPTY_METADATA

    def __post_init__(self) -> None:
        if self.disposition is Disposition.MODEL_TURN:
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "MODEL_TURN 是「未命中命令」的分流结论，不是 handler 可以返回的结果。",
            )
        if (self.disposition is Disposition.REJECTED) != (self.error is not None):
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "error 必须且只能出现在 REJECTED 的命令结果上。",
                detail={"disposition": self.disposition.value},
            )
        if (self.disposition is Disposition.COMMAND_CONTINUE) != (
            self.rewritten_input is not None
        ):
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "rewritten_input 必须且只能出现在 COMMAND_CONTINUE 的命令结果上。",
                detail={"disposition": self.disposition.value},
            )
        if self.disposition is Disposition.COMMAND_HANDLED and not (self.content or self.fragments):
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "已处理的命令必须产生输出或上下文片段，否则用户只会看到毫无反馈。",
            )
        object.__setattr__(
            self, "metadata", normalize_metadata(self.metadata, field="command_result.metadata")
        )
