"""`D23` 生产级 `PluginContext`：配置块、状态目录、凭据、事件桥与两个门面。

职责：验资源门面、`ctx.secret()` 的两种结局、事件桥
把同步 bus 接到异步 handler 上的规则、`instance` / `turns` 在就绪之前不可用。
不负责：验装配根怎么构造它（`test_bootstrap.py`）。

**哨兵贯穿全文**：明文凭据不得出现在任何一条错误、`repr` 或事件序列化里（`MOD-002`）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from karyvia.contracts import (
    ErrorCode,
    EventName,
    InstanceId,
    KaryviaError,
    RuntimeEvent,
    SecretStr,
)
from karyvia.kernel.observability import EventBus
from karyvia.runtime.access import GuardedHttpAccess
from karyvia.runtime.plugin_context import (
    PluginRuntime,
    RuntimePluginContext,
    build_plugin_context,
)

#: 形状匹配 `errors.py::_SECRET_VALUE_PATTERNS` 的哨兵，`sk-` + 16 位以上。
SENTINEL = "sk-0123456789abcdef"

#: `workspace=None` 是一个**有意义的取值**（「这个实例没有 workspace」），因此默认值不能
#: 是 `None`——用一个哨兵把「没传」和「传了 None」分开。
_UNSET: Path = Path("<unset>")


def make_ctx(
    tmp_path: Path,
    *,
    secrets: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
    runtime: PluginRuntime | None = None,
    workspace: Path | None = _UNSET,
) -> RuntimePluginContext:
    ctx = build_plugin_context(
        "probe",
        config={"a": 1},
        secrets=secrets or {},
        state_dir=tmp_path / "plugins" / "probe",
        bus=EventBus(InstanceId("test")),
        runtime=runtime or PluginRuntime(),
        env=env,
        workspace=(tmp_path / "workspace") if workspace is _UNSET else workspace,
    )
    assert isinstance(ctx, RuntimePluginContext)
    return ctx


# ------------------------------------------------------------------ 配置与状态目录


def test_the_config_block_is_handed_over_untouched(tmp_path: Path) -> None:
    assert make_ctx(tmp_path).config == {"a": 1}


def test_the_state_dir_is_created_on_first_access(tmp_path: Path) -> None:
    """装配根不为一个可能从未写盘的插件先建目录。"""
    ctx = make_ctx(tmp_path)
    assert not (tmp_path / "plugins" / "probe").exists()
    assert ctx.state_dir.is_dir()


# ---------------------------------------------------------------------- 资源服务


def test_context_hands_back_a_guarded_network_facade(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    assert isinstance(ctx.net, GuardedHttpAccess)


def test_a_facade_without_a_workspace_is_honest_about_it(tmp_path: Path) -> None:
    """没有 workspace 时 `fs` / `shell` 无处落地——报 `CAPABILITY_MISSING` 而不是
    悄悄拿一个临时目录当根。"""
    ctx = make_ctx(tmp_path, workspace=None)
    with pytest.raises(KaryviaError) as caught:
        _ = ctx.fs
    assert caught.value.code is ErrorCode.CAPABILITY_MISSING


# ---------------------------------------------------------------------- 凭据


def test_a_secret_resolves_from_the_environment(tmp_path: Path) -> None:
    ctx = make_ctx(
        tmp_path,
        secrets={"api_key": "${NM_TOKEN}"},
        env={"NM_TOKEN": SENTINEL},
    )
    secret = ctx.secret("api_key")
    assert isinstance(secret, SecretStr)
    assert secret.reveal() == SENTINEL
    # 掩码是默认渲染，明文只经 `reveal()` 取出。
    assert SENTINEL not in f"{secret!r} {secret}"


def test_a_missing_secret_reports_its_config_pointer(tmp_path: Path) -> None:
    with pytest.raises(KaryviaError) as missing:
        make_ctx(tmp_path).secret("api_key")
    assert missing.value.code is ErrorCode.CONFIG_SECRET_MISSING
    assert missing.value.detail["pointer"] == "/plugins/probe/secrets/api_key"


def test_an_unexported_variable_reports_only_its_name(tmp_path: Path) -> None:
    """`EDG-502`：错误里只有变量名与位置，没有任何值。"""
    ctx = make_ctx(tmp_path, secrets={"api_key": "${NOPE}"}, env={})
    with pytest.raises(KaryviaError) as caught:
        ctx.secret("api_key")
    assert caught.value.code is ErrorCode.CONFIG_SECRET_MISSING
    assert "NOPE" in repr(caught.value.detail)


def test_a_literal_secret_is_still_wrapped(tmp_path: Path) -> None:
    """按位置就是一个凭据——不含 `${VAR}` 也要包起来，免得它以明文进日志。"""
    ctx = make_ctx(tmp_path, secrets={"api_key": SENTINEL})
    assert ctx.secret("api_key").reveal() == SENTINEL
    assert SENTINEL not in str(ctx.secret("api_key"))


# ---------------------------------------------------------------------- 事件桥


async def test_events_reach_an_async_handler(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    seen: list[RuntimeEvent] = []

    async def handler(event: RuntimeEvent) -> None:
        seen.append(event)

    ctx.events.subscribe(EventName.INSTANCE_READY, handler)
    ctx.bridge._bus.publish(EventName.INSTANCE_READY)  # noqa: SLF001 - 桥就是接在它上面的
    await asyncio.sleep(0)
    await asyncio.gather(*ctx.bridge.tasks)
    assert [event.name for event in seen] == [EventName.INSTANCE_READY]


async def test_only_the_subscribed_event_is_delivered(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    calls = 0

    async def handler(event: RuntimeEvent) -> None:
        nonlocal calls
        del event
        calls += 1

    ctx.events.subscribe(EventName.INSTANCE_READY, handler)
    # 同一个 handler 重复订阅同一事件视为一次（`EventSubscriber` 的约定）。
    ctx.events.subscribe(EventName.INSTANCE_READY, handler)
    ctx.bridge._bus.publish(EventName.INSTANCE_STOPPING)  # noqa: SLF001
    ctx.bridge._bus.publish(EventName.INSTANCE_READY)  # noqa: SLF001
    await asyncio.sleep(0)
    await asyncio.gather(*ctx.bridge.tasks)
    assert calls == 1


def test_publishing_without_a_loop_is_counted_not_crashed(tmp_path: Path) -> None:
    """`publish()` 会在没有事件循环的路径上被调用（`instance.starting`、`karyvia config show`）。"""
    ctx = make_ctx(tmp_path)

    async def handler(event: RuntimeEvent) -> None:  # pragma: no cover - 永远不会被跑到
        del event

    ctx.events.subscribe(EventName.INSTANCE_READY, handler)
    ctx.bridge._bus.publish(EventName.INSTANCE_READY)  # noqa: SLF001
    assert ctx.bridge.dropped == 1


# ---------------------------------------------------------- 同步 handler（`D41`）
#
# `sdk.EventHandler` 从 `D41` 起同时接受同步与协程两种形状。补这三条之前，同步 handler
# 会先被正常调用、再在一条无人认领的 Task 里 `await None` 抛 `TypeError`——官方插件
# `feishu`（工具提示）与 `openai-api`（用量统计）注册的都是同步 handler，两处因此一直在
# 每个事件上多产一条异常 Task。**测试测不到、类型能看见**，与 `D39` 那次同一类。


async def test_events_reach_a_sync_handler(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    seen: list[RuntimeEvent] = []

    def handler(event: RuntimeEvent) -> None:
        seen.append(event)

    ctx.events.subscribe(EventName.INSTANCE_READY, handler)
    ctx.bridge._bus.publish(EventName.INSTANCE_READY)  # noqa: SLF001
    await asyncio.sleep(0)
    assert [event.name for event in seen] == [EventName.INSTANCE_READY]


async def test_a_sync_handler_spawns_no_task(tmp_path: Path) -> None:
    """就地跑完，不派生 Task。

    这条是那个 bug 的直接反面：修之前 `tasks` 里会多出一条 `TypeError` 的 Task，
    而它的异常从来没有人取——只在解释器 GC 时刷一句
    "Task exception was never retrieved"。断言「一条都没有」比断言「没抛异常」有用，
    因为原来的路径**也没有抛**，它只是把失败挪进了别处。
    """
    ctx = make_ctx(tmp_path)

    def handler(event: RuntimeEvent) -> None:
        del event

    ctx.events.subscribe(EventName.INSTANCE_READY, handler)
    ctx.bridge._bus.publish(EventName.INSTANCE_READY)  # noqa: SLF001
    assert ctx.bridge.tasks == set()
    await asyncio.sleep(0)
    assert ctx.bridge.tasks == set()


def test_a_sync_handler_runs_even_without_a_loop(tmp_path: Path) -> None:
    """没有事件循环时同步 handler 照跑，不计入 `dropped`。

    `dropped` 说的是「有一次投递没能发生」。同步 handler 不需要循环，把它也算进去会让
    那个计数在排查时指向错误的方向。
    """
    ctx = make_ctx(tmp_path)
    calls = 0

    def handler(event: RuntimeEvent) -> None:
        nonlocal calls
        del event
        calls += 1

    ctx.events.subscribe(EventName.INSTANCE_READY, handler)
    ctx.bridge._bus.publish(EventName.INSTANCE_READY)  # noqa: SLF001
    assert (calls, ctx.bridge.dropped) == (1, 0)


# ------------------------------------------------------------------ 后台任务与门面


async def test_spawn_task_registers_the_task(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    done = asyncio.Event()

    async def body() -> None:
        done.set()

    ctx.spawn_task(body(), name="probe")
    assert not done.is_set()
    assert [name for _, name in ctx.pending_tasks] == ["probe"]
    await ctx.activate()
    await asyncio.wait_for(done.wait(), timeout=1)


def test_spawn_task_without_a_loop_is_deferred_until_activation(tmp_path: Path) -> None:
    """`setup()` 是同步入口；登记任务不要求它自己拥有事件循环。"""
    ctx = make_ctx(tmp_path)

    async def body() -> None:  # pragma: no cover - 不会被跑到
        return None

    coro = body()
    ctx.spawn_task(coro, name="probe")
    assert [name for _, name in ctx.pending_tasks] == ["probe"]
    coro.close()


async def test_activation_and_cleanup_follow_lifecycle_order(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    trace: list[str] = []

    async def action(name: str) -> None:
        trace.append(name)

    ctx.on_start(lambda: action("start-1"))
    ctx.on_start(lambda: action("start-2"))
    ctx.add_cleanup(lambda: action("stop-1"))
    ctx.add_cleanup(lambda: action("stop-2"))

    await ctx.activate()
    await ctx.shutdown()
    assert trace == ["start-1", "start-2", "stop-2", "stop-1"]


async def test_shutdown_closes_a_task_that_never_reached_activation(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    closed = False

    async def body() -> None:
        nonlocal closed
        try:
            await asyncio.sleep(0)
        finally:
            closed = True

    coro = body()
    ctx.spawn_task(coro, name="pending")
    await ctx.shutdown()
    assert coro.cr_frame is None
    # 关闭一个尚未开始的协程不会进入它的函数体；重要的是它不再等待 GC 才报警。
    assert closed is False


async def test_cleanup_failure_does_not_skip_later_cleanup(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    cleaned: list[str] = []

    async def fail() -> None:
        cleaned.append("failed")
        raise RuntimeError("cleanup failed")

    async def succeed() -> None:
        cleaned.append("succeeded")

    ctx.add_cleanup(succeed)
    ctx.add_cleanup(fail)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        await ctx.shutdown()
    assert cleaned == ["failed", "succeeded"]


def test_the_two_facades_are_unavailable_before_the_instance_is_ready(tmp_path: Path) -> None:
    """`PluginContext` 要在 `setup()` 之前交给插件，而门面此时还不存在。"""
    ctx = make_ctx(tmp_path)
    for accessor in ("instance", "turns"):
        with pytest.raises(KaryviaError) as caught:
            getattr(ctx, accessor)
        assert caught.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED
