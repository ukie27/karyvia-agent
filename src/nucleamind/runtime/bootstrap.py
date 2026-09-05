"""启动序列：从「实例在哪」到一个可以接受输入的 `AgentInstance`（技术方案 §10.1）。

职责：按 §10.1 的十步装配一个实例——取锁、加载配置、挑选内建清单、发现并规划外部插件、
注册能力、解析覆盖、校验必需能力、装出 `OrchestratorDeps` 与两个门面。
不负责：跑它（`instance.py`）、解析 argv（`runtime/cli/`）、阶段 A 的三项判定
（`plugin_plan.py` + `kernel/plugins/loader.py`）、插件装配策略（`plugin_bootstrap.py`）、
生成首次运行的配置。

**插件装配策略已收口在 `plugin_bootstrap.py`**：内建配置块派生、阶段 A 桥接、
能力筛选与 `setup()` 配置同源都在那里。本模块只按顺序调用它们，并用 `StartupResources`
保证任何一步失败时，已经产生的任务、订阅和 sink 都会在实例锁释放前回滚。

**`EDG-108` 在这里落地两次**：配置试图禁用 CLI 入口时直接拒绝启动；覆盖 CLI 的提供方
没能交出实现时，用同一批 manifest 再装一次、但只让内建提供 CLI 入口（`BAS-010` 的
「强制回落」，且它不允许被配置成 `fail_start`）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import date
from pathlib import Path

from nucleamind.builtins.registry import BUILTIN_MANIFESTS
from nucleamind.contracts import (
    CapabilityKind,
    CliEntry,
    EventName,
    InstanceId,
    JsonValue,
    ModelInfo,
    ModelProvider,
    NucleaError,
    SessionStore,
)
from nucleamind.kernel.config import (
    InstanceLayout,
    InstanceLock,
    LoadedConfig,
    load_config,
)
from nucleamind.kernel.observability import (
    Diagnostics,
    EventBus,
    JsonlFileSink,
    MemoryRingSink,
    PluginState,
    PluginStatus,
    write_config_error,
)
from nucleamind.kernel.plugins import PluginLifecycle, channels_from, cli_entry_from
from nucleamind.kernel.routing import (
    ConcurrencyPolicy,
    DedupCache,
    Dispatcher,
    SessionScheduler,
    build_command_index,
)
from nucleamind.kernel.turn import (
    HookRouter,
    OrchestratorDeps,
    ToolExecutor,
    TurnCompactionPolicy,
    TurnOrchestrator,
    TurnReceipt,
    bindings_from,
    context_providers_from,
    tools_from,
)
from nucleamind.sdk import PluginManifest

from .instance import AgentInstance, Closer, outbound_router
from .introspection import build_instance_view, build_turn_control
from .inventory import PluginInventory
from .plugin_bootstrap import (
    build_lifecycles,
    builtin_config_blocks,
    capability_filter,
    plan_external,
    select_manifests,
    wire_all,
)
from .plugin_context import PluginRuntime, RuntimePluginContext
from .plugin_home import GlobalPluginHome
from .plugin_plan import discover_plugins
from .selection import (
    missing_capability,
    require_sessions,
    select_model,
    select_recall,
    select_turn_compactor,
)
from .startup import StartupResources
from .wiring import Wiring

#: `PluginManifest` 从这里再导出一次：`embed/` 只能 import `contracts/` 与 `runtime/`
#: （`R5`），而 `open_instance(manifests=...)` 的类型在 `sdk/`。转发比让门面收 `object` 诚实。
__all__ = [
    "BUILTIN_MANIFESTS",
    "PluginManifest",
    "bootstrap",
    "builtin_config_blocks",
    "capability_filter",
    "load_config_or_report",
    "plan_external",
    "require_sessions",
    "select_manifests",
    "select_recall",
    "wire_all",
]


def load_config_or_report(
    layout: InstanceLayout,
    *,
    env: Mapping[str, str] | None,
    overrides: Sequence[str] | None,
    home: Path | None,
) -> LoadedConfig:
    """§10.1 步骤 2。解析失败时把错误写进 `logs/`（`EDG-501` 的后半句）。

    **这是 `write_config_error()` 唯一的调用点**：配置解析失败发生在事件总线建起来之前，
    做成 sink 就等于把这条需求推回它无法成立的时序里。写盘失败不掩盖
    原始错误——它 best-effort，返回 `False` 而已。
    """
    try:
        return load_config(
            instance_dir=layout.root, env=env, overrides=overrides, home=home, ensure_dirs=False
        )
    except NucleaError as error:
        write_config_error(layout.config_error_log_path(date.today()), error)
        raise


async def bootstrap(
    *,
    instance_dir: Path | str | None = None,
    instance: str | None = None,
    env: Mapping[str, str] | None = None,
    overrides: Sequence[str] | None = None,
    home: Path | None = None,
    manifests: Sequence[PluginManifest] = BUILTIN_MANIFESTS,
    acquire_lock: bool = True,
) -> AgentInstance:
    """装配一个实例（§10.1 的十步）。返回的实例尚未 `start()`。

    **异常约定**：任何一步失败都原样抛 `NucleaError`——启动失败必须是显式的。已经取到的
    锁在失败路径上被释放，否则一次启动失败会砖掉实例目录直到下次回收。
    """
    # 后续启动步骤共享同一实例目录，因此先解析布局并持有单实例锁。
    layout = InstanceLayout.resolve(instance_dir=instance_dir, instance=instance, env=env, home=home)
    layout.ensure()
    lock = InstanceLock(layout.lock_path).acquire() if acquire_lock else None
    try:
        plugin_home = GlobalPluginHome.resolve(env=env, home=home)
        with plugin_home.registration():
            plugin_home.register_instance(layout.root)
        return await _bootstrap_with_lock(
            layout, lock, env=env, overrides=overrides, home=home, manifests=manifests
        )
    except BaseException:
        if lock is not None:
            lock.release()
        raise


async def _bootstrap_with_lock(
    layout: InstanceLayout,
    lock: InstanceLock | None,
    *,
    env: Mapping[str, str] | None,
    overrides: Sequence[str] | None,
    home: Path | None,
    manifests: Sequence[PluginManifest],
) -> AgentInstance:
    """在已持有实例锁的前提下完成配置、插件和运行对象组装。

    与 `bootstrap()` 分开后，锁的释放路径只需一层 `try`，所有启动异常都能走同一清理逻辑。
    """
    # 配置决定清理预算、日志 sink、插件选择和所有运行策略，必须先完成加载。
    loaded = load_config_or_report(layout, env=env, overrides=overrides, home=home)
    config = loaded.config
    resources = StartupResources()
    try:
        return await _build_instance(
            layout,
            lock,
            loaded=loaded,
            env=env,
            home=home,
            manifests=manifests,
            resources=resources,
        )
    except BaseException:
        await resources.rollback(timeout_ms=config.plugins.stop_timeout_ms)
        raise


async def _build_instance(
    layout: InstanceLayout,
    lock: InstanceLock | None,
    *,
    loaded: LoadedConfig,
    env: Mapping[str, str] | None,
    home: Path | None,
    manifests: Sequence[PluginManifest],
    resources: StartupResources,
) -> AgentInstance:
    """在启动资源事务内完成插件装配，并在成功时把所有权交给实例。"""
    config = loaded.config
    instance_id = InstanceId(layout.root.name)

    # JSONL sink 的开关来自配置，因此只能在配置成功后接入；更早的事件只保留在内存环中。
    bus = EventBus(instance_id)
    ring = MemoryRingSink()
    bus.subscribe(ring, name="memory-ring")
    if config.logging.file_enabled:
        sink = JsonlFileSink(layout.events_log_path)
        bus.subscribe(sink, name="jsonl-file")
        resources.add_closer(_closer(sink.close))
    bus.publish(EventName.INSTANCE_STARTING, payload={"instance": str(instance_id)})

    # 先筛选内建能力，再把外部插件接到同一份加载计划中。
    selected = select_manifests(manifests, config)

    # 发现外部插件：未列入 `plugins.enabled` 的候选连 manifest 都不读。
    inventory = discover_plugins(
        config,
        layout,
        bus,
        entry_points=GlobalPluginHome.resolve(env=env, home=home).entry_points,
    )
    # 阶段 A 校验与拓扑排序。产出接到诊断上，`/plugins` 因此列得出
    # 候选、跳过原因与两个阶段的失败。
    plan, inventory = plan_external(
        inventory, config, layout, loaded.workspace_root, bus, selected
    )
    external_ids = [manifest.id for manifest in plan.manifests]
    # 内建在前、外部插件按拓扑序在后。顺序只保证「被依赖者先 setup」，覆盖由 manifest 的
    # `overrides` 决定（`EDG-102`）。
    all_manifests = (*selected, *plan.manifests)

    # 内建与外部插件共用注册、覆盖解析和 Registry 冻结路径（`SDK-007`）。
    runtime = PluginRuntime()
    attempt = resources.plugin_checkpoint()
    wiring = await wire_all(
        all_manifests,
        config,
        layout,
        loaded.workspace_root,
        bus,
        runtime,
        env,
        resources.contexts,
        external_ids=external_ids,
    )
    if cli_entry_from(wiring.registry) is None and any(
        decl.kind is CapabilityKind.CLI_ENTRY
        for manifest in all_manifests
        for decl in manifest.capabilities
    ):
        # `EDG-108`/`BAS-010`：覆盖 CLI 的提供方没交出实现，强制回落到内建实现。
        # 重装一次而不是打补丁——半装好的 registry 已经冻结，改它比重来更容易出错。
        outcomes = await resources.rollback_plugins(
            attempt, timeout_ms=config.plugins.stop_timeout_ms
        )
        cleanup_error = next((item.error for item in outcomes if item.error is not None), None)
        if cleanup_error is not None:
            raise cleanup_error
        wiring = await wire_all(
            all_manifests,
            config,
            layout,
            loaded.workspace_root,
            bus,
            runtime,
            env,
            resources.contexts,
            builtin_cli_only=True,
            external_ids=external_ids,
        )
    for outcome in wiring.outcomes:
        bus.publish(
            EventName.PLUGIN_LOADED if outcome.error is None else EventName.PLUGIN_LOAD_FAILED,
            payload={"provider": str(outcome.provider)},
            error=outcome.error,
        )
    # Registry 冻结后才能可靠地选择启动必需的单值能力。
    registry = wiring.registry
    sessions = require_sessions(registry)
    model, model_id, model_info = select_model(registry, config)
    cli = cli_entry_from(registry)
    if cli is None:
        raise missing_capability("CLI_ENTRY", "没有本地交互入口（BAS-009）。")

    instance = _assemble(
        layout=layout,
        loaded=loaded,
        bus=bus,
        ring=ring,
        wiring=wiring,
        inventory=inventory,
        sessions=sessions,
        model=model,
        model_id=model_id,
        turn_compactor=select_turn_compactor(registry, config),
        model_info=model_info,
        cli=cli.value,
        contexts=tuple(resources.contexts),
        external_plugin_ids=frozenset(external_ids),
        lifecycles=build_lifecycles(all_manifests, wiring.outcomes),
        runtime=runtime,
        lock=lock,
        closers=tuple(resources.closers),
    )
    resources.transfer()
    return instance


def _plugin_status_source(
    inventory: PluginInventory, lifecycles: Sequence[PluginLifecycle]
) -> Callable[[], Sequence[PluginStatus]]:
    """`/plugins` 的数据源：发现清单叠上运行期状态。

    清单本身只知道「已发现 / 已跳过 / 已失败」——它在 `setup()` 跑之前就产出了，因此一个
    已经在跑的插件在那里仍然写着 `discovered`。运行期的真相在 `PluginLifecycle` 上，
    **状态由它的 `state` 投影给出**（`PHASE_STATES` 是唯一那张表，这里不另写一份映射）。

    只覆盖清单里已有的 id：`lifecycles` 同时含内建，而内建不是插件（见调用点的注释）。
    每次调用重算一次——生命周期是可变的，缓存一份快照会让 `/plugins` 永远停在启动那一刻。
    """
    by_id = {lifecycle.plugin_id: lifecycle for lifecycle in lifecycles}

    def statuses() -> Sequence[PluginStatus]:
        rows: list[PluginStatus] = []
        for status in inventory.statuses():
            lifecycle = by_id.get(str(status.plugin_id))
            if lifecycle is None or status.state is PluginState.FAILED:
                # 阶段 A 落榜的插件根本没有生命周期，而已经记下的失败不该被一个
                # 后续生命周期状态盖掉。
                rows.append(status)
                continue
            rows.append(
                replace(
                    status,
                    state=lifecycle.state,
                    failure=lifecycle.error,
                    failed_phase=(
                        None
                        if lifecycle.failed_phase is None
                        else lifecycle.failed_phase.value
                    ),
                )
            )
        return tuple(rows)

    return statuses


def _closer(fn: Callable[[], None]) -> Closer:
    """把一个同步收尾包成 `Closer`。"""

    async def close() -> None:
        fn()

    return close


def _assemble(
    *,
    layout: InstanceLayout,
    loaded: LoadedConfig,
    bus: EventBus,
    ring: MemoryRingSink,
    wiring: Wiring,
    inventory: PluginInventory,
    sessions: SessionStore,
    model: ModelProvider,
    model_id: str,
    turn_compactor: TurnCompactionPolicy,
    model_info: ModelInfo | None,
    cli: CliEntry,
    contexts: tuple[RuntimePluginContext, ...],
    external_plugin_ids: frozenset[str],
    lifecycles: tuple[PluginLifecycle, ...],
    runtime: PluginRuntime,
    lock: InstanceLock | None,
    closers: Sequence[Closer],
) -> AgentInstance:
    """把已取回的能力装成 `OrchestratorDeps` 与 `AgentInstance`。

    三个超时、并发策略、队列上限、去重参数与命令前缀**全部来自配置**——这里是那些字段
    唯一的消费点，`kernel/` 里的默认值只在没有装配根的测试里生效。
    """
    config = loaded.config
    registry = wiring.registry
    channels = tuple(
        (binding.value.channel_id, binding.value) for binding in channels_from(registry)
    )
    by_channel = dict(channels)

    deliver = outbound_router(by_channel, bus)

    def report_failure(error: NucleaError) -> None:
        """Hook 失败的去向。`publish()` 有返回值，`on_failure` 要的是 `None`——
        写成 lambda 会把那个 `RuntimeEvent` 当成返回值交回去。"""
        bus.publish(EventName.PLUGIN_FAILED, error=error)

    executor = ToolExecutor(tools_from(registry))
    router = HookRouter(
        bindings_from(registry),
        observer_timeout_ms=config.hooks.observer_timeout_ms,
        interceptor_timeout_ms=config.hooks.interceptor_timeout_ms,
        on_failure=report_failure,
    )
    deps = OrchestratorDeps(
        instance_id=bus.instance_id,
        bus=bus,
        sessions=sessions,
        model=model,
        tools=executor,
        hooks=router,
        dispatcher=Dispatcher(build_command_index(registry), prefix=config.routing.command_prefix),
        scheduler=SessionScheduler[TurnReceipt](
            policy=ConcurrencyPolicy(config.routing.session_concurrency),
            queue_max_size=config.routing.queue_max_size,
        ),
        dedup=DedupCache(
            capacity=config.routing.dedup_capacity, ttl_ms=config.routing.dedup_ttl_ms
        ),
        limits=loaded.limits,
        model_id=model_id,
        turn_compactor=turn_compactor,
        # 模型看得见的工具集与调度用的工具集同源——`ToolExecutor.specs` 是唯一来源。
        tool_specs=executor.specs,
        context_providers=context_providers_from(registry),
        model_info=model_info,
        context_provider_timeout_ms=config.context.provider_timeout_ms,
        deliver=deliver,
        memory=select_recall(registry, config),
        retry=config.retry.to_policy(),
    )
    orchestrator = TurnOrchestrator(deps)
    diagnostics = Diagnostics(
        events=ring,
        capabilities_source=lambda: wiring.report,
        # `/plugins` 的数据源。内建不出现在这里——它们是 `Builtin()` 提供方而不是
        # 插件，`/capabilities` 才是回答「这项能力谁提供的」的地方。
        plugins_source=_plugin_status_source(inventory, lifecycles),
    )
    # 两个实例门面都通过 callable 读取实时状态，理由见 `introspection.py`。
    # 配置文档交的是 `${VAR}` 字面量那一棵树（`/config` 的脱敏靠这条结构性保证）。
    runtime.ready(
        instance_view=build_instance_view(
            commands_source=lambda: build_command_index(registry),
            diagnostics=diagnostics,
            config_source=lambda: _config_document(loaded),
            sessions=sessions,
        ),
        turn_control=build_turn_control(orchestrator),
    )
    return AgentInstance(
        instance_id=bus.instance_id,
        layout=layout,
        config=loaded,
        bus=bus,
        diagnostics=diagnostics,
        registry=registry,
        report=wiring.report,
        deps=deps,
        orchestrator=orchestrator,
        cli_entry=cli,
        channels=channels,
        outcomes=wiring.outcomes,
        contexts=contexts,
        external_plugin_ids=external_plugin_ids,
        lifecycles=lifecycles,
        stop_timeout_ms=config.plugins.stop_timeout_ms,
        channel_concurrency=config.routing.channel_concurrency,
        runtime=runtime,
        lock=lock,
        closers=tuple(closers),
    )


def _config_document(loaded: LoadedConfig) -> Mapping[str, JsonValue]:
    """`/config` 看到的那棵树。`to_json()` 的 `config` 段就是它。"""
    document = loaded.to_json()["config"]
    return document if isinstance(document, Mapping) else {}
