"""`D23` `AgentInstance`：Channel 泵、被拒 turn 的回音、停止顺序与 `Ctrl-C` 状态机。

职责：验运行期那几条容易被忽略的路径——重复投递怎么给用户交代、一条炸掉的消息会不会
把泵带走、`stop()` 会不会漏掉插件派生的任务、两次 `Ctrl-C` 各做什么。
不负责：验装配（`test_bootstrap.py`）、验渲染（`tests/builtins/test_cli_entry.py`）。

**`_Interrupts` 单独测**：`nm run` 的其余部分要一个真终端，而这个状态机恰好是那条命令
里唯一有分支的地方——第一次取消 turn、第二次退出，`§10.3` 的全部内容。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from nucleamind.contracts import (
    CancelReason,
    CapabilityKind,
    ErrorCode,
    EventName,
    NucleaError,
    StreamState,
)
from nucleamind.kernel.config import InstanceLock
from nucleamind.kernel.plugins import PluginPhase
from nucleamind.kernel.turn import CancelToken
from nucleamind.runtime.cli.commands.run import _Interrupts
from nucleamind.runtime.instance import AgentInstance, delivery_error
from nucleamind.sdk import NucleaAPI

from ._support import (
    MULTI_CHANNEL_ID,
    SCRIPT,
    TEST_MANIFESTS,
    ScriptedChannel,
    inbound,
    manifests_with_multi_channel,
    register_test_manifest,
    text_response,
    write_config,
)
from .test_bootstrap import _boot


@pytest.fixture(autouse=True)
def _script() -> None:
    SCRIPT[:] = [text_response("好的。"), text_response("好的。")]


def console_of(instance: AgentInstance) -> object:
    channel = dict(instance.channels)["cli"]
    return channel._console  # noqa: SLF001 - 断言渲染结果需要它


async def test_a_duplicate_message_gets_an_explicit_echo(tmp_path: Path) -> None:
    """`EDG-201`：重复投递不产生第二次副作用，但用户要知道发生了什么。

    被去重的消息没有终态事件（那条 turn 从未开始），泵因此自己合成一条出站消息——
    否则 CLI 会永远等一个不会到来的终态。
    """
    write_config(tmp_path)
    instance = await _boot(tmp_path, manifests=TEST_MANIFESTS)
    try:
        await instance.start()
        console = console_of(instance)
        first = console.submit("在吗")  # type: ignore[attr-defined]
        await asyncio.wait_for(console.wait_for_turn(), timeout=2)  # type: ignore[attr-defined]

        # 同一个 message_id 再投一次：去重命中。
        console._counter -= 1  # type: ignore[attr-defined] # noqa: SLF001 - 复现同一个 id
        again = console.submit("在吗")  # type: ignore[attr-defined]
        assert again.message_id == first.message_id
        await asyncio.wait_for(console.wait_for_turn(), timeout=2)  # type: ignore[attr-defined]
        assert "重复投递" in console.rendered[-1]  # type: ignore[attr-defined]
        assert console.last_state is StreamState.FAILED  # type: ignore[attr-defined]
    finally:
        await instance.stop()


async def test_stop_is_idempotent_and_publishes_both_events(tmp_path: Path) -> None:
    write_config(tmp_path)
    instance = await _boot(tmp_path, manifests=TEST_MANIFESTS)
    await instance.start()
    await instance.stop()
    await instance.stop()
    names = [event.name for event in instance.diagnostics.events.events()]
    assert names.count(EventName.INSTANCE_STOPPED) == 1
    assert names.count(EventName.INSTANCE_STOPPING) == 1


async def test_a_failed_channel_start_is_isolated(tmp_path: Path) -> None:
    """单个 Channel 启动失败只停掉自身，实例与其它入口继续运行。"""
    _FAILING_CHANNELS.clear()
    manifest_path = tmp_path / "failing-channel.toml"
    manifest_path.write_text(
        f"""
id = "failing-channel"
version = "0.1.0"
sdk_range = ">=4.0.0,<5.0.0"
setup = "tests.runtime.test_instance:setup_failing_channel"

