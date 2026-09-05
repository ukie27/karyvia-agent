"""输入分流：命令与模型 turn（技术方案 §6.3，需求 `KER-006`、`CMD-002`、`CMD-003`）。

职责：在进入 engine 之前决策一次——这条输入是命令（已处理 / 改写后继续）、普通模型
turn，还是该被拒绝；并在启动期把命令名与别名的冲突查出来。
不负责：执行 turn、组装上下文、分配 `turn_id`、发布任何事件、持久化。命令的具体实现是
`builtins/commands_core/`（`D22`）与插件的事。

**本模块不认识 `EventBus`，也不构造 `Correlation`。** `KER-010` 要求命令即使不进模型也
分配 `turn_id` 并发布 turn 事件，那件事整个由 `D14` 的 orchestrator 做：turn 事件只能有
一个发布点，否则「命令类 turn」与「模型类 turn」的事件序列会分别由两处维护，
`OBS-002` 的按序重放随之出现两套口径。dispatcher 只回答「这条输入该怎么走」。

**只有以前缀开头才尝试解析**（§6.3 第一条）：普通聊天文本占绝大多数，为它们做参数解析
是纯浪费；更要紧的是，任何「智能」匹配都会让用户的正常文本偶然变成命令。
"""

from __future__ import annotations

import difflib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Final

from karyvia.contracts import (
    AttachmentRef,
    CancelSignal,
    CapabilityKind,
    CommandHandler,
    CommandInvocation,
    CommandResult,
    CommandSpec,
    Correlation,
    Disposition,
    ErrorCode,
    InboundMessage,
    KaryviaError,
)
from karyvia.kernel.registry import CapabilityRegistry

__all__ = [
    "DEFAULT_COMMAND_PREFIX",
    "CommandIndex",
    "Dispatcher",
    "DispatchOutcome",
    "ParsedCommand",
    "RegisteredCommand",
    "build_command_index",
    "parse_command",
]

#: 命令前缀的默认值。前缀是路由的配置项而不是命令身份的一部分（见 `contracts/command.py`），
#: 与 `kernel/config/schema.py` 的同名常量必须相等。
DEFAULT_COMMAND_PREFIX: Final = "/"


@dataclass(frozen=True, slots=True)
class RegisteredCommand:
    """`CapabilityKind.COMMAND` 的载荷形状：声明 + 执行体。

    registry 的 `payload` 是 `object`，谁注册谁定形状。把这个 dataclass 放在 dispatcher
    而不是 registry，是因为「命令长什么样」是分流的知识，不是登记的知识——registry 只搬运。
    `D16` 的 Host 分派必须注册这个形状，`build_command_index()` 会当场核对。
    """

    spec: CommandSpec
    handler: CommandHandler


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    """一次前缀解析的结果。名字未必存在——是否命中要查 `CommandIndex`。"""

    name: str
    args: tuple[str, ...]
    raw_text: str


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """分流结论。四态之外还带上进模型的最终文本与命令结果。

    `model_input` 只在要进模型时有值：`MODEL_TURN` 时是原文，`COMMAND_CONTINUE` 时是命令
    改写后的文本。让它在另外两态下恒为 `None`，编排层就不可能「顺手」把一条已处理命令的
    原文又送进模型。
    """

    disposition: Disposition
    model_input: str | None = None
    #: 与 `model_input` 同属一条用户消息；命令改写文本时附件仍必须跟着进入模型。
    model_attachments: tuple[AttachmentRef, ...] = ()
    result: CommandResult | None = None
    #: 命中的命令名（规范名，非别名）；未命中命令时为 `None`。
    command_name: str | None = None

    def __post_init__(self) -> None:
        enters_model = self.disposition in (Disposition.MODEL_TURN, Disposition.COMMAND_CONTINUE)
        if enters_model != (self.model_input is not None):
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "model_input 必须且只能出现在会进入模型的分流结论上。",
                detail={"disposition": self.disposition.value},
            )
        if not enters_model and self.model_attachments:
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "不会进入模型的分流结论不该带附件。",
                detail={"disposition": self.disposition.value},
            )
        if (self.disposition is Disposition.MODEL_TURN) and self.result is not None:
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "未命中命令的分流结论不该带命令结果。",
                detail={"disposition": self.disposition.value},
            )

    @property
    def error(self) -> KaryviaError | None:
        """被拒绝的原因；其余情况为 `None`。"""
        return self.result.error if self.result is not None else None


