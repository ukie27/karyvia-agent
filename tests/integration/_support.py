"""`test_skeleton_turn.py` 的装配：把 `sdk.testing` 的 Fake 接到**真实** Kernel 上。

职责：提供一个 `wire()`——建能力注册表、走一次覆盖解析并冻结、从冻结的注册表派生
Hook / 工具 / Context Provider / 命令索引，再装成 `TurnOrchestrator`；外加几个最小合规的
能力实现（工具、Context Provider、命令）。
不负责：任何断言（那全在 `test_skeleton_turn.py`）；本模块不含 IO、不联网。

**与 `tests/kernel/_orchestrator_support.py` 的分工**：那份夹具在 kernel 边界上也放
Fake（`RecordingToolInvoker` / `RecordingHookDispatcher`），因为它要单独测编排的决定。
这里恰好相反——`ToolExecutor`、`HookRouter`、`Dispatcher`、`SessionScheduler`、
`DedupCache`、`EventBus` 全是生产实现，Fake 只出现在**能力边界**上（模型、会话存储、
工具、Context Provider）。D15 要暴露的正是这条真实装配链上的问题。

**能力经真 Host 注册再由 `*_from(registry)` 取回**，而不是直接把列表塞进
`OrchestratorDeps`：`D14` 定死的四个注册载荷形状（`RegisteredHook` /
`RegisteredContextProvider` / `RegisteredTool` / `RegisteredCommand`）只有走这条路才会被
真正核对。**`D16` 之后这里用的就是生产 Host（`kernel.plugins.CapabilityHost`）**——
`D15` 时它还是手写的 `batch.add(...)`，那是权宜；留着两套注册路径就等于让集成测试证明的
是一条没人会走的路。模型与会话存储仍直接注入 `OrchestratorDeps`：`kernel/turn/` 至今没有
`model_from()` / `session_store_from()` 那样的槽位（取回函数本身 `D16` 已补在
`kernel/plugins/capabilities.py`，接进 deps 是 `D23` 装配根的事）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from nucleamind.contracts import (
    Builtin,
    CancelSignal,
    CapabilityKind,
    CommandInvocation,
    CommandParam,
    CommandResult,
    CommandSpec,
    Concurrency,
    ContextFragment,
    Correlation,
    Disposition,
    EventName,
    FragmentKind,
    FragmentScope,
    InboundMessage,
    InstanceId,
    JsonSchema,
    ModelResponse,
    OutboundMessage,
    RiskLevel,
    RuntimeEvent,
    Sender,
    SessionKey,
    SessionSnapshot,
    SideEffect,
    ToolHandler,
    ToolInvocation,
    ToolResult,
    ToolSpec,
    TrustLevel,
    TurnId,
)
from nucleamind.kernel.observability import EventBus, MemoryRingSink
from nucleamind.kernel.plugins import CapabilityDeclaration, CapabilityHost
from nucleamind.kernel.registry import CapabilityRegistry, ResolutionReport, resolve_into
from nucleamind.kernel.routing import (
    DedupCache,
    Dispatcher,
    RegisteredCommand,
    SessionScheduler,
    build_command_index,
)
from nucleamind.kernel.turn import (
    HookRouter,
    OrchestratorDeps,
    RegisteredContextProvider,
    RegisteredHook,
    RegisteredTool,
    ToolExecutor,
    TurnLimits,
    TurnOrchestrator,
    TurnReceipt,
    bindings_from,
    context_providers_from,
    tools_from,
)
from nucleamind.sdk.testing import (
    FAKE_MODEL_ID,
    FakeModelProvider,
    FakePluginContext,
    InMemorySessionStore,
)

__all__ = [
    "INSTANCE",
    "SESSION_KEY",
    "EchoTool",
    "Skeleton",
    "StaticCommand",
    "StaticContextProvider",
    "command",
    "continued",
    "fragment",
    "handled",
    "inbound",
    "tool",
    "wire",
]

#: 骨架实例的标识。事件与 `Correlation` 上出现它就说明走的是这条集成路径。
INSTANCE = InstanceId("d15-skeleton")

#: 全部用例只用一条会话，因此它的键是确定的——断言持久化结果时直接用它。
SESSION_KEY = SessionKey(channel_id="cli", conversation_id="local")

#: 固定时钟。turn 的时长断言与事件重放都不该依赖真实墙钟。
NOW = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)

_ECHO_SCHEMA: JsonSchema = {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"],
}


# --------------------------------------------------------------------------- 输入与片段


def inbound(content: str, *, message_id: str = "m1", channel_id: str = "cli") -> InboundMessage:
    """一条来自 CLI 的入站消息。"""
    return InboundMessage(
        message_id=message_id,
        instance_id=INSTANCE,
        channel_id=channel_id,
        conversation_id="local",
        sender=Sender(user_id="u1", is_operator=True),
        content=content,
        timestamp=NOW,
    )


def fragment(
    content: str,
    *,
    source: str = "builtin:context-skeleton",
    kind: FragmentKind = FragmentKind.SYSTEM,
    trust: TrustLevel = TrustLevel.SYSTEM,
    priority: int = 0,
) -> ContextFragment:
    return ContextFragment(
        source=source,
        kind=kind,
        content=content,
        priority=priority,
        estimated_tokens=8,
        scope=FragmentScope.SESSION,
        trust=trust,
    )


# ------------------------------------------------------------------------------ 能力实现


class EchoTool:
    """最小合规 `ToolHandler`：把 `path` 回显成内容。

    `before` 是注入的挂钩，取消用例靠它在工具执行到一半时按下取消——这比 `sleep` 出来的
    时间窗确定得多。
    """

    def __init__(self, before: Callable[[], Awaitable[None]] | None = None) -> None:
        #: 执行正文之前跑的协程。公开可写，取消用例在装配之后才知道该取消谁。
        self.before = before
        #: 收到过的全部调用，按顺序。「哪个工具真的跑了」直接读它。
        self.calls: list[ToolInvocation] = []

    async def execute(self, invocation: ToolInvocation, cancel: CancelSignal) -> ToolResult:
        del cancel
        self.calls.append(invocation)
        if self.before is not None:
            await self.before()
        path = invocation.call.arguments.get("path")
        return ToolResult(
            call_id=invocation.call.call_id,
            ok=True,
            content=f"读到了 {path}",
            truncated=False,
            side_effect=SideEffect.NONE,
        )


def tool(
    name: str,
    handler: ToolHandler,
    *,
    concurrency: Concurrency = Concurrency.PARALLEL,
) -> RegisteredTool:
    """把一个 handler 包成可注册的只读工具。

    只读 + `SAFE` 是刻意的：骨架要断言 `side_effect`，而只读工具的结论最明确
    （`NONE`），不会与「工具自己报了什么」混在一起。
    """
    return RegisteredTool(
        spec=ToolSpec(
            name=name,
            description=f"骨架工具 {name}：回显 path 参数。",
            parameters=_ECHO_SCHEMA,
            read_only=True,
            risk=RiskLevel.SAFE,
            concurrency=concurrency,
        ),
        handler=handler,
    )


class StaticContextProvider:
    """最小合规 `ContextProvider`：每轮贡献同一批片段。"""

    def __init__(self, *fragments: ContextFragment) -> None:
        self._fragments = fragments
        self.calls = 0

    async def provide(
        self, snapshot: SessionSnapshot, correlation: Correlation, cancel: CancelSignal
    ) -> tuple[ContextFragment, ...]:
        del snapshot, correlation, cancel
        self.calls += 1
        return self._fragments


class StaticCommand:
    """最小合规 `CommandHandler`：返回一段固定输出并注入一个片段。"""

    def __init__(self, result: CommandResult) -> None:
        self._result = result
        self.calls = 0

    async def handle(self, invocation: CommandInvocation, cancel: CancelSignal) -> CommandResult:
        del invocation, cancel
        self.calls += 1
        return self._result


def command(
    name: str,
    result: CommandResult,
    *,
    description: str = "骨架命令",
    parameters: tuple[CommandParam, ...] = (),
) -> tuple[str, RegisteredCommand]:
    spec = CommandSpec(name=name, description=description, parameters=parameters)
    return name, RegisteredCommand(spec=spec, handler=StaticCommand(result))


def handled(content: str, *fragments: ContextFragment) -> CommandResult:
    """一条已处理完的命令：不进模型（`Disposition.COMMAND_HANDLED`）。"""
    return CommandResult(
        disposition=Disposition.COMMAND_HANDLED, content=content, fragments=fragments
    )


def continued(rewritten: str, *fragments: ContextFragment) -> CommandResult:
    """一条改写输入后继续进模型的命令（`Disposition.COMMAND_CONTINUE`、`CMD-004`）。"""
    return CommandResult(
        disposition=Disposition.COMMAND_CONTINUE,
        rewritten_input=rewritten,
        fragments=fragments,
    )


# ---------------------------------------------------------------------------------- 装配


def _declare(kind: CapabilityKind, name: str) -> CapabilityDeclaration:
    return CapabilityDeclaration(kind=kind, name=name)


def _dispatch(
    host: CapabilityHost[FakePluginContext],
    declaration: CapabilityDeclaration,
    payload: object,
) -> None:
    """按 kind 走对应的注册方法。载荷形状由 Host 自己再构造一遍——这正是要核对的。"""
    if isinstance(payload, RegisteredTool):
        host.register_tool(payload.spec, payload.handler)
    elif isinstance(payload, RegisteredHook):
        host.on(payload.hook, payload.handler)
    elif isinstance(payload, RegisteredContextProvider):
        host.register_context_provider(declaration.name, payload.provider)
    elif isinstance(payload, RegisteredCommand):
        host.register_command(payload.spec, payload.handler)
    else:  # pragma: no cover - 骨架只用这四类
        raise AssertionError(f"未知的注册载荷：{type(payload).__name__}")


@dataclass(slots=True)
class Skeleton:
    """一次装配的全部把手。"""

    orchestrator: TurnOrchestrator
    deps: OrchestratorDeps
    model: FakeModelProvider
    sessions: InMemorySessionStore
    executor: ToolExecutor
    ring: MemoryRingSink
    report: ResolutionReport
    delivered: list[OutboundMessage] = field(default_factory=list)

    async def send(self, content: str, *, message_id: str = "m1") -> TurnReceipt:
        return await self.orchestrator.handle(inbound(content, message_id=message_id))

    def events(self) -> tuple[RuntimeEvent, ...]:
        return self.ring.events()

    def names(self) -> list[EventName]:
        return [event.name for event in self.events()]

    def of_turn(self, turn_id: TurnId) -> tuple[RuntimeEvent, ...]:
        return self.ring.by_turn(turn_id)


def wire(
    responses: Sequence[ModelResponse],
    *,
    tools: Sequence[RegisteredTool] = (),
    hooks: Sequence[tuple[str, RegisteredHook]] = (),
    context: Sequence[tuple[str, RegisteredContextProvider]] = (),
    commands: Sequence[tuple[str, RegisteredCommand]] = (),
    limits: TurnLimits | None = None,
    stream: bool = True,
) -> Skeleton:
    """注册能力 → 解析并冻结 → 派生绑定 → 装成一个可用的 `TurnOrchestrator`。

    注册**走生产 Host**（`CapabilityHost`），因此这条链子顺带核对了 Host 的分派：
    能力名怎么定、载荷是什么形状、声明表是否与实际注册一致，全都由它说了算。

    所有能力走同一个 Host 和 RegistrationBatch；插件故障的隔离语义由消费者统一处理，
    注册载荷不携带提供方自行决定的中断策略。
    """
    registry = CapabilityRegistry()
    entries: list[tuple[CapabilityDeclaration, object]] = []
    for item in tools:
        entries.append((_declare(CapabilityKind.TOOL, item.spec.name), item))
    for _, hook in hooks:
        entries.append((_declare(CapabilityKind.HOOK, hook.hook.value), hook))
    for name, provider in context:
        entries.append((_declare(CapabilityKind.CONTEXT, name), provider))
    for name, registered in commands:
        entries.append((_declare(CapabilityKind.COMMAND, name), registered))

    if entries:
        batch = registry.batch(Builtin())
        host = CapabilityHost(
            batch,
            FakePluginContext("d15-skeleton"),
            declarations=tuple(declaration for declaration, _ in entries),
        )
        for declaration, payload in entries:
            _dispatch(host, declaration, payload)
        host.finish()
        batch.commit()

    report = resolve_into(registry)
    # 冲突就地失败：一个装不起来的实例继续跑下去，后面的断言全部失去意义（`CMD-002`
    # 的「启动期报错」在真实装配里也是这个位置）。
    report.raise_if_failed()

    bus = EventBus(INSTANCE)
    ring = MemoryRingSink()
    bus.subscribe(ring, name="memory-ring")

    executor = ToolExecutor(tools_from(registry))
    router = HookRouter(
        bindings_from(registry),
        on_failure=lambda error: bus.publish(EventName.PLUGIN_FAILED, error=error),
    )
    model = FakeModelProvider(responses)
    sessions = InMemorySessionStore()
    delivered: list[OutboundMessage] = []

    async def deliver(message: OutboundMessage) -> None:
        delivered.append(message)

    deps = OrchestratorDeps(
        instance_id=INSTANCE,
        bus=bus,
        sessions=sessions,
        model=model,
        tools=executor,
        hooks=router,
        dispatcher=Dispatcher(build_command_index(registry)),
        scheduler=SessionScheduler[TurnReceipt](),
        dedup=DedupCache(),
        limits=limits or TurnLimits(),
        model_id=FAKE_MODEL_ID,
        # 模型看得见的工具集与调度用的工具集同源——`ToolExecutor.specs` 是唯一来源。
        tool_specs=executor.specs,
        context_providers=context_providers_from(registry),
        model_info=model.describe(FAKE_MODEL_ID),
        stream=stream,
        deliver=deliver,
    )
    return Skeleton(
        orchestrator=TurnOrchestrator(deps),
        deps=deps,
        model=model,
        sessions=sessions,
        executor=executor,
        ring=ring,
        report=report,
        delivered=delivered,
    )