[[capabilities]]
kind = "channel"
name = "{MULTI_CHANNEL_ID}"
""".strip(),
        encoding="utf-8",
    )
    register_test_manifest(manifest_path)
    write_config(tmp_path, plugins={"enabled": ["failing-channel"]})
    instance = await _boot(tmp_path)

    await instance.start()

    channel = _FAILING_CHANNELS[0]
    assert channel.started == 1
    assert channel.stopped == 1
    lifecycle = next(item for item in instance.lifecycles if item.plugin_id == "failing-channel")
    assert lifecycle.phase is PluginPhase.FAILED
    assert MULTI_CHANNEL_ID not in instance.active_channel_ids
    assert "cli" in instance.active_channel_ids
    names = [event.name for event in instance.diagnostics.events.events()]
    assert EventName.INSTANCE_READY in names
    assert EventName.PLUGIN_FAILED in names

    await instance.stop()
    assert channel.stopped == 1
    InstanceLock(instance.layout.lock_path).acquire().release()
    with pytest.raises(NucleaError) as caught:
        await instance.start()
    assert caught.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


async def test_stop_cancels_plugin_tasks(tmp_path: Path) -> None:
    """`EDG-104`/`EDG-105`：插件派生的后台任务在停止时被收走。"""
    write_config(tmp_path)
    instance = await _boot(tmp_path, manifests=TEST_MANIFESTS)
    await instance.start()
    ctx = instance.contexts[0]

    async def forever() -> None:
        await asyncio.sleep(3600)

    ctx.spawn_task(forever(), name="probe")
    task = next(iter(ctx.tasks))
    await instance.stop()
    assert task.cancelled()


class FailingStartChannel(ScriptedChannel):
    async def start(self) -> None:
        self.started += 1
        raise RuntimeError("channel start failed")


_FAILING_CHANNELS: list[FailingStartChannel] = []


def setup_failing_channel(api: NucleaAPI) -> None:
    channel = FailingStartChannel()
    _FAILING_CHANNELS.append(channel)
    api.register_channel(MULTI_CHANNEL_ID, channel)


# ---------------------------------------------------------------- D28 生命周期


async def test_stop_walks_the_lifecycle_in_reverse_load_order(tmp_path: Path) -> None:
    """`PLG-005`：停止顺序是加载顺序的逆序，且每个提供方都走完状态机。"""
    write_config(tmp_path)
    instance = await _boot(tmp_path, manifests=TEST_MANIFESTS)
    loaded = [lifecycle.plugin_id for lifecycle in instance.lifecycles]
    assert loaded == [ctx.plugin_id for ctx in instance.contexts]
    assert all(item.phase is PluginPhase.LOADED for item in instance.lifecycles)

    await instance.start()
    assert all(item.phase is PluginPhase.STARTED for item in instance.lifecycles)

    await instance.stop()
    assert all(item.phase is PluginPhase.STOPPED for item in instance.lifecycles)
    deactivated = [
        event.payload["plugin"]
        for event in instance.diagnostics.events.events()
        if event.name is EventName.PLUGIN_DEACTIVATED
    ]
    assert deactivated == list(reversed(loaded))


async def test_plugin_start_and_cleanup_follow_the_load_topology(tmp_path: Path) -> None:
    write_config(tmp_path)
    instance = await _boot(tmp_path, manifests=TEST_MANIFESTS)
    trace: list[str] = []

    def action(label: str) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            trace.append(label)

        return run

    for context in instance.contexts:
        context.on_start(action(f"start:{context.plugin_id}"))
        context.add_cleanup(action(f"stop:{context.plugin_id}"))

    await instance.start()
    loaded = [context.plugin_id for context in instance.contexts]
    assert trace == [f"start:{plugin_id}" for plugin_id in loaded]

    await instance.stop()
    assert trace == [
        *(f"start:{plugin_id}" for plugin_id in loaded),
        *(f"stop:{plugin_id}" for plugin_id in reversed(loaded)),
    ]


async def test_stop_unsubscribes_the_plugin_event_bridge(tmp_path: Path) -> None:
    """`EDG-105` 第二项：停止后事件订阅失效，handler 不再收到投递。"""
    write_config(tmp_path)
    instance = await _boot(tmp_path, manifests=TEST_MANIFESTS)
    await instance.start()
    ctx = instance.contexts[0]
    seen: list[str] = []

    async def observe(event: object) -> None:
        seen.append(type(event).__name__)

    ctx.events.subscribe(EventName.INSTANCE_STOPPING, observe)
    assert any(sub.name == f"plugin:{ctx.plugin_id}" for sub in instance.bus.subscribers())

    await instance.stop()
    assert not any(sub.name == f"plugin:{ctx.plugin_id}" for sub in instance.bus.subscribers())
    before = len(seen)
    instance.bus.publish(EventName.INSTANCE_STOPPING)
    await asyncio.sleep(0)
    assert len(seen) == before


async def test_a_stopped_plugin_cannot_spawn_new_tasks(tmp_path: Path) -> None:
    """`EDG-105` 第三项的另一半：停止之后派生的任务不在任何一轮取消的覆盖范围里。"""
    write_config(tmp_path)
    instance = await _boot(tmp_path, manifests=TEST_MANIFESTS)
    await instance.start()
    ctx = instance.contexts[0]
    await instance.stop()

    async def late() -> None:  # pragma: no cover - 它就不该被跑
        await asyncio.sleep(0)

    with pytest.raises(NucleaError) as caught:
        ctx.spawn_task(late(), name="late")
    assert caught.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED
    assert ctx.tasks == set()


async def test_a_disabled_plugin_leaves_no_capability_behind(tmp_path: Path) -> None:
    """`EDG-105` 第一项：被禁用的提供方在这次实例里一项能力都查不到。

    首版不热更新（技术方案 §10.4）：禁用在**下一次启动**生效，因此「注销能力」的兑现
    形态是「它的 `setup()` 根本没跑过」——registry 解析后只读（`NFR-403`），运行期没有
    第二条把已冻结的能力摘掉的路径。
    """
    write_config(tmp_path, plugins={"disable": ["tools-shell"]})
    instance = await _boot(tmp_path, manifests=TEST_MANIFESTS)
    try:
        assert "tools-shell" not in [ctx.plugin_id for ctx in instance.contexts]
        # 内建全部以 `Builtin()` 身份注册，因此「谁不在了」只能按能力名看。
        active = [item.ref.name for item in instance.registry.of_kind(CapabilityKind.TOOL)]
        assert "shell.exec" not in active
        assert "shell.exec" not in [tool.name for tool in instance.deps.tool_specs]
    finally:
        await instance.stop()


async def test_a_hanging_plugin_does_not_hold_up_the_instance(tmp_path: Path) -> None:
    """`EDG-104`：吞掉取消的后台任务不能扣住实例退出。"""
    write_config(tmp_path, plugins={"stop_timeout_ms": 50})
    instance = await _boot(tmp_path, manifests=TEST_MANIFESTS)
    await instance.start()
    ctx = instance.contexts[0]
    assert instance.stop_timeout_ms == 50

    async def stubborn() -> None:
        """吞掉停止流程那一次取消。`release` 之后才肯死，否则用例自己也收不了场。"""
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                if release.is_set():
                    raise
                continue

    release = asyncio.Event()

    ctx.spawn_task(stubborn(), name="stubborn")
    task = next(iter(ctx.tasks))
    await asyncio.wait_for(instance.stop(), timeout=5)
    # 用例自己收尾：被放弃的任务在生产里随进程一起没了，而这里事件循环还要继续用。
    release.set()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    failures = [
        event
        for event in instance.diagnostics.events.events()
        if event.name is EventName.PLUGIN_FAILED and event.payload.get("timed_out")
    ]
    assert [event.payload["plugin"] for event in failures] == [ctx.plugin_id]
    assert failures[0].error is not None
    assert failures[0].error.code is ErrorCode.TIMEOUT_PLUGIN_STOP
    lifecycle = next(item for item in instance.lifecycles if item.plugin_id == ctx.plugin_id)
    assert lifecycle.phase is PluginPhase.FAILED


# ---------------------------------------------------------------- Ctrl-C 状态机


class _FakeOrchestrator:
    """只提供 `_Interrupts` 要的两件事。"""

    def __init__(self, live: tuple[str, ...]) -> None:
        self.live_turns = live
        self.cancelled: list[tuple[str, CancelReason]] = []

    def cancel(self, turn_id: str, reason: CancelReason) -> bool:
        self.cancelled.append((turn_id, reason))
        return True


def _interrupts(live: tuple[str, ...]) -> tuple[_Interrupts, _FakeOrchestrator, CancelToken]:
    orchestrator = _FakeOrchestrator(live)
    instance = type("_I", (), {"orchestrator": orchestrator})()
    cancel = CancelToken()
    return _Interrupts(instance, cancel), orchestrator, cancel  # type: ignore[arg-type]


def test_the_first_ctrl_c_cancels_the_live_turn(capsys: pytest.CaptureFixture[str]) -> None:
    """会话继续——中断的是这一轮，不是进程（§10.3、`CliEntry.run` 的取消语义）。"""
    interrupts, orchestrator, cancel = _interrupts(("turn-1",))
    interrupts()
    assert orchestrator.cancelled == [("turn-1", CancelReason.USER)]
    assert not cancel.requested
    assert "再按一次" in capsys.readouterr().err


def test_ctrl_c_with_nothing_running_is_a_quit() -> None:
    """没有 turn 在跑时，`Ctrl-C` 的唯一合理含义是「退出」。"""
    interrupts, orchestrator, cancel = _interrupts(())
    # **本用例刻意是同步的**：退出路径会 `await stop()` 之后 `os._exit`，在测试进程里跑到
    # 那一步会直接把 pytest 打死。没有运行中的循环时 `create_task` 抛 `RuntimeError`，
    # 恰好让断言停在「令牌已被请求」这一步——退出动作是异步的，这条顺带钉住了。
    with pytest.raises(RuntimeError):
        interrupts()
    assert cancel.requested
    assert orchestrator.cancelled == []


# ---------------------------------------------------------- Channel 泵与统一 Session 调度


def multi_channel(instance: AgentInstance) -> ScriptedChannel:
    channel = dict(instance.channels)[MULTI_CHANNEL_ID]
    assert isinstance(channel, ScriptedChannel)
    return channel


def _answered(channel: ScriptedChannel, conversation: str) -> bool:
    """该会话是否已经拿到一条**完整答案**。

    不能只看「有没有出站消息」：流式的 `STARTED` / `DELTA` 也在 `delivered` 里，用它判
    会把「turn 刚开始」当成「turn 结束了」。
    """
    return any(
        m.conversation_id == conversation and m.is_complete_answer for m in channel.delivered
    )


async def _wait_for(predicate: Callable[[], bool], *, timeout: float = 3.0) -> None:
    """等一个条件成立。**不 `sleep` 固定时长**：断言的是「发生了」而不是「多久之内」。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("等待的条件在超时前没有成立")
        await asyncio.sleep(0)