class CommandIndex:
    """命令名与别名到实现的映射，启动期建好即只读。

    别名与命令名在同一个命名空间里：`/h` 是别名还是某个命令的名字，对敲它的人没有区别，
    因此冲突判定也必须在同一个集合上做。
    """

    __slots__ = ("_by_name", "_specs")

    def __init__(self, by_name: Mapping[str, RegisteredCommand]) -> None:
        self._by_name = dict(by_name)
        # 规范名去重后的声明清单，按名字排序——`/help` 的输出不该随注册顺序漂移。
        seen = {entry.spec.name: entry.spec for entry in by_name.values()}
        self._specs = tuple(seen[name] for name in sorted(seen))

    def get(self, name: str) -> RegisteredCommand | None:
        """按名字或别名查一个命令。不存在返回 `None`。"""
        return self._by_name.get(name)

    def names(self) -> tuple[str, ...]:
        """全部可敲的名字（命令名 + 别名），已排序。近似建议按它匹配。"""
        return tuple(sorted(self._by_name))

    def specs(self) -> tuple[CommandSpec, ...]:
        """全部命令声明，按命令名排序，去重。`/help` 的唯一内容来源。"""
        return self._specs

    def __len__(self) -> int:
        return len(self._specs)

    def __iter__(self) -> Iterator[CommandSpec]:
        return iter(self._specs)


def build_command_index(registry: CapabilityRegistry) -> CommandIndex:
    """从已冻结的 registry 建命令索引，**在启动期**把命名冲突查出来（`CMD-002`）。

    registry 的 MULTI_UNIQUE 只保证 `name` 在 kind 内唯一，别名撞车它看不见：两个插件各自
    注册 `/status` 和 `/st`、`/statistics` 和 `/st`，注册阶段一路绿灯，到调用期才由加载顺序
    择一——那正是 `CMD-002` 禁止的。

    **异常约定**：任一名字被两处占用抛 `PLUGIN_REGISTRATION_CONFLICT`；载荷不是
    `RegisteredCommand` 抛 `KERNEL_INVARIANT_VIOLATED`；registry 未冻结由 registry 自己抛。
    """
    by_name: dict[str, RegisteredCommand] = {}
    owner: dict[str, str] = {}
    for registration in registry.of_kind(CapabilityKind.COMMAND):
        payload = registration.payload
        if not isinstance(payload, RegisteredCommand):
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "COMMAND 能力的载荷必须是 RegisteredCommand。",
                detail={"capability": registration.ref.target, "actual": type(payload).__name__},
                capability=registration.ref,
            )
        provider = str(registration.ref.provider)
        for name in payload.spec.all_names:
            if name in owner:
                raise KaryviaError(
                    ErrorCode.PLUGIN_REGISTRATION_CONFLICT,
                    "命令名或别名被两个提供方同时占用，请禁用其中之一或使用覆盖声明。",
                    detail={"name": name, "providers": [owner[name], provider]},
                    capability=registration.ref,
                )
            owner[name] = provider
            by_name[name] = payload
    return CommandIndex(by_name)


def parse_command(content: str, prefix: str = DEFAULT_COMMAND_PREFIX) -> ParsedCommand | None:
    """解析前缀命令。不以前缀开头、或前缀后没有名字时返回 `None`（按普通文本处理）。

    参数按空白切分。**不做引号与转义**：命令是给人敲的位置参数（见 `CommandSpec`），
    需要塞进带空格的长文本时，那个参数就该是最后一个 `repeated` 参数。
    """
    if not content.startswith(prefix):
        return None
    body = content[len(prefix) :]
    if not body or body[:1].isspace():
        return None
    parts = body.split()
    # 命令名统一小写：`/Help` 和 `/help` 对敲的人是同一件事，而命令名的形状本就限定为
    # 小写（`contracts/command.py`），大写形态不可能是任何别的东西。
    return ParsedCommand(name=parts[0].lower(), args=tuple(parts[1:]), raw_text=content)


def _usage(spec: CommandSpec, prefix: str) -> str:
    """一行用法说明，用于参数不符时的可诊断输出。"""
    parts = [
        f"<{param.name}>" if param.required else f"[{param.name}]" for param in spec.parameters
    ]
    tail = "..." if spec.parameters and spec.parameters[-1].repeated else ""
    return " ".join([f"{prefix}{spec.name}", *parts]).strip() + tail


def _rejected(error: KaryviaError, content: str = "") -> DispatchOutcome:
    """把一处拒绝折成分流结论。`CommandResult` 的不变量要求 REJECTED 带 error。"""
    return DispatchOutcome(
        disposition=Disposition.REJECTED,
        result=CommandResult(
            disposition=Disposition.REJECTED,
            content=content or error.user_message,
            error=error,
        ),
    )


