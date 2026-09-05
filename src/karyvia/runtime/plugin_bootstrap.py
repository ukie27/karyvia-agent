"""插件装配策略：把实例配置、Manifest 与 Runtime 上下文交给统一注册机制。

职责：派生每个提供方的配置块、规划外部插件、筛选内建 Manifest、驱动
``wire_capabilities()``，并把加载结果投影成生命周期。
不负责：实例锁、配置文件错误落盘、事件 sink、必需能力选择和 ``AgentInstance`` 构造；
这些属于顶层 ``bootstrap.py``。

本模块仍是 Runtime 组装的一部分，不是第二个组装根。它把“插件如何加入本次启动”集中起来，
让顶层启动流程只表达资源所有权与阶段顺序。
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path

from karyvia.builtins import cli_entry, commands_core, session_jsonl, tools_fs, tools_shell
from karyvia.builtins.registry import BUILTIN_MANIFESTS
from karyvia.contracts import (
    Builtin,
    CapabilityKind,
    ErrorCode,
    EventName,
    JsonValue,
    KaryviaError,
    Plugin,
    PluginId,
    ProviderId,
)
from karyvia.kernel.config import InstanceLayout, KaryviaConfig
from karyvia.kernel.observability import EventBus
from karyvia.kernel.plugins import LoadOutcome, PluginLifecycle, PluginPhase
from karyvia.sdk import CapabilityDecl, PluginContext, PluginManifest

from .inventory import PluginInventory
from .plugin_context import PluginRuntime, RuntimePluginContext, build_plugin_context
from .plugin_plan import (
    ExternalPlan,
    correct_inventory,
    plan_external_plugins,
    plan_plugins,
)
from .wiring import Wiring, wire_capabilities

__all__ = [
    "build_lifecycles",
    "builtin_config_blocks",
    "capability_filter",
    "config_block_for",
    "plan_external",
    "select_manifests",
    "wire_all",
]


_ENABLED_NAMES: Mapping[str, Callable[[Mapping[str, JsonValue]], Collection[str]]] = {
    "tools-fs": tools_fs.enabled_tool_names,
    "tools-shell": tools_shell.enabled_tool_names,
    "commands-core": commands_core.enabled_command_names,
}

_FILTERED_KINDS = (CapabilityKind.TOOL, CapabilityKind.COMMAND)


def builtin_config_blocks(
    config: KaryviaConfig, layout: InstanceLayout, workspace: Path
) -> dict[str, dict[str, JsonValue]]:
    """返回只有组装根能推导出的内建配置默认值。"""
    return {
        "session-jsonl": {session_jsonl.CONFIG_DIRECTORY_KEY: str(layout.sessions_dir)},
        "tools-fs": {tools_fs.CONFIG_WORKSPACE_KEY: str(workspace)},
        "tools-shell": {tools_shell.CONFIG_WORKSPACE_KEY: str(workspace)},
        "commands-core": {commands_core.CONFIG_PREFIX_KEY: config.routing.command_prefix},
        "cli-entry": {cli_entry.CONFIG_INSTANCE_ID_KEY: layout.root.name},
    }


def config_block_for(
    manifest: PluginManifest,
    config: KaryviaConfig,
    derived: Mapping[str, Mapping[str, JsonValue]],
) -> dict[str, JsonValue]:
    """合并派生默认值与用户配置；用户显式值优先。"""
    block: dict[str, JsonValue] = dict(derived.get(manifest.id, {}))
    block.update(config.plugins.entry(manifest.id).config)
    return block


def capability_filter(
    config: KaryviaConfig, derived: Mapping[str, Mapping[str, JsonValue]]
) -> Callable[[PluginManifest, CapabilityDecl], bool]:
    """构造与 ``setup()`` 使用同一配置块的能力筛选器。"""

    def keep(manifest: PluginManifest, decl: CapabilityDecl) -> bool:
        enabled_names = _ENABLED_NAMES.get(manifest.id)
        if enabled_names is None or decl.kind not in _FILTERED_KINDS:
            return True
        return decl.name in enabled_names(config_block_for(manifest, config, derived))

    return keep


def plan_external(
    inventory: PluginInventory,
    config: KaryviaConfig,
    layout: InstanceLayout,
    workspace: Path,
    bus: EventBus,
    builtins: Sequence[PluginManifest],
    *,
    strict: bool = True,
) -> tuple[ExternalPlan, PluginInventory]:
    """把装配根拥有的最终配置块与状态目录交给阶段 A。"""
    derived = builtin_config_blocks(config, layout, workspace)

    def config_for(manifest: PluginManifest) -> Mapping[str, JsonValue]:
        return config_block_for(manifest, config, derived)

    def state_dir_for(plugin_id: str) -> Path:
        return layout.plugins_dir / plugin_id

    provided = [manifest.id for manifest in builtins]
    if strict:
        return plan_plugins(
            inventory,
            bus,
            config_for=config_for,
            state_dir_for=state_dir_for,
            provided=provided,
        )
    plan = plan_external_plugins(
        inventory.discovered,
        config_for=config_for,
        state_dir_for=state_dir_for,
        provided=provided,
    )
    return plan, correct_inventory(inventory, plan)


def select_manifests(
    manifests: Sequence[PluginManifest], config: KaryviaConfig
) -> tuple[PluginManifest, ...]:
    """去掉按提供方禁用的内建 Manifest；CLI 入口不允许被禁用。"""
    disabled = set(config.plugins.disable)
    kept: list[PluginManifest] = []
    for manifest in manifests:
        if manifest.id not in disabled:
            kept.append(manifest)
            continue
        if any(decl.kind is CapabilityKind.CLI_ENTRY for decl in manifest.capabilities):
            raise KaryviaError(
                ErrorCode.CONFIG_INVALID,
                "CLI 入口不可禁用：不装任何 Channel 插件时它是唯一的本地交互入口。",
                detail={
                    "pointer": "/plugins/disable",
                    "plugin": manifest.id,
                    "suggestion": "从 plugins.disable 里删掉它；要换实现请用插件覆盖 "
                    "builtin:stdio。",
                },
            )
    return tuple(kept)


async def wire_all(
    manifests: Sequence[PluginManifest],
    config: KaryviaConfig,
    layout: InstanceLayout,
    workspace: Path,
    bus: EventBus,
    runtime: PluginRuntime,
    env: Mapping[str, str] | None,
    contexts: list[RuntimePluginContext],
    *,
    builtin_cli_only: bool = False,
    external_ids: Collection[str] = (),
) -> Wiring:
    """让内建与外部插件经同一 Host、配置和能力筛选路径完成注册。

    外部插件失败保留在 ``Wiring.outcomes``，由诊断呈现并继续装配。内建清单是宿主自己
    发布的基线，不属于第三方故障隔离边界；其 setup 失败时保留原始错误并终止启动。
    """
    derived = builtin_config_blocks(config, layout, workspace)
    keep = capability_filter(config, derived)
    external = set(external_ids)

    def provider_for(manifest: PluginManifest) -> ProviderId:
        return Plugin(PluginId(manifest.id)) if manifest.id in external else Builtin()

    def keep_with_cli(manifest: PluginManifest, decl: CapabilityDecl) -> bool:
        if (
            builtin_cli_only
            and decl.kind is CapabilityKind.CLI_ENTRY
            and manifest.id != "cli-entry"
        ):
            return False
        return keep(manifest, decl)

    def context_for(manifest: PluginManifest) -> PluginContext:
        entry = config.plugins.entry(manifest.id)
        ctx = build_plugin_context(
            manifest.id,
            config=config_block_for(manifest, config, derived),
            secrets=entry.secrets,
            state_dir=layout.plugins_dir / manifest.id,
            bus=bus,
            runtime=runtime,
            env=env,
            workspace=workspace,
            config_path=layout.config_path,
        )
        # 构造点与生产类型都在 Runtime；这个窄化让启动事务能接管任务和订阅。
        assert isinstance(ctx, RuntimePluginContext)  # noqa: S101
        contexts.append(ctx)
        bus.publish(EventName.PLUGIN_DISCOVERED, payload={"plugin": manifest.id})
        return ctx

    wiring = await wire_capabilities(
        manifests=manifests,
        context_for=context_for,
        provider_for=provider_for,
        keep=keep_with_cli,
    )
    _raise_builtin_failure(manifests, wiring.outcomes)
    return wiring


def _raise_builtin_failure(
    manifests: Sequence[PluginManifest], outcomes: Sequence[LoadOutcome]
) -> None:
    """内建装配错误属于宿主启动错误，不降级成后续的泛化能力缺失。"""
    builtin_ids = {manifest.id for manifest in BUILTIN_MANIFESTS}
    for manifest, outcome in zip(manifests, outcomes, strict=True):
        if manifest.id in builtin_ids and outcome.error is not None:
            raise outcome.error


def build_lifecycles(
    manifests: Sequence[PluginManifest], outcomes: Sequence[LoadOutcome]
) -> tuple[PluginLifecycle, ...]:
    """按 Manifest/加载结果的位置对应关系建立正式生命周期。"""
    lifecycles: list[PluginLifecycle] = []
    for manifest, outcome in zip(manifests, outcomes, strict=False):
        lifecycle = PluginLifecycle(
            plugin_id=manifest.id,
            dependencies=manifest.dependencies,
        )
        lifecycle.advance(PluginPhase.VALIDATED)
        if outcome.error is None:
            lifecycle.advance(PluginPhase.LOADED)
        else:
            lifecycle.fail(outcome.error)
        lifecycles.append(lifecycle)
    return tuple(lifecycles)