async def test_two_conversations_on_one_channel_do_not_block_each_other(
    tmp_path: Path,
) -> None:
    """一个用户的慢 turn 不会卡住同一个 Channel 上的其它 Session。"""
    write_config(tmp_path)
    SCRIPT[:] = [text_response("答案"), text_response("答案")]
    instance = await _boot(tmp_path, manifests=manifests_with_multi_channel())
    try:
        await instance.start()
        channel = multi_channel(instance)
        blocked = asyncio.Event()
        slow_entered = asyncio.Event()
        provider = instance.deps.model
        original_complete = provider.complete
        original_stream = provider.stream

        def is_slow(request: object) -> bool:
            return any("慢" in message.content for message in request.messages)  # type: ignore[attr-defined]

        async def gated_complete(request: object, cancel: object) -> object:
            if is_slow(request):
                slow_entered.set()
                await blocked.wait()
            return await original_complete(request, cancel)  # type: ignore[arg-type]

        def gated_stream(request: object, cancel: object) -> object:
            async def run() -> object:
                if is_slow(request):
                    slow_entered.set()
                    await blocked.wait()
                async for chunk in original_stream(request, cancel):  # type: ignore[arg-type]
                    yield chunk

            return run()

        # **两条路都要拦**：走 `complete` 还是 `stream` 由装配决定，只拦一条会让这条用例
        # 的结论取决于「谁先跑完」而不是「有没有被挡住」。
        provider.complete = gated_complete  # type: ignore[assignment, method-assign]
        provider.stream = gated_stream  # type: ignore[assignment, method-assign]

        channel.push(inbound("slow", "慢一点"))
        channel.push(inbound("fast", "快一点"))

        # 先确认慢会话真的卡在模型上，再断言快会话已经答完——否则断言的是时序巧合。
        await asyncio.wait_for(slow_entered.wait(), timeout=3)
        await _wait_for(lambda: _answered(channel, "fast"))
        assert not _answered(channel, "slow")
        blocked.set()
        await _wait_for(lambda: _answered(channel, "slow"))
    finally:
        await instance.stop()


