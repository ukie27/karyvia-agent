"""`AgentInstance`：一个已装好的实例的运行与停止。

职责：启动长生命周期服务（Channel + 每个 Channel 的入站泵）、把出站消息路由回对应
Channel、跑 CLI 入口、按相反顺序停止一切并释放实例锁。
不负责：装配它（`bootstrap.py`）、解析配置（`kernel/config/`）、执行 turn
（`kernel/turn/`）、解析 argv（`runtime/cli/`）。

**Channel 泵是「CLI 也是 Channel」这条设计的兑现点**：入站消息从
`channel.receive()` 来、经 `orchestrator.handle()`、出站经 `deliver` 路由回
`channel.deliver`。CLI 与未来任何平台走的是同一段代码，没有第二条路径。

**泵只负责流量，不调度 Session**：每条消息按接收顺序同步登记到 Orchestrator，再并发等待
结果；同一 Session 的 `queue` / `merge` / `reject` 只由 `SessionScheduler` 决定。泵只保留
每条 Channel 的总在途上限，避免平台突发产生无界任务。

**被拒的 turn 也要有回音**：去重命中或队列拒绝时 `TurnReceipt.admitted=False`，
orchestrator 不会发终态出站消息（那条 turn 从未开始）。泵因此自己合成一条
`stream_state=FAILED` 的出站消息——否则 CLI 会永远等一个不会到来的终态。
合成的仍是 `OutboundMessage`，不是绕过契约的旁路。总在途上限的拒绝走同一条合成路径；
它没有进入 Orchestrator，因此发的是 `instance.input_dropped` 而不是 turn 事件。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from karyvia.contracts import (
    CancelSignal,
    CapabilityKind,
    Channel,
    CliEntry,
    Correlation,
    ErrorCode,
    EventName,
    HookContext,
    HookName,
    InboundMessage,
    InstanceId,
    KaryviaError,
    OutboundMessage,
    Plugin,
    SessionKey,
    StreamState,
    TurnId,
)
from karyvia.kernel.config import InstanceLayout, InstanceLock, LoadedConfig
from karyvia.kernel.observability import Diagnostics, EventBus
from karyvia.kernel.plugins import (
    DEFAULT_STOP_TIMEOUT_MS,
    LoadOutcome,
    PluginLifecycle,
    PluginPhase,
    StopAction,
    stop_plugins,
    units_for,
)
from karyvia.kernel.registry import CapabilityRegistry, ResolutionReport
from karyvia.kernel.turn import (
    OrchestratorDeps,
    ToolExecutor,
    TurnOrchestrator,
    TurnReceipt,
)

from .plugin_context import PluginRuntime, RuntimePluginContext

#: `TurnReceipt` 从这里再导出一次：`embed/` 只能 import `contracts/` 与 `runtime/`（`R5`），
#: 而一次 `submit()` 的返回值类型在 `kernel/turn/`。转发比让门面用 `object` 诚实得多。
__all__ = [
    "DEFAULT_CHANNEL_CONCURRENCY",
    "AgentInstance",
    "Closer",
    "TurnReceipt",
    "delivery_error",
    "outbound_router",
]

#: 停止时要跑的一件收尾事。用 callable 而不是一张「谁要关」的类型表：
#: 模型的 `aclose()`、sink 的 `close()` 与锁的 `release()` 没有共同接口，
#: 为它们发明一个只会多出一层。
Closer = Callable[[], Awaitable[None]]

#: 单条 Channel 同时尚未收口的消息数。它是 Runtime 入站流量的总量护栏，不参与
#: Session 的 queue / merge / reject 策略。
DEFAULT_CHANNEL_CONCURRENCY = 64

#: 在途 Turn 收到业务取消后正常持久化与发终态的等待预算。字段可在构造时注入，未来若需要
#: 配置化，组装根只需贯通数值，不必改停止算法。
DEFAULT_TURN_SHUTDOWN_GRACE_MS = 5_000


class _InstancePhase(StrEnum):
    """实例内部生命周期。失败启动会先进入 FAILED，再由同一停止路径收敛到 STOPPED。"""

    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    FAILED = "failed"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass(slots=True)
class AgentInstance:
    """一个装好的实例。`bootstrap()` 是它唯一的构造者。"""

    instance_id: InstanceId
    layout: InstanceLayout
    config: LoadedConfig
    bus: EventBus
    diagnostics: Diagnostics
    registry: CapabilityRegistry
    report: ResolutionReport
    deps: OrchestratorDeps
    orchestrator: TurnOrchestrator
    cli_entry: CliEntry
    channels: tuple[tuple[str, Channel], ...] = ()
    outcomes: tuple[LoadOutcome, ...] = ()
    contexts: tuple[RuntimePluginContext, ...] = ()
    #: 只有这些由全局插件目录发现的提供方享受启动故障隔离；内建错误仍使实例启动失败。
    external_plugin_ids: frozenset[str] = frozenset()
    #: 每个提供方的生命周期，与 `contexts` 同序。装配根按加载结果把它们置于
    #: `LOADED` 或 `FAILED`；`start()` / `stop()` 在这里继续推进。
    lifecycles: tuple[PluginLifecycle, ...] = ()
    #: 单个插件的停止预算（配置 `plugins.stop_timeout_ms`）。
    stop_timeout_ms: int = DEFAULT_STOP_TIMEOUT_MS
    turn_shutdown_grace_ms: int = DEFAULT_TURN_SHUTDOWN_GRACE_MS
    #: 单条 Channel 的总在途消息上限，来自配置 `routing.channel_concurrency`。
    channel_concurrency: int = DEFAULT_CHANNEL_CONCURRENCY
    runtime: PluginRuntime = field(default_factory=PluginRuntime)
    lock: InstanceLock | None = None
    closers: tuple[Closer, ...] = ()
    _pumps: list[asyncio.Task[None]] = field(default_factory=list, init=False)
    _channel_tasks: dict[str, set[asyncio.Task[None]]] = field(default_factory=dict, init=False)
    _active_channels: list[Channel] = field(default_factory=list, init=False)
    _phase: _InstancePhase = field(default=_InstancePhase.CREATED, init=False)
    _lifecycle_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    # ------------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        """启动插件与入站泵；外部插件失败隔离，内建失败走实例原子停止。"""
        async with self._lifecycle_lock:
            if self._phase is _InstancePhase.RUNNING:
                return
            if self._phase is not _InstancePhase.CREATED:
                raise KaryviaError(
                    ErrorCode.KERNEL_INVARIANT_VIOLATED,
                    "实例只能从已装配状态启动。",
                    detail={"phase": self._phase.value},
                )
            self._phase = _InstancePhase.STARTING
            try:
                await self._start_plugins()
                for channel_id, channel in self.channels:
                    lifecycle = self._channel_lifecycle(channel_id)
                    if lifecycle is not None and lifecycle.phase is PluginPhase.FAILED:
                        continue
                    try:
                        await channel.start()
                    except Exception as exc:  # noqa: BLE001 - 外部 Channel 在此隔离
                        error = _as_karyvia(exc)
                        if lifecycle is not None:
                            lifecycle.fail(error)
                        self.bus.publish(
                            EventName.PLUGIN_FAILED,
                            payload={
                                "plugin": (
                                    lifecycle.plugin_id
                                    if lifecycle is not None
                                    else self._channel_owner(channel_id)
                                ),
                                "phase": "start",
                            },
                            error=error,
                        )
                        await self._safe(channel.stop())
                        if lifecycle is None:
                            if error is exc:
                                raise
                            raise error from exc
                        continue
                    self._active_channels.append(channel)
                    self._channel_tasks[channel_id] = set()
                    self._pumps.append(
                        asyncio.create_task(
                            self._run_channel(channel_id, channel), name=f"pump:{channel_id}"
                        )
                    )
                await self.deps.hooks.dispatch(HookContext(HookName.INSTANCE_READY))
                self.bus.publish(
                    EventName.INSTANCE_READY,
                    payload={
                        "channels": [channel_id for channel_id, _ in self.channels],
                        "capabilities": len(self.report.active),
                    },
                )
                self._phase = _InstancePhase.RUNNING
            except BaseException:
                self._phase = _InstancePhase.FAILED
                await self._stop_locked()
                raise

    async def run_cli(self, argv: Sequence[str], cancel: CancelSignal) -> int:
        """跑内建（或覆盖了它的）CLI 入口。**它拥有进程**，返回值即退出码。"""
        if self._phase is not _InstancePhase.RUNNING:
            await self.start()
        return await self.cli_entry.run(argv, cancel)

    async def submit(self, message: InboundMessage) -> TurnReceipt:
        """直接投一条入站消息（`embed/` 与测试用）。

        **不是绕过 Channel 的近路**：它走的是 `orchestrator.handle()`，与泵完全同一个
        入口，只是消息不从某个平台来。出站消息按同一条 `deliver` 路由。
        """
        return await self.orchestrator.handle(message)

    async def stop(self) -> None:
        """按相反顺序停止。**约定不抛**：一条失败的收尾不该盖住其余收尾。"""
        async with self._lifecycle_lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        """持有生命周期锁时执行唯一的停止路径。"""
        if self._phase in {_InstancePhase.STOPPING, _InstancePhase.STOPPED}:
            return
        self._phase = _InstancePhase.STOPPING
        self.bus.publish(EventName.INSTANCE_STOPPING)
        self.orchestrator.begin_shutdown()

        for channel in reversed(self._active_channels):
            await self._safe(channel.stop())
        self._active_channels.clear()
        for pump in self._pumps:
            pump.cancel()
        if self._pumps:
            await asyncio.gather(*self._pumps, return_exceptions=True)
        self._pumps.clear()
        forced = await self.orchestrator.finish_shutdown(
            timeout_ms=self.turn_shutdown_grace_ms
        )
        channel_tasks = [task for tasks in self._channel_tasks.values() for task in tasks]
        if forced is not None:
            for task in channel_tasks:
                task.cancel()
                task.add_done_callback(_consume_task_result)
        elif channel_tasks:
            await asyncio.gather(*channel_tasks, return_exceptions=True)
        self._channel_tasks.clear()

        # 实例级 shutdown 观察者看到的是「不再有 Turn 使用插件资源」的时刻。
        await self._safe(self.deps.hooks.dispatch(HookContext(HookName.INSTANCE_SHUTDOWN)))
        self._report_orphans()

        # 插件按逆加载序逐个停止，每个插件拥有独立超时预算。
        await self._stop_plugins()

        self.bus.publish(EventName.INSTANCE_STOPPED)
        for closer in self.closers:
            await self._safe(closer())
        if self.lock is not None:
            self.lock.release()
        self._phase = _InstancePhase.STOPPED

    # ------------------------------------------------------------------ 内部

    async def _start_plugins(self) -> None:
        """按加载拓扑激活插件；失败只影响自身及其依赖者。"""
        lifecycles = {item.plugin_id: item for item in self.lifecycles}
        for context in self.contexts:
            lifecycle = lifecycles.get(context.plugin_id)
            if lifecycle is None or lifecycle.phase is not PluginPhase.LOADED:
                continue
            blocked_by = tuple(
                dependency
                for dependency in lifecycle.dependencies
                if (
                    dependency_lifecycle := lifecycles.get(dependency)
                ) is not None
                and dependency_lifecycle.phase is PluginPhase.FAILED
            )
            if blocked_by:
                error = KaryviaError(
                    ErrorCode.PLUGIN_LOAD_FAILED,
                    "插件依赖未能启动，因此跳过激活。",
                    detail={
                        "plugin_id": context.plugin_id,
                        "failed_dependencies": blocked_by,
                    },
                )
                lifecycle.fail(error)
                self.bus.publish(
                    EventName.PLUGIN_FAILED,
                    payload={"plugin": context.plugin_id, "phase": "start"},
                    error=error,
                )
                continue
            try:
                await context.activate()
            except Exception as exc:  # noqa: BLE001 - 转成稳定诊断后跳过本插件
                error = _as_karyvia(exc)
                lifecycle.fail(error)
                self.bus.publish(
                    EventName.PLUGIN_FAILED,
                    payload={"plugin": context.plugin_id, "phase": "start"},
                    error=error,
                )
                if context.plugin_id not in self.external_plugin_ids:
                    if error is exc:
                        raise
                    raise error from exc
                continue
            lifecycle.advance(PluginPhase.STARTED)
            self.bus.publish(EventName.PLUGIN_ACTIVATED, payload={"plugin": context.plugin_id})

    @property
    def active_channel_ids(self) -> tuple[str, ...]:
        """实际启动成功并已接入消息泵的 Channel。"""
        return tuple(
            channel_id
            for channel_id, channel in self.channels
            if channel in self._active_channels
        )

    def _channel_owner(self, channel_id: str) -> str:
        """从冻结报告回查 Channel 的提供方，供启动失败诊断使用。"""
        binding = next(
            (
                item
                for item in self.report.active
                if item.kind is CapabilityKind.CHANNEL and item.name == channel_id
            ),
            None,
        )
        return str(binding.provider) if binding is not None else channel_id

    def _channel_lifecycle(self, channel_id: str) -> PluginLifecycle | None:
        """外部 Channel 所属插件的生命周期；内建 Channel 没有外部插件状态。"""
        binding = next(
            (
                item
                for item in self.report.active
                if item.kind is CapabilityKind.CHANNEL and item.name == channel_id
            ),
            None,
        )
        if binding is None or not isinstance(binding.provider, Plugin):
            return None
        return next(
            (
                lifecycle
                for lifecycle in self.lifecycles
                if lifecycle.plugin_id == binding.provider.plugin_id
            ),
            None,
        )

    async def _stop_plugins(self) -> None:
        """按逆加载序停掉每个提供方，并把结果发成事件。

        **停止顺序不在这里算**：`contexts` 是装配根按 `wire_all()` 的
        manifest 顺序追加的，而那个顺序对外部插件就是 `LoadPlan.order`（内建在前）。
        `units_for()` 把它翻过来，因此「被依赖者后停」与「被依赖者先起」共用同一个序，
        没有第二次拓扑排序。

        每个插件各有独立超时：一个停不下来的插件只让自己记一条
        `TIMEOUT_PLUGIN_STOP`，不会连累后面的插件或扣住进程退出。
        """
        actions: dict[str, StopAction] = {ctx.plugin_id: ctx.shutdown for ctx in self.contexts}
        units = units_for(
            tuple(ctx.plugin_id for ctx in self.contexts),
            actions,
            {lifecycle.plugin_id: lifecycle for lifecycle in self.lifecycles},
        )
        for outcome in await stop_plugins(units, timeout_ms=self.stop_timeout_ms):
            if outcome.error is None:
                self.bus.publish(
                    EventName.PLUGIN_DEACTIVATED, payload={"plugin": outcome.plugin_id}
                )
                continue
            self.bus.publish(
                EventName.PLUGIN_FAILED,
                payload={"plugin": outcome.plugin_id, "timed_out": outcome.timed_out},
                error=outcome.error,
            )

    def _report_orphans(self) -> None:
        """停止时报告孤儿工具任务（留下的那条）。

        孤儿是「超时后连宽限期都没等回来」的工具调用，它的副作用是 `UNKNOWN`。实例正在
        关闭，这是最后一个能说出「有几次调用可能还在改外部世界」的时刻——不说，那条信息
        就随进程一起没了。**没有孤儿时不发事件**：一条恒定出现的 `0` 只会让真正有孤儿的
        那次淹在噪声里。

        `dropped` 一并报出：「表里没有」与「被挤掉了」是两个不同的结论（`invoker.py`）。

        **`isinstance` 是必要的**：`OrchestratorDeps.tools` 的类型是 `ToolInvoker`，而孤儿表
        不在那个协议里——第三方执行器可以完全没有这个概念。给协议加一个成员会逼每个实现
        都编一张空表出来，那比这里少报一次更糟。
        """
        executor = self.deps.tools
        if not isinstance(executor, ToolExecutor):
            return
        orphans = executor.orphans
        dropped = executor.orphans_dropped
        if not orphans and not dropped:
            return
        self.bus.publish(
            EventName.PLUGIN_FAILED,
            payload={
                "reason": "tool_orphans",
                "count": len(orphans),
                "dropped": dropped,
                "orphans": [
                    {
                        "tool": task.tool,
                        "call_id": task.call_id,
                        "turn_id": task.turn_id,
                        "grace_ms": task.grace_ms,
                    }
                    for task in orphans
                ],
            },
        )

    async def _run_channel(self, channel_id: str, channel: Channel) -> None:
        """按接收顺序登记消息；Session 调度与结果等待彼此分离。"""
        tasks = self._channel_tasks[channel_id]
        async for message in channel.receive():
            if len(tasks) >= self.channel_concurrency:
                error = KaryviaError(
                    ErrorCode.INPUT_SESSION_BUSY,
                    "这条 Channel 的在途消息已达上限，请稍后重试。",
                    detail={"reason": "channel_saturated", "limit": self.channel_concurrency},
                )
                await self._dropped(channel, message, error)
                continue
            try:
                submission = self.orchestrator.submit(message)
            except Exception as exc:  # noqa: BLE001 - 一条坏消息不能终止整条 Channel
                self._pump_failure(exc)
                continue
            task = asyncio.create_task(
                self._settle_channel_message(channel, message, submission),
                name=f"channel-input:{channel_id}:{message.message_id}",
            )
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    async def _settle_channel_message(
        self,
        channel: Channel,
        message: InboundMessage,
        submission: asyncio.Future[TurnReceipt],
    ) -> None:
        """等待一条已登记消息，并为未准入结果回音。"""
        try:
            receipt = await submission
        except Exception as exc:  # noqa: BLE001 - 失败隔离与旧泵一致
            self._pump_failure(exc)
            return
        if not receipt.admitted:
            await self._echo(channel, _rejection(message, receipt))

    def _pump_failure(self, exc: Exception) -> None:
        """一条消息处理失败。只记不抛，不能带走整条 Channel 泵。"""
        self.bus.publish(EventName.PLUGIN_FAILED, error=_as_karyvia(exc))

    async def _dropped(
        self, channel: Channel, message: InboundMessage, error: KaryviaError
    ) -> None:
        """一条消息在**进 orchestrator 之前**被 Channel 总在途上限拒绝。

        给它铸一个 `turn_id` 再走 `_rejection()`：`orchestrator.handle()` 被 scheduler
        拒绝时做的正是同一件事，用户拿到的因此仍是 `[未受理：…]` + `FAILED`，两条背压
        路径在 Channel 侧长得一模一样。

        **发的是 `instance.input_dropped` 而不是 `turn.rejected`**：这条消息从未进过
        orchestrator，而 turn 事件只有那一个发布点。理由写在 `contracts/events.py`。
        """
        receipt = TurnReceipt(turn_id=TurnId(uuid.uuid4().hex), admitted=False, error=error)
        await self._echo(channel, _rejection(message, receipt))
        self.bus.publish(
            EventName.INSTANCE_INPUT_DROPPED,
            payload={
                "channel": message.channel_id,
                "conversation": message.conversation_id,
            },
            error=error,
        )

    async def _safe(self, awaitable: Awaitable[object]) -> None:
        """跑一件收尾/投递，异常只记不抛。`BaseException` 放行。"""
        try:
            await awaitable
        except Exception as exc:  # noqa: BLE001 - 见 docstring
            self.bus.publish(EventName.PLUGIN_FAILED, error=_as_karyvia(exc))

    async def _echo(self, channel: Channel, message: OutboundMessage) -> None:
        """投递一条**合成的回音**（背压/去重的 `[未受理：…]`），失败只记不抛。

        与 `bootstrap.py::deliver` 分开是因为它走的不是 `OrchestratorDeps.deliver`：
        这些消息由泵自己合成，Orchestrator 根本没见过它们。但**投递失败的记法必须相同**：
        这里发布 `channel.delivery_failed`，不能把“回音发不出去”误报成插件执行故障。
        """
        try:
            await channel.deliver(message)
        except Exception as exc:  # noqa: BLE001 - 见 docstring
            self.bus.publish(
                EventName.CHANNEL_DELIVERY_FAILED,
                correlation=Correlation(
                    instance_id=self.instance_id,
                    session_key=message.session_key,
                    turn_id=message.turn_id,
                ),
                payload={
                    "channel": message.channel_id,
                    "conversation": message.conversation_id,
                    "stream_state": message.stream_state.value,
                    # 这一条是合成回音而不是模型输出。两者的处置不同：回音发不出去意味着
                    # 用户连「被拒了」都不知道。
                    "synthetic": True,
                },
                error=delivery_error(exc),
            )


def outbound_router(
    by_channel: Mapping[str, Channel], bus: EventBus
) -> Callable[[OutboundMessage], Awaitable[None]]:
    """造 `OrchestratorDeps.deliver`：按 `channel_id` 把出站消息路由回对应 Channel。

        **它在这里而不是在装配根里**：`bootstrap.py` 只负责「装」，而「出站怎么走」是本模块
        的职责第二条。路由行为与实例运行期放在一起，装配根只传入完成接线后的 callable。

    找不到对应 Channel 时**静默丢弃**（寻址在消息自己身上）：那是
    `embed.submit()` 这类没有 Channel 的调用方的正常情形，它拿的是 `TurnReceipt.messages`。

    **投递失败折成一条 `channel.delivery_failed`，不上抛**：这一步在
    turn 的最后，模型输出与会话历史都已经正确产生了，让它把 turn 变成 `FAILED` 等于用
    「没送出去」冒充「没算出来」。这里是那条约定的兑现点——
    `contracts/protocols.py::Channel.deliver` 因此可以照约定抛 `EXTERNAL_CHANNEL`。
    `BaseException`（取消、Ctrl-C）放行。
    """

    async def deliver(message: OutboundMessage) -> None:
        channel = by_channel.get(message.channel_id)
        if channel is None:
            return
        try:
            await channel.deliver(message)
        except Exception as exc:  # noqa: BLE001 - 见 docstring
            bus.publish(
                EventName.CHANNEL_DELIVERY_FAILED,
                # 关联标识齐全，因此这条事件挂在**它所属的那个 turn** 上：出站消息自带
                # `session_key + turn_id`， 的按序重放不需要再猜。
                correlation=Correlation(
                    instance_id=bus.instance_id,
                    session_key=message.session_key,
                    turn_id=message.turn_id,
                ),
                payload={
                    "channel": message.channel_id,
                    "conversation": message.conversation_id,
                    "stream_state": message.stream_state.value,
                },
                error=delivery_error(exc),
            )

    return deliver


def delivery_error(exc: Exception) -> KaryviaError:
    """把一次投递失败折成 `KaryviaError`。`bootstrap.py` 的路由点共用它。

    照约定抛 `EXTERNAL_CHANNEL` 的实现原样带出（`retryable` 是实现方的判断，这里不覆写）；
    其余异常折成 `EXTERNAL_CHANNEL` 而不是 `KERNEL_UNEXPECTED`——投递失败的原因在外部
    平台那一侧，把它记成内核异常会把排查方向指错。**只放类型名不放异常消息**：平台 SDK
    的异常文本可能带着 webhook URL 或令牌。
    """
    if isinstance(exc, KaryviaError):
        return exc
    return KaryviaError(
        ErrorCode.EXTERNAL_CHANNEL,
        "出站消息投递失败。",
        detail={"exception": type(exc).__name__},
        # 未按约定抛 `KaryviaError` 的实现没有告诉我们能不能重试，因此不替它猜。
        retryable=False,
    )


def _as_karyvia(exc: Exception) -> KaryviaError:
    """折成 `KaryviaError`。**只放类型名不放异常消息**——第三方实现的异常文本可能带凭据
    。"""
    if isinstance(exc, KaryviaError):
        return exc
    return KaryviaError(
        ErrorCode.KERNEL_UNEXPECTED,
        "实例运行期出现未预期异常。",
        detail={"exception": type(exc).__name__},
    )


def _consume_task_result(task: asyncio.Task[None]) -> None:
    """强制停止后取走后台任务结果，避免稍后产生无人认领异常。"""
    if not task.cancelled():
        task.exception()


def _rejection(message: InboundMessage, receipt: TurnReceipt) -> OutboundMessage:
    """给一条未被准入的消息合成回音。

    去重命中时说清楚指向哪个 turn——「什么也没发生」和「这条我上次已经答过了」是两个
    不同的结论。
    """
    if receipt.duplicate_of is not None:
        content = f"[重复投递，已忽略；上一次是 turn {receipt.duplicate_of}]"
    elif receipt.error is not None:
        content = f"[未受理：{receipt.error.user_message}]"
    else:
        content = "[未受理]"
    key = SessionKey(
        channel_id=message.channel_id,
        conversation_id=message.conversation_id,
    )
    return OutboundMessage(
        session_key=key,
        channel_id=key.channel_id,
        conversation_id=key.conversation_id,
        turn_id=TurnId(str(receipt.turn_id)),
        content=content,
        stream_state=StreamState.FAILED,
    )
