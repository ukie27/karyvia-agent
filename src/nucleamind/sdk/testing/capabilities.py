"""能力边界上的 Fake：工具、Channel、Context、Compactor、Memory、CLI 与插件上下文。

职责：为 `TOOL` / `CHANNEL` / `CONTEXT` / `MEMORY` / `CLI_ENTRY` 与 `PluginContext` 各提供
一个**最小合规**的参考实现，供契约测试基类与插件作者直接使用。
不负责：任何生产行为；也不覆盖 `fakes.py` 已有的模型与会话存储两类。

**为什么与 `fakes.py` 分开**：`fakes.py` 已有 276 行，`sdk/` 的单文件上限是 800，本模块
再塞进去会逼近上限；更要紧的是两者的定位不同——`fakes.py` 里的
`FakeModelProvider` / `InMemorySessionStore` 是**可编脚本的**测试替身（有状态、可断言
调用序列），这里的几个是**参考实现**：它们存在的意义是「契约基类有一个跑得通的样例」，
`ToolContract` 与 `ChannelContract` 自 `D05` 发布至今都没有这样的东西，`D15` 与
`tests/sdk/` 因此各自私下写了一份。两个入口都由 `sdk.testing` 统一导出。

`FakePluginContext` 只提供不涉及外部资源的参考实现。文件、网络与进程访问仍需插件测试
自行注入窄替身，避免测试在不知情时执行真实副作用。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from logging import Logger, getLogger
from pathlib import Path

from nucleamind.contracts import (
    CancelReason,
    CancelSignal,
    CommandSpec,
    CompactionModel,
    CompactionRequest,
    CompactionResult,
    ContextFragment,
    Correlation,
    ErrorCode,
    EventName,
    FragmentKind,
    FragmentScope,
    InboundMessage,
    JsonValue,
    NucleaError,
    OutboundMessage,
    RiskLevel,
    SecretStr,
    SessionKey,
    SessionSnapshot,
    SideEffect,
    ToolInvocation,
    ToolResult,
    ToolSpec,
    TrustLevel,
    TurnCompactionRequest,
    TurnCompactionResult,
    TurnId,
)

__all__ = [
    "ECHO_SPEC",
    "EchoTool",
    "FakeCliEntry",
    "FakeInstanceView",
    "FakeMemoryProvider",
    "FakePluginContext",
    "FakeTurnControl",
    "NullChannel",
    "RecordingEventSubscriber",
    "StaticContextCompactor",
    "StaticContextProvider",
    "StaticTurnContextCompactor",
]


# ------------------------------------------------------------------------------------ 工具

#: `EchoTool` 的声明。只读 + `SAFE`，因此 `ToolContract` 的「只读工具不产生副作用」
#: 那条用例在它身上是可断言的。
ECHO_SPEC = ToolSpec(
    name="test.echo",
    description="原样返回 text 参数。",
    parameters={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
    read_only=True,
    risk=RiskLevel.SAFE,
)


class EchoTool:
    """最小合规 `ToolHandler`：回显 `text`，坏参数返回失败结果而**不是**抛异常。

    「失败是一等结果」是 `ToolHandler` 契约的核心（逸出的异常会让 Kernel 只能把副作用
    标成 `UNKNOWN`），因此参考实现必须把这条演示出来。
    """

    def __init__(self) -> None:
        #: 收到过的全部调用，按顺序。「这个工具真的跑了吗」直接读它。
        self.calls: list[ToolInvocation] = []

    async def execute(self, invocation: ToolInvocation, cancel: CancelSignal) -> ToolResult:
        del cancel
        self.calls.append(invocation)
        text = invocation.call.arguments.get("text")
        if not isinstance(text, str):
            return ToolResult(
                call_id=invocation.call.call_id,
                ok=False,
                content="text 必须是字符串。",
                truncated=False,
                side_effect=SideEffect.NONE,
                error=NucleaError(ErrorCode.INPUT_MALFORMED, "text 必须是字符串。"),
            )
        return ToolResult(
            call_id=invocation.call.call_id,
            ok=True,
            content=text,
            truncated=False,
            side_effect=SideEffect.NONE,
        )


# -------------------------------------------------------------------------- Context Provider


class StaticContextProvider:
    """最小合规 `ContextProvider`：每轮贡献同一批片段。

    默认那一条是 `trust=SYSTEM`——它是唯一能进系统指令位置的凭据（`kind` 不参与判定），
    参考实现演示的正是这条。
    """

    def __init__(self, *fragments: ContextFragment) -> None:
        self._fragments = fragments or (
            ContextFragment(
                source="builtin:test",
                kind=FragmentKind.SYSTEM,
                content="你是一个测试助手。",
                priority=0,
                estimated_tokens=8,
                scope=FragmentScope.SESSION,
                trust=TrustLevel.SYSTEM,
            ),
        )
        #: 被调用过几次。
        self.calls = 0

    async def provide(
        self, snapshot: SessionSnapshot, correlation: Correlation, cancel: CancelSignal
    ) -> tuple[ContextFragment, ...]:
        del snapshot, correlation, cancel
        self.calls += 1
        return self._fragments


# ------------------------------------------------------------------------- Context Compactor


class StaticContextCompactor:
    """最小可脚本化 `ContextCompactor`：每次返回同一个结果或 `None`。"""

    def __init__(self, result: CompactionResult | None = None) -> None:
        self.result = result
        self.requests: list[CompactionRequest] = []

    async def compact(
        self, request: CompactionRequest, cancel: CancelSignal
    ) -> CompactionResult | None:
        cancel.raise_if_requested()
        self.requests.append(request)
        return self.result


class StaticTurnContextCompactor:
    """最小可脚本化 `TurnContextCompactor`：记录请求并返回固定结果。"""

    def __init__(self, result: TurnCompactionResult | None = None) -> None:
        self.result = result or TurnCompactionResult(through_units=1, summary="已压缩。")
        self.requests: list[TurnCompactionRequest] = []

    async def compact(
        self,
        request: TurnCompactionRequest,
        model: CompactionModel,
        cancel: CancelSignal,
    ) -> TurnCompactionResult:
        del model
        cancel.raise_if_requested()
        self.requests.append(request)
        return self.result


# ---------------------------------------------------------------------------------- Channel


class NullChannel:
    """最小合规 `Channel`：可启停、可投递，`receive()` 产出预置的入站消息后即结束。

    `stop()` 可重复调用（契约要求它不抛），`deliver()` 只是记录——断言「发出去了什么」
    直接读 `delivered`。
    """

    def __init__(self, channel_id: str = "fake", inbox: Iterable[InboundMessage] = ()) -> None:
        self._channel_id = channel_id
        self._inbox = tuple(inbox)
        #: 投递出去的全部消息，按顺序。
        self.delivered: list[OutboundMessage] = []
        self.started = 0
        self.stopped = 0

    @property
    def channel_id(self) -> str:
        return self._channel_id

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1

    def receive(self) -> AsyncIterator[InboundMessage]:
        return self._receive()

    async def _receive(self) -> AsyncIterator[InboundMessage]:
        for message in self._inbox:
            yield message

    async def deliver(self, message: OutboundMessage) -> None:
        self.delivered.append(message)


# ----------------------------------------------------------------------------------- Memory


class FakeMemoryProvider:
    """内存版 `MemoryProvider`。`recall()` 按插入顺序返回，即「相关度顺序」。

    刻意不做任何检索：契约只要求「按相关度顺序返回」，而一个假的相关度模型只会让继承者
    去模仿它。子串匹配足以让调用方的代码路径跑通。
    """

    def __init__(self) -> None:
        self._records: dict[str, ContextFragment] = {}
        self._next = 0

    async def remember(self, fragment: ContextFragment, cancel: CancelSignal) -> str:
        del cancel
        self._next += 1
        record_id = f"mem-{self._next}"
        self._records[record_id] = fragment
        return record_id

    async def recall(
        self,
        query: str,
        *,
        scope: FragmentScope,
        limit: int,
        cancel: CancelSignal,
    ) -> Mapping[str, ContextFragment]:
        del cancel
        hits = {
            record_id: fragment
            for record_id, fragment in self._records.items()
            if fragment.scope is scope and query in fragment.content
        }
        return dict(list(hits.items())[:limit])

    async def forget(self, record_id: str) -> bool:
        return self._records.pop(record_id, None) is not None


# -------------------------------------------------------------------------------- CLI 入口


class FakeCliEntry:
    """最小合规 `CliEntry`：记录 argv 并返回预置的退出码。"""

    def __init__(self, exit_code: int = 0) -> None:
        self._exit_code = exit_code
        #: 每次 `run()` 收到的 argv，按顺序。
        self.invocations: list[tuple[str, ...]] = []

    async def run(self, argv: Sequence[str], cancel: CancelSignal) -> int:
        cancel.raise_if_requested()
        self.invocations.append(tuple(argv))
        return self._exit_code


# --------------------------------------------------------------------------- PluginContext


class RecordingEventSubscriber:
    """记录订阅关系的 `EventSubscriber`。重复订阅同一 `(event, handler)` 视为一次。"""

    def __init__(self) -> None:
        self.subscriptions: list[tuple[EventName, object]] = []

    def subscribe(self, event: EventName, handler: object) -> None:
        if (event, handler) not in self.subscriptions:
            self.subscriptions.append((event, handler))


class FakeInstanceView:
    """最小合规 `InstanceView`（`D22`）：五份数据都由构造参数直接给出。

    刻意**不**内建任何默认内容：一个自带三条假命令的视图会让 `/help` 的测试在什么都没
    注册时也「看起来对」。空实例的正确形态就是空——`EDG-101` 要求那也是可用的。
    """

    def __init__(
        self,
        *,
        commands: Sequence[CommandSpec] = (),
        capabilities: Mapping[str, JsonValue] | None = None,
        plugins: Sequence[Mapping[str, JsonValue]] = (),
        config_document: Mapping[str, JsonValue] | None = None,
        snapshots: Mapping[str, SessionSnapshot] | None = None,
    ) -> None:
        self._commands = tuple(commands)
        self._capabilities = dict(capabilities or {})
        self._plugins = tuple(plugins)
        self._config = dict(config_document or {})
        #: 按 `storage_id()` 索引——那是已发布的编码契约，让 Fake 也走一遍它。
        self._snapshots = dict(snapshots or {})

    def commands(self) -> tuple[CommandSpec, ...]:
        return self._commands

    def set_commands(self, commands: Sequence[CommandSpec]) -> None:
        """事后填入命令清单。

        **不是便利方法，是在模拟真实的时序**：命令索引要等全部插件注册完才建得出来，而
        `PluginContext` 必须在 `setup()` **之前**就交给插件。生产实现因此持有一个 callable
        （`runtime/introspection.py::KernelInstanceView.commands_source`）；测试里装配完再
        填一次是同一件事的最小形态。
        """
        self._commands = tuple(commands)

    def capabilities(self) -> Mapping[str, JsonValue]:
        return dict(self._capabilities)

    def plugins(self) -> tuple[Mapping[str, JsonValue], ...]:
        return self._plugins

    def config_document(self) -> Mapping[str, JsonValue]:
        return dict(self._config)

    async def session_snapshot(self, key: SessionKey) -> SessionSnapshot:
        """不存在的会话返回**空快照**而不是抛错，与 `SessionStore.load()` 一致。"""
        existing = self._snapshots.get(key.storage_id())
        return existing if existing is not None else SessionSnapshot(session_key=key)


class FakeTurnControl:
    """最小合规 `TurnControl`（`D22`）：记下每次取消请求，供断言。

    `cancel_turn()` 对未知 `turn_id` 返回 `False` 而**不抛**——「已经结束了」与「失败了」
    对用户是同一个好结果，对诊断不是。请求仍然记进 `requested`：`/cancel` 是否真的把
    请求发出去了，与目标当时在不在跑是两件事。
    """

    def __init__(self, live: Sequence[TurnId] = ()) -> None:
        self._live = list(live)
        #: 按顺序记录 `(turn_id, reason)`，包括打在已结束 turn 上的那些。
        self.requested: list[tuple[TurnId, CancelReason]] = []

    def live_turns(self) -> tuple[TurnId, ...]:
        return tuple(self._live)

    def cancel_turn(self, turn_id: TurnId, reason: CancelReason = CancelReason.USER) -> bool:
        self.requested.append((turn_id, reason))
        if turn_id not in self._live:
            return False
        self._live.remove(turn_id)
        return True


class FakePluginContext:
    """最小合规 `PluginContext`；资源 I/O 需要测试显式注入自己的替身。"""

    def __init__(
        self,
        plugin_id: str = "fake",
        *,
        config: Mapping[str, JsonValue] | None = None,
        state_dir: Path | None = None,
        secrets: Mapping[str, str] | None = None,
        instance: FakeInstanceView | None = None,
        turns: FakeTurnControl | None = None,
    ) -> None:
        self._plugin_id = plugin_id
        self._config = dict(config or {})
        self._state_dir = state_dir or Path(".")
        self._secrets = dict(secrets or {})
        self._events = RecordingEventSubscriber()
        #: `D22`：诊断视图与 turn 控制面默认给空实现，而不是留 `None` 让属性访问炸掉。
        self._instance = instance if instance is not None else FakeInstanceView()
        self._turns = turns if turns is not None else FakeTurnControl()
        #: 经 `spawn_task()` 登记过的任务名，按顺序。不真的起协程——「谁的任务」可判定
        #: 才是这个 API 存在的理由，而那件事记下名字就够断言了。
        self.tasks: list[str] = []
        self.start_actions: list[Callable[[], Awaitable[None]]] = []
        self.cleanup_actions: list[Callable[[], Awaitable[None]]] = []

    @property
    def plugin_id(self) -> str:
        return self._plugin_id

    @property
    def config(self) -> Mapping[str, JsonValue]:
        return dict(self._config)

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    @property
    def logger(self) -> Logger:
        return getLogger(f"nucleamind.plugin.{self._plugin_id}")

    @property
    def events(self) -> RecordingEventSubscriber:
        return self._events

    @property
    def instance(self) -> FakeInstanceView:
        return self._instance

    @property
    def turns(self) -> FakeTurnControl:
        return self._turns

    def spawn_task(self, coro: object, *, name: str) -> None:
        del coro
        self.tasks.append(name)

    def on_start(self, action: Callable[[], Awaitable[None]]) -> None:
        self.start_actions.append(action)

    def add_cleanup(self, action: Callable[[], Awaitable[None]]) -> None:
        self.cleanup_actions.append(action)

    @property
    def fs(self) -> object:
        raise NotImplementedError("FakePluginContext 不提供真实文件访问。")

    @property
    def net(self) -> object:
        raise NotImplementedError("FakePluginContext 不提供真实出网。")

    @property
    def shell(self) -> object:
        raise NotImplementedError("FakePluginContext 不提供真实子进程。")

    def secret(self, name: str) -> SecretStr:
        """取一个测试凭据；缺失时与生产上下文使用同一错误码。"""
        if name not in self._secrets:
            raise NucleaError(
                ErrorCode.CONFIG_SECRET_MISSING,
                "配置里没有该凭据。",
                detail={"plugin": self._plugin_id, "secret": name},
            )
        return SecretStr(self._secrets[name])