async def test_the_same_conversation_still_enters_the_scheduler_in_arrival_order(
    tmp_path: Path,
) -> None:
    """`EDG-202` 的逐字断言：同 Session 严格 FIFO、单写者。

    事件流里第二条 turn 的 `turn.started` 一定在第一条的终态之后——这就是「同一时刻至多
    一个写者」在事件层面的形状。
    """
    write_config(tmp_path)
    SCRIPT[:] = [text_response("一"), text_response("二")]
    instance = await _boot(tmp_path, manifests=manifests_with_multi_channel())
    seen: list[str] = []
    instance.bus.subscribe(lambda event: seen.append(event.name.value))
    try:
        await instance.start()
        channel = multi_channel(instance)
        channel.push(inbound("same", "一"))
        channel.push(inbound("same", "二"))
        await _wait_for(
            lambda: len([m for m in channel.delivered if m.is_complete_answer]) == 2
        )
    finally:
        await instance.stop()

    starts = [i for i, name in enumerate(seen) if name == EventName.TURN_STARTED.value]
    completions = [i for i, name in enumerate(seen) if name == EventName.TURN_COMPLETED.value]
    assert len(starts) == 2 and len(completions) == 2
    assert starts[0] < completions[0] < starts[1] < completions[1]


async def test_a_full_session_queue_is_rejected_by_the_scheduler(
    tmp_path: Path,
) -> None:
    """Channel 也只使用 `queue_max_size`，拒绝必须来自 Orchestrator。"""
    write_config(tmp_path, routing={"queue_max_size": 1})
    SCRIPT[:] = [text_response("好") for _ in range(6)]
    instance = await _boot(tmp_path, manifests=manifests_with_multi_channel())
    rejected: list[str] = []

    def watch(event: object) -> None:
        if event.name is EventName.TURN_REJECTED:  # type: ignore[attr-defined]
            rejected.append(event.name.value)  # type: ignore[attr-defined]

    instance.bus.subscribe(watch)
    try:
        await instance.start()
        channel = multi_channel(instance)
        blocked = asyncio.Event()
        provider = instance.deps.model
        original = provider.complete  # type: ignore[union-attr]

        async def gated(request: object, cancel: object) -> object:
            await blocked.wait()
            return await original(request, cancel)  # type: ignore[arg-type]

        provider.complete = gated  # type: ignore[union-attr, assignment, method-assign]

        for index in range(5):
            channel.push(inbound("busy", str(index), message_id=f"m{index}"))
        await _wait_for(lambda: bool(rejected))
        failures = [m for m in channel.delivered if m.stream_state is StreamState.FAILED]
        assert failures, "被拒的消息必须有回音"
        assert "未受理" in failures[0].content
        assert not any(
            event.name is EventName.INSTANCE_INPUT_DROPPED
            for event in instance.diagnostics.events.events()
        )
        blocked.set()
    finally:
        await instance.stop()


