"""`test_orchestrator.py` / `test_context_builder.py` / `test_invoker.py` 的夹具。

职责：提供编排层测试用的 Fake（会话存储、Context Provider、工具 handler、事件收集器）
与一个把 `OrchestratorDeps` 装配好的构造器。
不负责：断言任何行为——断言全在各 `test_*.py`；本模块不含 IO、不联网。

与 `_engine_support.py` 分开而不是合并：那份夹具服务的是「engine 的纯循环」，这份服务的是
「有状态、有 IO 的编排」。合并会让两个文件互相牵动，而 AGENTS.md 原则 5 明确说优先重复。
模型 Fake 与契约构造器仍然复用 `_engine_support`——那部分是同一件东西。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import UTC, datetime

from karyvia.contracts import (
    AttachmentRef,
    Builtin,
    CancelSignal,
    CommandParam,
    CommandResult,
    CommandSpec,
    ContextFragment,
    Correlation,
    ErrorCode,
    EventName,
    FragmentKind,
    FragmentScope,
    InboundMessage,
    InstanceId,
    KaryviaError,
    OutboundMessage,
    RuntimeEvent,
    Sender,
    SessionKey,
    SessionMessage,
    SessionSnapshot,
    SideEffect,
    ToolInvocation,
    ToolResult,
    TrustLevel,
)
from karyvia.kernel.observability import EventBus
from karyvia.kernel.routing import (
    CommandIndex,
    DedupCache,
    Dispatcher,
    RegisteredCommand,
    SessionScheduler,
)
from karyvia.kernel.turn import (
    ContextProviderBinding,
    OrchestratorDeps,
    RetryPolicy,
    TurnCompactionPolicy,
    TurnLimits,
    TurnOrchestrator,
    TurnReceipt,
)
from karyvia.sdk.testing import StaticTurnContextCompactor

from ._engine_support import ScriptedProvider

__all__ = [
    "INSTANCE",
    "EventCollector",
    "FailingStore",
    "FakeContextProvider",
    "FakeSessionStore",
    "FakeToolHandler",
    "build",
    "fragment",
    "inbound",
    "make_command",
]

INSTANCE = InstanceId("test-instance")


def inbound(
    content: str = "统计一下文件数",
    *,
    message_id: str = "m1",
    channel_id: str = "cli",
    conversation_id: str = "local",
    is_operator: bool = True,
    attachments: tuple[AttachmentRef, ...] = (),
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        instance_id=INSTANCE,
        channel_id=channel_id,
        conversation_id=conversation_id,
        sender=Sender(user_id="u1", is_operator=is_operator),
        content=content,
        timestamp=datetime(2026, 8, 12, tzinfo=UTC),
        attachments=attachments,
    )


def fragment(
    source: str = "builtin:context-basic",
    *,
    content: str = "工作区是 D:/demo",
    kind: FragmentKind = FragmentKind.RUNTIME,
    trust: TrustLevel = TrustLevel.SYSTEM,
    priority: int = 0,
    tokens: int = 10,
    **extra: object,
) -> ContextFragment:
    return ContextFragment(
        source=source,
        kind=kind,
        content=content,
        priority=priority,
        estimated_tokens=tokens,
        scope=FragmentScope.SESSION,
        trust=trust,
        **extra,  # type: ignore[arg-type]  # boundary: 测试夹具，透传给契约自己校验
    )


# --------------------------------------------------------------------------------- 会话


class FakeSessionStore:
    """内存 `SessionStore`。写入按 session 追加，读出即全量。"""

    def __init__(self, initial: Sequence[SessionMessage] = ()) -> None:
        self.data: dict[str, list[SessionMessage]] = {}
        self.initial = list(initial)
        self.appends: list[tuple[SessionKey, tuple[SessionMessage, ...]]] = []
        self.compactions: list[tuple[SessionKey, int, SessionMessage]] = []
        self.operations: list[str] = []
        self.compacted_through: dict[str, int] = {}

    async def load(self, key: SessionKey) -> SessionSnapshot:
        records = self.data.get(key.storage_id(), list(self.initial))
        return SessionSnapshot(
            session_key=key,
            messages=tuple(records),
            compacted_through=self.compacted_through.get(key.storage_id(), 0),
        )

    async def append(self, key: SessionKey, messages: Sequence[SessionMessage]) -> None:
        self.operations.append("append")
        self.appends.append((key, tuple(messages)))
        bucket = self.data.setdefault(key.storage_id(), list(self.initial))
        bucket.extend(messages)

    async def compact(self, key: SessionKey, through: int, summary: SessionMessage) -> None:
        self.operations.append("compact")
        self.compactions.append((key, through, summary))
        bucket = self.data.setdefault(key.storage_id(), list(self.initial))
        bucket.insert(through, summary)
        self.compacted_through[key.storage_id()] = through

    async def delete(self, key: SessionKey) -> bool:
        return self.data.pop(key.storage_id(), None) is not None

    async def list_keys(self) -> tuple[SessionKey, ...]:
        return ()


class FailingStore(FakeSessionStore):
    """写入必失败的存储（`SES-003`）。"""

    async def append(self, key: SessionKey, messages: Sequence[SessionMessage]) -> None:
        raise KaryviaError(ErrorCode.PERSISTENCE_WRITE_FAILED, "磁盘满了。")


# ------------------------------------------------------------------------------- context


class FakeContextProvider:
    """按需返回片段、抛异常或永远挂住的 `ContextProvider`。"""

    def __init__(
        self,
        fragments: Iterable[ContextFragment] = (),
        *,
        error: BaseException | None = None,
        hang: bool = False,
    ) -> None:
        self._fragments = tuple(fragments)
        self._error = error
        self._hang = hang
        self.calls = 0

    async def provide(
        self, snapshot: SessionSnapshot, correlation: Correlation, cancel: CancelSignal
    ) -> tuple[ContextFragment, ...]:
        del snapshot, correlation, cancel
        self.calls += 1
        if self._error is not None:
            raise self._error
        if self._hang:
            import asyncio

            await asyncio.Event().wait()
        return self._fragments


def binding(
    provider: FakeContextProvider, *, name: str = "basic", priority: int = 0
) -> ContextProviderBinding:
    from karyvia.contracts import Builtin

    return ContextProviderBinding(provider=provider, owner=Builtin(), name=name, priority=priority)


# --------------------------------------------------------------------------------- 工具


class FakeToolHandler:
    """可脚本化的 `ToolHandler`：返回结果、抛异常或按给定协程执行。"""

    def __init__(
        self,
        result: ToolResult | None = None,
        *,
        error: BaseException | None = None,
        body: Callable[[ToolInvocation, CancelSignal], Awaitable[ToolResult]] | None = None,
    ) -> None:
        self._result = result
        self._error = error
        self._body = body
        self.calls: list[ToolInvocation] = []

    async def execute(self, invocation: ToolInvocation, cancel: CancelSignal) -> ToolResult:
        self.calls.append(invocation)
        if self._error is not None:
            raise self._error
        if self._body is not None:
            return await self._body(invocation, cancel)
        return self._result or ToolResult(
            call_id=invocation.call.call_id,
            ok=True,
            content="工具结果",
            truncated=False,
            side_effect=SideEffect.OCCURRED,
        )


# --------------------------------------------------------------------------------- 事件


class EventCollector:
    """订阅 bus 并按顺序记下全部事件。"""

    def __init__(self, bus: EventBus) -> None:
        self.events: list[RuntimeEvent] = []
        bus.subscribe(self.events.append, name="collector")

    @property
    def names(self) -> list[EventName]:
        return [event.name for event in self.events]

    def of(self, name: EventName) -> list[RuntimeEvent]:
        return [event for event in self.events if event.name is name]

    def first(self, name: EventName) -> RuntimeEvent:
        return self.of(name)[0]


# --------------------------------------------------------------------------------- 装配


def make_command(
    name: str,
    handler: object,
    *,
    operator_only: bool = False,
    aliases: tuple[str, ...] = (),
    takes_text: bool = False,
) -> tuple[str, RegisteredCommand]:
    """构造一条已注册命令。`takes_text` 声明一个可重复的尾参，用来接自由文本。"""
    parameters = (
        (CommandParam(name="text", description="自由文本", repeated=True),) if takes_text else ()
    )
    spec = CommandSpec(
        name=name,
        description=f"测试命令 {name}",
        parameters=parameters,
        operator_only=operator_only,
        aliases=aliases,
    )
    return name, RegisteredCommand(spec=spec, handler=handler)  # type: ignore[arg-type]


class ScriptedCommand:
    """按脚本返回 `CommandResult` 的 `CommandHandler`。"""

    def __init__(self, result: CommandResult | BaseException) -> None:
        self._result = result
        self.calls = 0

    async def handle(self, invocation: object, cancel: object) -> CommandResult:
        del invocation, cancel
        self.calls += 1
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


class Harness:
    """一次装配的全部把手，测试直接读它的字段做断言。"""

    def __init__(
        self,
        orchestrator: TurnOrchestrator,
        deps: OrchestratorDeps,
        events: EventCollector,
        store: FakeSessionStore,
        provider: ScriptedProvider,
        delivered: list[OutboundMessage],
    ) -> None:
        self.orchestrator = orchestrator
        self.deps = deps
        self.events = events
        self.store = store
        self.provider = provider
        self.delivered = delivered

    async def send(self, message: InboundMessage | None = None) -> TurnReceipt:
        return await self.orchestrator.handle(message or inbound())


def build(
    provider: ScriptedProvider,
    *,
    tools: object = None,
    tool_specs: Sequence[object] = (),
    hooks: object = None,
    store: FakeSessionStore | None = None,
    commands: dict[str, RegisteredCommand] | None = None,
    context_providers: Sequence[ContextProviderBinding] = (),
    turn_compactor: TurnCompactionPolicy | None = None,
    limits: TurnLimits | None = None,
    retry: RetryPolicy | None = None,
    stream: bool = False,
    clock: Callable[[], datetime] | None = None,
) -> Harness:
    """把一套 Fake 装成 `TurnOrchestrator`。缺省项一律给最简单的真实现。"""
    from ._engine_support import RecordingHookDispatcher, RecordingToolInvoker

    bus = EventBus(INSTANCE)
    events = EventCollector(bus)
    delivered: list[OutboundMessage] = []
    sessions = store if store is not None else FakeSessionStore()

    async def deliver(message: OutboundMessage) -> None:
        delivered.append(message)

    deps = OrchestratorDeps(
        instance_id=INSTANCE,
        bus=bus,
        sessions=sessions,  # type: ignore[arg-type]  # boundary: 结构化子类型，Fake 形状对得上
        model=provider,  # type: ignore[arg-type]
        tools=tools or RecordingToolInvoker(),  # type: ignore[arg-type]
        hooks=hooks or RecordingHookDispatcher(),  # type: ignore[arg-type]
        dispatcher=Dispatcher(CommandIndex(commands or {})),
        scheduler=SessionScheduler[TurnReceipt](),
        dedup=DedupCache(),
        limits=limits or TurnLimits(),
        # 默认「只试一次」：绝大多数用例的脚本恰好写了几条响应，让重试去多要一条会把
        # 它们变成 `模型脚本已耗尽`。要验重试的用例自己传一个策略（`D48`）。
        retry=retry or RetryPolicy(max_attempts=1),
        model_id="fake-model",
        turn_compactor=turn_compactor
        or TurnCompactionPolicy(StaticTurnContextCompactor(), "basic", Builtin()),
        tool_specs=tuple(tool_specs),  # type: ignore[arg-type]
        context_providers=tuple(context_providers),
        stream=stream,
        deliver=deliver,
        clock=clock or (lambda: datetime(2026, 8, 12, tzinfo=UTC)),
    )
    return Harness(TurnOrchestrator(deps), deps, events, sessions, provider, delivered)
