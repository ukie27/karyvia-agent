"""编排的装配面与产物：`OrchestratorDeps`、`TurnReceipt`、`EventTap` 与 Engine 四槽依赖。

职责：声明 orchestrator 需要哪些协作者、一次 `handle()` 交回什么，保留每轮
`model.request_started` 的发布时机，并把协作者装成 engine 的四个槽（`engine_deps()`）。
不负责：任何流程（`orchestrator.py`）、任何 IO。

**与流程分成两个模块**有两个理由，都不是「文件太长」：`orchestrator.py` 的 ≤500 行是
技术方案 §6.2 写死的硬约束，而装配方（`D23` 的 wiring）只需要这里的三样东西、用不到编排
细节——让它 import 一个不含流程的模块，「谁依赖谁」在 import 清单上就是可读的。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from karyvia.contracts import (
    ErrorCode,
    EventName,
    HookContext,
    HookName,
    HookOutcome,
    InstanceId,
    JsonValue,
    KaryviaError,
    ModelInfo,
    ModelMessage,
    ModelProvider,
    ModelRequest,
    OutboundMessage,
    SessionStore,
    StreamState,
    ToolSpec,
    TurnId,
    TurnOutcome,
)
from karyvia.kernel.observability import EventBus
from karyvia.kernel.routing import DedupCache, Dispatcher, SessionScheduler

from .compaction import SessionCompactionTracker
from .context_builder import DEFAULT_CONTEXT_PROVIDER_TIMEOUT_MS, ContextProviderBinding
from .deps import EngineDeps, HookDispatcher, ToolInvoker
from .limits import BudgetLedger, TurnLimits
from .memory import MemoryRecall
from .request_size import TokenAccounting
from .retry import RetryingModel, RetryPolicy
from .transcript import TurnState
from .turn_compaction import TurnCompactingModel, TurnCompactionPolicy

__all__ = [
    "DROPPED_ATTACHMENTS_KEY",
    "EventTap",
    "OrchestratorDeps",
    "TurnReceipt",
    "emit_outbound",
    "engine_deps",
    "utc_now",
]

#: 终帧 metadata 里「有几个附件因为超上界没带上」的键（`D47`）。Channel 可以据此加一句
#: 说明；没有它时不该猜——`attachments` 的长度只说明发了几个，不说明丢了几个。
DROPPED_ATTACHMENTS_KEY: Final = "attachments_dropped"

#: 契约要求 `DELTA` / `FINAL` 有正文；`CANCELLED` / `FAILED` 允许空正文，由 Channel 按
#: `EDG-304` 附加标记后呈现。空正文的前两种直接不发，而不是硬塞一个占位符。
_NEEDS_CONTENT: Final = (StreamState.DELTA, StreamState.FINAL)


def utc_now() -> datetime:
    """默认时钟。注入可替换——测试与重放都不该依赖真实墙钟。"""
    return datetime.now(UTC)


async def emit_outbound(
    state: TurnState,
    content: str,
    stream_state: StreamState,
    deliver: Callable[[OutboundMessage], Awaitable[None]] | None,
    *,
    reasoning: bool = False,
    final: bool = False,
) -> OutboundMessage | None:
    """产出并投递一条出站消息，返回它（不该发时返回 `None`）。

    寻址三件套（`channel_id` / `conversation_id` / `turn_id`）从 `Correlation` 取，
    Channel 因此不必维护自己的 Session 映射（`MSG-006`）；契约会当场校验它们与
    `session_key` 一致。

    **附件只挂在终帧上**（`final=True`，`D47`）：中间帧是同一段正文的分片，把附件挂上去
    等于让 Channel 收到 N 份同样的附件。终帧包含 `CANCELLED` / `FAILED`——已经生成出来的
    文件该交给用户，而契约允许这两种状态空正文。**只有附件、没有正文的终帧照发**：
    契约的「内容与附件不能同时为空」本来就是二选一。

    **刻意不加 `attachments` 形参**：它已经收 `state`，加一个参数会让唯一的调用方
    （`orchestrator._emit`）跟着长一行，而那个文件贴着 500 行上限。
    """
    attachments = tuple(state.attachments) if final else ()
    if not content and stream_state in _NEEDS_CONTENT and not attachments:
        return None
    metadata: dict[str, JsonValue] = {"reasoning": True} if reasoning else {}
    if final and state.dropped_attachments:
        # 撞上 `MAX_ATTACHMENTS` 时**说出来**：一条「有几张图没发出来」的消息，
        # 比用户自己数出来强。放在 metadata 而不是事件里，是因为该看到它的是收件人。
        metadata[DROPPED_ATTACHMENTS_KEY] = state.dropped_attachments
    key = state.correlation.session_key
    message = OutboundMessage(
        session_key=key,
        channel_id=key.channel_id,
        conversation_id=key.conversation_id,
        turn_id=state.correlation.turn_id,
        content=content,
        attachments=attachments,
        stream_state=stream_state,
        metadata=metadata,
    )
    if not final:
        state.emitted.append(message)
    if deliver is not None:
        await deliver(message)
    return message


@dataclass(frozen=True, slots=True)
class TurnReceipt:
    """一次 `handle()` 的结论。

    `admitted=False` 覆盖两种情形：去重命中（`duplicate_of` 有值）与调度器拒绝
    （`error` 有值）。两者都没有 turn 终态，因此 `outcome` 为 `None`——用一个
    `TurnStatus` 硬凑会让「这次到底跑没跑」变得不可判定。
    """

    turn_id: TurnId
    admitted: bool
    outcome: TurnOutcome | None = None
    error: KaryviaError | None = None
    duplicate_of: TurnId | None = None
    content: str = ""
    messages: tuple[OutboundMessage, ...] = ()


@dataclass(frozen=True, slots=True)
class OrchestratorDeps:
    """装配一次实例所需的全部协作者（`D23` 的 wiring 按这张表接线）。

    槽位比 `EngineDeps` 的四个多得多，这是分层的直接结果：engine 之所以能只有四个槽，
    正是因为「有状态、有 IO 的部分」全在这一层。
    """

    instance_id: InstanceId
    bus: EventBus
    sessions: SessionStore
    model: ModelProvider
    tools: ToolInvoker
    hooks: HookDispatcher
    dispatcher: Dispatcher
    scheduler: SessionScheduler[TurnReceipt]
    dedup: DedupCache
    limits: TurnLimits
    model_id: str
    turn_compactor: TurnCompactionPolicy
    tool_specs: tuple[ToolSpec, ...] = ()
    context_providers: tuple[ContextProviderBinding, ...] = ()
    model_info: ModelInfo | None = None
    stream: bool = True
    scope: str = "default"
    context_provider_timeout_ms: int = DEFAULT_CONTEXT_PROVIDER_TIMEOUT_MS
    deliver: Callable[[OutboundMessage], Awaitable[None]] | None = None
    #: 长期记忆的召回（`D44`）。`None` = 没有 kernel 侧召回，这也是默认——配置里没写
    #: `memory.provider` 时装配根不装它。见 `memory.py` 的模块 docstring。
    memory: MemoryRecall | None = None
    #: 模型请求的重试策略（`D48`）。默认值就是开箱行为：可重试的失败重发两次、空回复
    #: 当故障。见 `retry.py` 的模块 docstring。
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    #: 一个实例共享一份计量状态；Provider usage 为同 Session 的后续请求留下真实前缀锚点。
    token_accounting: TokenAccounting = field(default_factory=TokenAccounting)
    clock: Callable[[], datetime] = utc_now


def engine_deps(
    deps: OrchestratorDeps,
    ledger: BudgetLedger,
    request: ModelRequest,
    session_compaction: SessionCompactionTracker | None,
) -> EngineDeps:
    """把编排层的协作者装成 engine 的四个槽。

    `model` 先套 `RetryingModel`，再套 Turn 压缩包装器；Engine 对两者都无感知。

    `ledger` 交给重试是为了不睡过 turn 的死线、以及判断这条 turn 跑过工具没有；它与
    engine 用同一本账，因此两边看到的是同一份记账。
    """
    retrying = RetryingModel(deps.model, deps.retry, deps.bus, ledger=ledger)
    model_info = deps.model_info or deps.model.describe(deps.model_id)
    protected_user = _current_user(request.messages)
    return EngineDeps(
        model=TurnCompactingModel(
            retrying,
            deps.turn_compactor,
            deps.bus,
            ledger,
            budget=deps.limits.resolve_context_budget(model_info),
            accounting=deps.token_accounting,
            protected_user=protected_user,
            model_info=model_info,
            session_compaction=session_compaction,
        ),
        tools=deps.tools,
        hooks=EventTap(deps.hooks, deps.bus),
        limits=deps.limits,
    )


def _current_user(messages: tuple[ModelMessage, ...]) -> ModelMessage:
    for message in reversed(messages):
        if message.role.value == "user":
            return message
    raise KaryviaError(
        ErrorCode.KERNEL_INVARIANT_VIOLATED,
        "进入 Engine 的请求缺少当前用户输入。",
    )


class EventTap:
    """Engine 每轮分发 `before_model_request` 时发布原有的模型请求事件。"""

    def __init__(self, inner: HookDispatcher, bus: EventBus) -> None:
        self._inner = inner
        self._bus = bus

    async def dispatch(self, context: HookContext) -> HookOutcome:
        if context.hook is HookName.BEFORE_MODEL_REQUEST and context.request is not None:
            self._bus.publish(
                EventName.MODEL_REQUEST_STARTED,
                correlation=context.correlation,
                payload={
                    "model_id": context.request.model_id,
                    "messages": len(context.request.messages),
                    "tools": len(context.request.tools),
                    "purpose": "agent",
                },
            )
        return await self._inner.dispatch(context)