async def test_channel_merge_reaches_the_scheduler(tmp_path: Path) -> None:
    """同一 Channel 的突发消息必须能被合并成一个真实 Turn。"""
    write_config(tmp_path, routing={"session_concurrency": "merge"})
    SCRIPT[:] = [text_response("合并完成")]
    instance = await _boot(tmp_path, manifests=manifests_with_multi_channel())
    try:
        await instance.start()
        channel = multi_channel(instance)
        for index in range(3):
            channel.push(inbound("same", str(index), message_id=f"merge-{index}"))
        await _wait_for(lambda: _answered(channel, "same"))
        started = [
            event
            for event in instance.diagnostics.events.events()
            if event.name is EventName.TURN_STARTED
        ]
        assert len(started) == 1
        assert started[0].payload["merged_from"] == ["merge-1", "merge-2"]
    finally:
        await instance.stop()


async def test_channel_reject_reaches_the_scheduler(tmp_path: Path) -> None:
    """`reject` 在 Channel 路径中立即拒绝同 Session 的第二条消息。"""
    write_config(tmp_path, routing={"session_concurrency": "reject"})
    SCRIPT[:] = [text_response("好")]
    instance = await _boot(tmp_path, manifests=manifests_with_multi_channel())
    try:
        await instance.start()
        channel = multi_channel(instance)
        channel.push(inbound("same", "一", message_id="reject-1"))
        channel.push(inbound("same", "二", message_id="reject-2"))
        await _wait_for(
            lambda: any(m.stream_state is StreamState.FAILED for m in channel.delivered)
        )
        assert any(
            event.name is EventName.TURN_REJECTED
            for event in instance.diagnostics.events.events()
        )
    finally:
        await instance.stop()


