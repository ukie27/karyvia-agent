"""Turn 事件：engine 产出的 9 个不可变事件与「异常属于哪个终态」的唯一映射（技术方案 §6.2）。

职责：定义 `run_turn()` 事件流的全部事件类型、`TurnEvent` / `TerminalEvent` 两个封闭联合，
以及把逸出异常翻译成终态事件的 `terminal_from_error()`。
不负责：分配 `sequence`、读时钟、认识 `InstanceId`、把事件翻译成 `contracts.RuntimeEvent`——
那三样属于 EventBus 与 Orchestrator；本模块不含 IO，也不 import
`contracts.events`。

**为什么是封闭联合而不是「一个类 + kind 枚举」**：唯一消费者是 Orchestrator，
而它必须处理每一种事件（每种都对应一次持久化或一条 `OutboundMessage`）。联合类型让
`match event:` 在 basedpyright 严格模式下拿到穷尽性检查——将来新增一个事件而 orchestrator
忘了处理，是类型检查期报错而不是线上少发一条消息。契约层的 `ModelChunk` 选了 kind + 可选载荷
的形态，因为它的 5 种载荷同构且小；engine 事件的载荷是 5 种完全不同的契约对象，不同构。

**为什么放 kernel 而不是 contracts**：三条各自独立的理由。其一，`tests/contracts/
test_field_traceability.py` 要求每个契约类型对上需求 §10 的某一节，而这些事件是 §6.2 那次
两层拆分的产物，是实现结构而不是需求资产。其二，进 `contracts.__all__` 就会被字面量快照
变成永久公开表面（`NFR-104`），等于承诺 engine 的循环结构今后不变；插件观察 turn 的正规路径
是 `RuntimeEvent` 与 9 个 Hook。其三，`TurnCancelled.checkpoint` 与 `TurnStoppedByLimit.breach`
引用的是 kernel 的 `Checkpoint` 与 `LimitBreach`，搬进零依赖的 contracts 就得把那两个也搬，
而它们是 Kernel 机制，不是跨层契约。

**事件流的不变量**（由 `engine.py` 的结构保证，并由测试断言）：恰好以一个 `TerminalEvent`
结尾，终态之后不再有任何事件；单个事件一旦 yield，其内容不再变更（全部 frozen）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, TypeAlias

from karyvia.contracts import (
    CancelReason,
    ErrorCategory,
    ErrorCode,
    KaryviaError,
    ModelMessage,
    ModelResponse,
    ToolCall,
    ToolResult,
)

from .cancel import Checkpoint
from .limits import LimitBreach

__all__ = [
    "TERMINAL_EVENTS",
    "ModelReasoningDelta",
    "ModelResponseCompleted",
    "ModelTextDelta",
    "TerminalEvent",
    "ToolCallCompleted",
    "ToolCallStarted",
    "ToolDisposition",
    "TurnCancelled",
    "TurnCompleted",
    "TurnEvent",
    "TurnFailed",
    "TurnStoppedByLimit",
    "terminal_from_error",
]


class ToolDisposition(StrEnum):
    """一次工具调用最终是怎么了。

    显式登记而不是让消费方去嗅 `result.error.code`：冻结的事件名里有
    `tool.call_completed` / `tool.call_blocked` / `tool.call_failed` 三个，Orchestrator 要在其间
    选一个。用错误码当控制流用，等于让「换一个错误码」变成「换一条事件路由」。
    """

    EXECUTED = "executed"
    """真的执行了。结果是工具自己给的，无论 ok 与否。"""

    BLOCKED = "blocked"
    """被 `before_tool_call` Hook 拦下，未执行（`side_effect=NONE`）。"""

    UNKNOWN_TOOL = "unknown_tool"
    """模型报了一个不在本次请求 `tools` 里的名字，未执行（`side_effect=NONE`）。"""

    SKIPPED = "skipped"
    """取消发生时尚未轮到执行，未执行（`side_effect=NONE`，技术方案 §6.4 检查点 5）。"""


@dataclass(frozen=True, slots=True)
class ModelTextDelta:
    """一个正文分片。`iteration` 从 1 起，同一轮内按到达顺序发出。"""

    iteration: int
    text: str


@dataclass(frozen=True, slots=True)
class ModelReasoningDelta:
    """一个推理分片。与正文分开是因为它不属于答案，渲染与持久化策略都不同。"""

    iteration: int
    text: str


@dataclass(frozen=True, slots=True)
class ModelResponseCompleted:
    """本轮模型响应已完整（流式则已折叠完毕）。

    `message` 是 engine **真正追加进下一轮请求**的那条 assistant 消息；本轮就此收尾
    （没有 tool_calls）时为 `None`，因为那条消息不进下一轮。带上它是为了让 orchestrator
    持久化的内容与模型下一轮看到的内容同源——两边各造一次，重放历史得到的请求就和本轮不同。
    """

    iteration: int
    response: ModelResponse
    message: ModelMessage | None = None


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    """一次工具调用即将发起。同一批并发工具的 `ToolCallStarted` 连续发出。"""

    iteration: int
    call: ToolCall


@dataclass(frozen=True, slots=True)
class ToolCallCompleted:
    """一次工具调用有了结果。并发批内按**完成顺序**发出，与消息顺序无关。

    `message` 同样是 engine 追加进下一轮的那条 tool 消息（已按 `tool_result_max_bytes`
    截断、空结果已占位）。消息进入 `messages` 的顺序恒等于 `tool_calls` 的顺序，
    与本事件的发出顺序**不同**——前者是模型的输入契约，后者是给 UI 的实时反馈。
    """

    iteration: int
    call: ToolCall
    result: ToolResult
    message: ModelMessage
    disposition: ToolDisposition


@dataclass(frozen=True, slots=True)
class TurnCompleted:
    """终态：模型给出了不带工具调用的回答（`TurnStatus.COMPLETED`）。

    `truncated=True` 表示模型因 `MAX_TOKENS` 停止、答案被截断（`EDG-304`）。
    此时出站消息的 `stream_state` 会被 orchestrator 改为 `CANCELLED` 以触发标记。
    """

    response: ModelResponse
    iterations: int
    tool_calls: int
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class TurnFailed:
    """终态：`TurnStatus.FAILED`。

    engine 自身不向上抛异常——没有正常事件序列的中断会让 orchestrator 无从善后
    （技术方案 §6.2 的 engine 不变量第二条）。工具失败**不**走这里：那是回给模型的
    一条 tool 消息，turn 继续。
    """

    error: KaryviaError
    iterations: int
    tool_calls: int


@dataclass(frozen=True, slots=True)
class TurnCancelled:
    """终态：`TurnStatus.CANCELLED`。

    `checkpoint` 指出停在 6 个命名检查点的哪一个（`raise_if_requested()` 而非
    `checkpoint()` 抛出时为 `None`），orchestrator 据此决定善后动作——技术方案 §6.4
    的表格是逐检查点写的。
    """

    reason: CancelReason
    iterations: int
    tool_calls: int
    checkpoint: Checkpoint | None = None


@dataclass(frozen=True, slots=True)
class TurnStoppedByLimit:
    """终态：`TurnStatus.STOPPED_BY_LIMIT`（`KER-005`：正常终止并返回可诊断结果）。

    `breach.describe()` 就是那份「可诊断说明」，可直接进 `OutboundMessage`。

    Orchestrator 将它翻译成独立的 `turn.stopped_by_limit`，不能用 `turn.completed` 承载；
    观察者必须能区分模型自然结束和预算中止（`EDG-304`）。

    `turn_timeout_ms` 越界**不**产生本事件：它的 `terminal_status` 是 `CANCELLED`
    （见 `limits.LIMIT_OUTCOMES`），走 `TurnCancelled(reason=TIMEOUT)`。
    """

    breach: LimitBreach
    iterations: int
    tool_calls: int


TerminalEvent: TypeAlias = TurnCompleted | TurnFailed | TurnCancelled | TurnStoppedByLimit
TurnEvent: TypeAlias = (
    ModelTextDelta
    | ModelReasoningDelta
    | ModelResponseCompleted
    | ToolCallStarted
    | ToolCallCompleted
    | TerminalEvent
)

#: 运行期判定「这是不是终态」。类型联合在运行期不可 `isinstance`，而
#: 「事件流恰好以一个终态结尾」这条不变量必须能被测试和 orchestrator 实际检查。
TERMINAL_EVENTS: Final[tuple[type, ...]] = (
    TurnCompleted,
    TurnFailed,
    TurnCancelled,
    TurnStoppedByLimit,
)

#: `CANCEL_REASON_CODES` 的逆向查表。它是多对一（`TIMEOUT`/`BUDGET` 共用一个码），
#: 因此只在拿不到令牌真实 reason 时兜底，`BUDGET` 优先——预算取消比超时更常见，
#: 而真正需要区分的调用点都持有令牌。
_REASON_BY_CODE: Final[dict[ErrorCode, CancelReason]] = {
    ErrorCode.CANCELLED_BY_USER: CancelReason.USER,
    ErrorCode.CANCELLED_BY_BUDGET: CancelReason.BUDGET,
    ErrorCode.CANCELLED_BY_SHUTDOWN: CancelReason.SHUTDOWN,
}

_CHECKPOINT_VALUES: Final[frozenset[str]] = frozenset(where.value for where in Checkpoint)


def terminal_from_error(
    error: Exception,
    *,
    reason: CancelReason | None,
    iterations: int,
    tool_calls: int,
) -> TerminalEvent:
    """把 engine 循环里逸出的异常翻译成终态事件。

    这是「`deps` 回调抛出的异常全部转成终态事件、engine 自身不向上抛」的唯一落点，
    因此 `engine.py` 里只需要一处 `except`。`reason` 传令牌的真实取消原因（没有则 `None`）：
    错误码到原因是多对一的，令牌才是权威。
    """
    if isinstance(error, KaryviaError):
        if error.category is ErrorCategory.CANCELLED:
            return TurnCancelled(
                reason=reason or _REASON_BY_CODE.get(error.code, CancelReason.USER),
                iterations=iterations,
                tool_calls=tool_calls,
                checkpoint=_checkpoint_from_detail(error),
            )
        return TurnFailed(error=error, iterations=iterations, tool_calls=tool_calls)
    # 非 KaryviaError 一律视作 Kernel 内部意外：能力实现抛裸异常本身就是契约违规
    # （`protocols.py` 每个方法都写了异常约定），把它包起来才有码可查、才已脱敏。
    return TurnFailed(
        error=KaryviaError(
            ErrorCode.KERNEL_UNEXPECTED,
            "turn 执行期出现未预期的异常。",
            detail={"exception": type(error).__name__, "message": str(error)},
        ),
        iterations=iterations,
        tool_calls=tool_calls,
    )


def _checkpoint_from_detail(error: KaryviaError) -> Checkpoint | None:
    """从 `CancelToken.checkpoint()` 写入的 `detail` 里还原检查点。

    取不到就是 `None`：`raise_if_requested()` 与能力实现自己抛的取消都没有这个字段，
    强行猜一个检查点会让 orchestrator 按错误的表格行去善后。
    """
    raw = error.detail.get("checkpoint")
    if isinstance(raw, str) and raw in _CHECKPOINT_VALUES:
        return Checkpoint(raw)
    return None