class Dispatcher:
    """输入分流器。索引在启动期建好，`dispatch()` 在每条消息上跑一次。

    索引不可变意味着「装了哪些命令」在实例生命周期内不变——这是 `CMD-002` 的另一面：
    如果命令集合能在运行期变，冲突就又变成了「取决于什么时候问」。
    """

    __slots__ = ("_index", "_prefix")

    def __init__(self, index: CommandIndex, *, prefix: str = DEFAULT_COMMAND_PREFIX) -> None:
        """**异常约定**：前缀为空或含空白抛 `KERNEL_INVARIANT_VIOLATED`——空前缀会让每条
        普通消息都被当成命令解析。"""
        if not prefix or any(char.isspace() for char in prefix):
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "命令前缀必须非空且不含空白。",
                detail={"prefix": prefix},
            )
        self._index = index
        self._prefix = prefix

    @property
    def index(self) -> CommandIndex:
        return self._index

    @property
    def prefix(self) -> str:
        return self._prefix

    async def dispatch(
        self,
        message: InboundMessage,
        correlation: Correlation,
        cancel: CancelSignal,
    ) -> DispatchOutcome:
        """决策一次。**永不抛出命令实现的异常**（`CMD-003`）。

        `correlation` 由调用方（`D14`）带着已分配的 `turn_id` 传入：命令处理、模型调用与
        事件发布共用同一个关联标识，单个 turn 才能按 `turn_id` 完整还原（`KER-010`）。

        **异常约定**：只在 Kernel 自身的不变量被破坏时抛。命令 handler 的任何异常都被折成
        `REJECTED` 结果——会话保持可用，进程不退出。
        """
        parsed = parse_command(message.content, self._prefix)
        if parsed is None:
            return DispatchOutcome(
                disposition=Disposition.MODEL_TURN,
                model_input=message.content,
                model_attachments=message.attachments,
            )

        entry = self._index.get(parsed.name)
        if entry is None:
            return _rejected(self._unknown_command(parsed.name))

        rejection = self._check_preconditions(entry.spec, parsed, message)
        if rejection is not None:
            return _rejected(rejection)

        result = await self._invoke(entry, parsed, message, correlation, cancel)
        return DispatchOutcome(
            disposition=result.disposition,
            model_input=result.rewritten_input,
            model_attachments=message.attachments if result.rewritten_input is not None else (),
            result=result,
            command_name=entry.spec.name,
        )

    # ------------------------------------------------------------------ 内部

    def _unknown_command(self, name: str) -> KaryviaError:
        """未命中命令。带一个近似建议——敲错命令是最常见的输入错误。"""
        close = difflib.get_close_matches(name, self._index.names(), n=1, cutoff=0.6)
        hint = (
            f"是否想敲 {self._prefix}{close[0]}？"
            if close
            else f"用 {self._prefix}help 查看可用命令。"
        )
        return KaryviaError(
            ErrorCode.CAPABILITY_MISSING,
            f"没有名为 {self._prefix}{name} 的命令。{hint}",
            detail={"command": name, "suggestion": close[0] if close else None},
        )

    def _check_preconditions(
        self, spec: CommandSpec, parsed: ParsedCommand, message: InboundMessage
    ) -> KaryviaError | None:
        """权限与参数形式的前置校验。通过返回 `None`。

        这些在调用 handler **之前**判：让每个命令自己检查 `is_operator` 和参数个数，
        等于把同一段样板复制到每个插件里，而漏写的那个不会有任何提示。
        """
        if spec.operator_only and not message.sender.is_operator:
            return KaryviaError(
                ErrorCode.PERMISSION_DENIED,
                f"{self._prefix}{spec.name} 只能由实例管理员执行。",
                detail={"command": spec.name},
            )
        required = sum(1 for param in spec.parameters if param.required)
        repeated = bool(spec.parameters) and spec.parameters[-1].repeated
        if len(parsed.args) < required:
            return KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                f"参数不足。用法：{_usage(spec, self._prefix)}",
                detail={"command": spec.name, "given": len(parsed.args), "required": required},
            )
        if not repeated and len(parsed.args) > len(spec.parameters):
            return KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                f"参数过多。用法：{_usage(spec, self._prefix)}",
                detail={
                    "command": spec.name,
                    "given": len(parsed.args),
                    "limit": len(spec.parameters),
                },
            )
        return None

    async def _invoke(
        self,
        entry: RegisteredCommand,
        parsed: ParsedCommand,
        message: InboundMessage,
        correlation: Correlation,
        cancel: CancelSignal,
    ) -> CommandResult:
        """调用 handler，把任何逸出的异常折成 `REJECTED` 结果（`CMD-003`）。

        `BaseException`（取消、`KeyboardInterrupt`）不在此列：那是进程级的停机信号，
        吞掉它会让 Ctrl-C 需要按两次。
        """
        invocation = CommandInvocation(
            name=entry.spec.name,
            args=parsed.args,
            raw_text=parsed.raw_text,
            message=message,
            correlation=correlation,
        )
        try:
            return await entry.handler.handle(invocation, cancel)
        except KaryviaError as exc:
            # 实现方给出的诊断信息比我们能编的更准，原样带上。
            return CommandResult(
                disposition=Disposition.REJECTED, content=exc.user_message, error=exc
            )
        except Exception as exc:
            # **不放异常消息**：第三方命令的异常文本可能带着凭据或路径。类型名足以定位，
            # 完整堆栈应当由 `D14` 记进事件而不是回给用户（`contracts/errors.py` 的口径）。
            error = KaryviaError(
                ErrorCode.KERNEL_UNEXPECTED,
                f"命令 {self._prefix}{entry.spec.name} 执行失败。",
                detail={"command": entry.spec.name, "exception": type(exc).__name__},
                correlation=correlation,
            )
            return CommandResult(
                disposition=Disposition.REJECTED, content=error.user_message, error=error
            )