async def test_channel_in_flight_limit_publishes_input_dropped(tmp_path: Path) -> None:
    """总在途护栏只保护 Runtime 任务数量，不冒充 Session 拒绝。"""
    write_config(tmp_path, routing={"channel_concurrency": 1})
    SCRIPT[:] = [text_response("好")]
    instance = await _boot(tmp_path, manifests=manifests_with_multi_channel())
    try:
        await instance.start()
        channel = multi_channel(instance)
        channel.push(inbound("a", "一", message_id="limit-1"))
        channel.push(inbound("b", "二", message_id="limit-2"))
        await _wait_for(
            lambda: any(
                event.name is EventName.INSTANCE_INPUT_DROPPED
                for event in instance.diagnostics.events.events()
            )
        )
    finally:
        await instance.stop()


async def test_stop_drains_every_channel_submission(tmp_path: Path) -> None:
    """停止之后不能留下 Channel 提交任务。"""
    write_config(tmp_path)
    SCRIPT[:] = [text_response("好") for _ in range(4)]
    instance = await _boot(tmp_path, manifests=manifests_with_multi_channel())
    await instance.start()
    channel = multi_channel(instance)
    for index in range(3):
        channel.push(inbound(f"c{index}", "在吗", message_id=f"s{index}"))
    await instance.stop()
    assert instance._channel_tasks == {}  # noqa: SLF001 - 停机所有权的最终状态


# --------------------------------------------- 投递失败：`channel.delivery_failed`（`D43`）


def _events(instance: AgentInstance) -> list[object]:
    """订阅并收集全部事件。断言的是「哪条事件出现了、哪条没有」。"""
    seen: list[object] = []
    instance.bus.subscribe(seen.append)
    return seen


def _names(seen: list[object]) -> list[EventName]:
    return [event.name for event in seen]  # type: ignore[attr-defined]


async def test_a_failing_delivery_does_not_fail_the_turn(tmp_path: Path) -> None:
    """`EDG-204` 与 `Channel.deliver` 的 docstring 从此不再矛盾。

    契约说投递失败抛 `EXTERNAL_CHANNEL`，而这条 turn 的模型输出与会话历史都已经正确产生
    了——它必须走到 `turn.completed`。「答案没算出来」与「答案没送出去」的处置不同
    （重跑 vs 重发），因此后者有自己的事件名。
    """
    write_config(tmp_path)
    instance = await _boot(tmp_path, manifests=manifests_with_multi_channel())
    seen = _events(instance)
    try:
        await instance.start()
        channel = multi_channel(instance)
        channel.fail_delivery_with = NucleaError(
            ErrorCode.EXTERNAL_CHANNEL, "平台拒收。", retryable=True
        )
        channel.push(inbound("c0", "在吗", message_id="m0"))
        await _wait_for(lambda: EventName.TURN_COMPLETED in _names(seen))

        names = _names(seen)
        assert EventName.CHANNEL_DELIVERY_FAILED in names
        assert EventName.TURN_FAILED not in names
        # 投递失败不该被折进 `plugin.failed`——内建 CLI 的投递失败与插件无关，而 `D43`
        # 之前它正是那么记的。
        assert EventName.PLUGIN_FAILED not in names
    finally:
        channel.fail_delivery_with = None
        await instance.stop()


async def test_the_delivery_failure_event_points_at_the_turn_it_belongs_to(
    tmp_path: Path,
) -> None:
    """`OBS-002`：这条事件挂在它所属的那个 turn 上，不需要靠猜。

    出站消息自带 `session_key + turn_id`，因此关联标识是齐的——发一条 `correlation=None`
    的事件等于让排查的人回到「按时间戳对齐」。
    """
    write_config(tmp_path)
    instance = await _boot(tmp_path, manifests=manifests_with_multi_channel())
    seen = _events(instance)
    try:
        await instance.start()
        channel = multi_channel(instance)
        channel.fail_delivery_with = RuntimeError("socket closed")
        channel.push(inbound("c0", "在吗", message_id="m0"))
        await _wait_for(lambda: EventName.TURN_COMPLETED in _names(seen))

        failures = [
            event
            for event in seen
            if event.name is EventName.CHANNEL_DELIVERY_FAILED  # type: ignore[attr-defined]
        ]
        assert failures
        first = failures[0]
        assert first.correlation is not None  # type: ignore[attr-defined]
        assert first.correlation.turn_id  # type: ignore[attr-defined]
        assert first.payload["channel"] == MULTI_CHANNEL_ID  # type: ignore[attr-defined]
        # 非 `NucleaError` 折成 `EXTERNAL_CHANNEL` 而不是 `KERNEL_UNEXPECTED`：原因在平台
        # 那一侧，记成内核异常会把排查方向指错。
        assert first.error is not None  # type: ignore[attr-defined]
        assert first.error.code is ErrorCode.EXTERNAL_CHANNEL  # type: ignore[attr-defined]
        # **只放类型名不放异常消息**：平台 SDK 的异常文本可能带 webhook URL 或令牌。
        assert "socket closed" not in str(dict(first.error.detail))  # type: ignore[attr-defined]
    finally:
        channel.fail_delivery_with = None
        await instance.stop()


async def test_a_failing_echo_is_marked_synthetic(tmp_path: Path) -> None:
    """合成回音（`[未受理：…]`）发不出去，与模型输出发不出去要分得开。

    前者意味着用户连「被拒了」都不知道，而那正是最需要被看见的一种失败。两者共用同一个
    事件名，靠载荷的 `synthetic` 区分——一次投递失败不值得发明第二个事件名。
    """
    write_config(tmp_path)
    instance = await _boot(tmp_path, manifests=manifests_with_multi_channel())
    seen = _events(instance)
    try:
        await instance.start()
        channel = multi_channel(instance)
        channel.fail_delivery_with = RuntimeError("gone")
        # 同一个 `message_id` 推两次：第二条被去重，泵为它合成一条回音。
        channel.push(inbound("c0", "在吗", message_id="same"))
        channel.push(inbound("c0", "在吗", message_id="same"))
        await _wait_for(
            lambda: any(
                event.name is EventName.CHANNEL_DELIVERY_FAILED  # type: ignore[attr-defined]
                and event.payload.get("synthetic") is True  # type: ignore[attr-defined]
                for event in seen
            )
        )
    finally:
        channel.fail_delivery_with = None
        await instance.stop()


def test_delivery_error_keeps_what_the_implementation_said() -> None:
    """照约定抛 `NucleaError` 的实现原样带出——`retryable` 是它的判断，不替它覆写。

    未按约定抛的实现没告诉我们能不能重试，因此折出来的那条标 `retryable=False`：
    猜一个 `True` 会让编排层去重发一条可能已经发出去的消息。
    """
    declared = NucleaError(ErrorCode.EXTERNAL_CHANNEL, "限流。", retryable=True)
    assert delivery_error(declared) is declared

    folded = delivery_error(ValueError("token=abc123"))
    assert folded.code is ErrorCode.EXTERNAL_CHANNEL
    assert folded.retryable is False
    assert dict(folded.detail) == {"exception": "ValueError"}
