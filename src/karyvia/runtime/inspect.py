"""只读诊断查询：不启动实例也能回答「装了什么」「谁提供了这项能力」。

职责：为 `karyvia plugins list` / `karyvia capabilities` / `karyvia session` 提供**不取实例锁、
不装 orchestrator**的三条查询路径——`inspect_plugins()` 跑到
加载前校验 为止，`inspect_capabilities()` 继续跑一次注册并交出覆盖解析报告，
`open_session_store()` 只装会话存储那一条能力。
不负责：装配可用实例（`bootstrap.py`）、改配置（`config_edit.py`）、格式化输出
（`runtime/cli/commands/`）、发现与加载前校验 的判定（`inventory.py` / `plugin_plan.py`）。

除了配置读取的既有错误诊断，以及插件状态目录的幂等版本标记，本模块不写业务数据。

**为什么不直接调 `bootstrap()`**，三条理由都是硬的：

1. 它**取实例锁**。看一眼装了什么不该与正在跑的实例互斥。
2. 它跑 §10.1 步骤 8 的必需能力校验。一个还没配模型的实例会让 `karyvia capabilities` 以
   「没有指定要用哪个模型」失败，而那恰恰是最需要看一眼能力表的时刻。

**同样地，报告里的冲突不抛出**：`raise_if_failed()` 是启动路径的语义。对这两条命令而言，
冲突正是要印出来的诊断结果。

**一处如实记下的副作用**：加载前校验 的 `check_state_version()` 在插件状态目录**已经存在**
且还没有标记文件时会补写 `.karyvia-state.json`。它与一次真实加载写的内容完全相同、
幂等，也不会为一个从未写盘的插件建目录。除此之外这两条路不写业务数据。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from karyvia.builtins.registry import BUILTIN_MANIFESTS
from karyvia.contracts import CapabilityKind, InstanceId, SessionStore
from karyvia.kernel.config import InstanceLayout, LoadedConfig, load_config
from karyvia.kernel.observability import EventBus, PluginStatus
from karyvia.kernel.plugins import LoadOutcome
from karyvia.kernel.registry import ResolutionReport
from karyvia.sdk import PluginManifest

from .bootstrap import load_config_or_report
from .inventory import PluginInventory
from .plugin_bootstrap import (
    plan_external,
    select_manifests,
    wire_all,
)
from .plugin_context import PluginRuntime
from .plugin_home import GlobalPluginHome
from .plugin_plan import discover_plugins
from .selection import require_sessions
from .startup import StartupResources

__all__ = ["Inspection", "inspect_capabilities", "inspect_plugins", "open_session_store"]


@dataclass(frozen=True, slots=True)
class Inspection:
    """一次只读查询的产物。

    `inventory` 是**修正过的**那一份（加载前校验 落榜项已从 `discovered` 移进 `failures`），
    与会话内 `/plugins` 看到的同源；`report` 只有 `inspect_capabilities()` 会填。
    """

    loaded: LoadedConfig
    inventory: PluginInventory
    report: ResolutionReport | None = None
    #: 逐提供方的加载结果。带 `error` 的那些是「`setup()` 没跑通」，与 `report.failures`
    #: 的「冲突」是两件事：前者的能力**从来没进过** registry，后者进了又被判出局。
    outcomes: tuple[LoadOutcome, ...] = ()

    @property
    def statuses(self) -> tuple[PluginStatus, ...]:
        """投影成 `/plugins` 那张表的形状，按 id 排序。"""
        return self.inventory.statuses()


def _prepare(
    *,
    instance_dir: Path | str | None,
    instance: str | None,
    env: Mapping[str, str] | None,
    overrides: Sequence[str] | None,
    home: Path | None,
    manifests: Sequence[PluginManifest],
) -> tuple[InstanceLayout, LoadedConfig, tuple[PluginManifest, ...], EventBus]:
    """配置 → 内建清单 → 一个不接任何 sink 的事件总线。

    总线是**必须**的（发现与加载前校验 都往上发事件），但这条路上没有订阅者：只读命令不该
    往 `logs/events-<date>.jsonl` 里追加一次「实例启动」。它同时也是「本函数不写文件」
    这条承诺的一部分——`bootstrap` 那条路上的 `write_config_error()` 同样不在这里。
    """
    layout = InstanceLayout.resolve(
        instance_dir=instance_dir, instance=instance, env=env, home=home
    )
    loaded = load_config(
        instance_dir=layout.root, env=env, overrides=overrides, home=home, ensure_dirs=False
    )
    builtins = select_manifests(manifests, loaded.config)
    return layout, loaded, builtins, EventBus(InstanceId(layout.root.name))


def inspect_plugins(
    *,
    instance_dir: Path | str | None = None,
    instance: str | None = None,
    env: Mapping[str, str] | None = None,
    overrides: Sequence[str] | None = None,
    home: Path | None = None,
    manifests: Sequence[PluginManifest] = BUILTIN_MANIFESTS,
) -> Inspection:
    """发现外部插件并跑完加载前校验，**不导入任何 `setup`**。

    `karyvia plugins list` 的数据源。跳过原因、发现阶段的失败与加载前校验 的失败在同一份清单里
    ——用户不需要知道一个插件是在哪个阶段落的榜，他只需要知道它没被加载、以及为什么。

    **异常约定**：配置读不出来（文件坏了、字段非法）时原样抛 `KaryviaError`——那时连
    「装了哪些插件」都无从谈起。插件自身的问题一律进 `inventory.failures`，不抛。
    """
    layout, loaded, builtins, bus = _prepare(
        instance_dir=instance_dir,
        instance=instance,
        env=env,
        overrides=overrides,
        home=home,
        manifests=manifests,
    )
    _, inventory = plan_external(
        discover_plugins(
            loaded.config,
            layout,
            bus,
            entry_points=GlobalPluginHome.resolve(env=env, home=home).entry_points,
        ),
        loaded.config,
        layout,
        loaded.workspace_root,
        bus,
        builtins,
        strict=False,
    )
    return Inspection(loaded=loaded, inventory=inventory)


async def inspect_capabilities(
    *,
    instance_dir: Path | str | None = None,
    instance: str | None = None,
    env: Mapping[str, str] | None = None,
    overrides: Sequence[str] | None = None,
    home: Path | None = None,
    manifests: Sequence[PluginManifest] = BUILTIN_MANIFESTS,
) -> Inspection:
    """跑一次完整注册并交出覆盖解析报告，**不做步骤 8 的必需能力判定**。

    `karyvia capabilities` 的数据源。它真的会跑每个提供方的 `setup()`——`shadowed` 关系只有在
    全部登记都到齐之后才算得出来（覆盖永不由加载顺序决定），没有更便宜的路。
    内建与外部插件走的仍是同一次 `wire_capabilities`。

    **跑完就把 ctx 收掉**：`setup()` 里订阅的事件与派生的后台任务在这条路上没有实例去
    停它们（`AgentInstance.stop()` 不在这条路上），因此本函数自己走一遍
    `RuntimePluginContext.shutdown()`——一条只读命令不该让进程带着几个还在跑的插件任务
    退出。这里没有进入正式生命周期，只需把本次诊断创建的上下文按逆序还回去。

    **异常约定**：与 `inspect_plugins()` 相同——配置问题抛，插件问题进报告。
    """
    layout, loaded, builtins, bus = _prepare(
        instance_dir=instance_dir,
        instance=instance,
        env=env,
        overrides=overrides,
        home=home,
        manifests=manifests,
    )
    plan, inventory = plan_external(
        discover_plugins(
            loaded.config,
            layout,
            bus,
            entry_points=GlobalPluginHome.resolve(env=env, home=home).entry_points,
        ),
        loaded.config,
        layout,
        loaded.workspace_root,
        bus,
        builtins,
        strict=False,
    )
    resources = StartupResources()
    try:
        wiring = await wire_all(
            (*builtins, *plan.manifests),
            loaded.config,
            layout,
            loaded.workspace_root,
            bus,
            PluginRuntime(),
            env,
            resources.contexts,
            external_ids=[manifest.id for manifest in plan.manifests],
        )
    finally:
        await resources.rollback(timeout_ms=loaded.config.plugins.stop_timeout_ms)
    return Inspection(
        loaded=loaded, inventory=inventory, report=wiring.report, outcomes=wiring.outcomes
    )


@asynccontextmanager
async def open_session_store(
    *,
    instance_dir: Path | str | None = None,
    instance: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    manifests: Sequence[PluginManifest] = BUILTIN_MANIFESTS,
) -> AsyncGenerator[tuple[LoadedConfig, SessionStore]]:
    """只把会话存储装起来（`karyvia session` 用）。**不取实例锁、不装 orchestrator。**

    只加载声明了 `SESSION_STORE` 的 manifest，因此不会因为「模型还没配」而让一条只读的
    诊断命令失败——但它仍然走同一条注册路径，插件覆盖了会话存储时 `karyvia session` 看到的
    就是插件那一份。直接 `new JsonlSessionStore(...)` 会让这条命令永远只认内建实现。

    返回异步上下文而不是裸 Store：第三方 Session 插件的 `setup()` 同样可能创建任务或
    订阅事件，命令结束时必须在停止预算内清理，不能把 PluginContext 丢给事件循环退出兜底。
    """
    layout = InstanceLayout.resolve(
        instance_dir=instance_dir, instance=instance, env=env, home=home
    )
    loaded = load_config_or_report(layout, env=env, overrides=None, home=home)
    builtins = select_manifests(manifests, loaded.config)
    selected = tuple(
        manifest
        for manifest in builtins
        if any(decl.kind is CapabilityKind.SESSION_STORE for decl in manifest.capabilities)
    )
    bus = EventBus(InstanceId(layout.root.name))
    # 外部插件走同一条路：覆盖了会话存储的插件，`karyvia session` 看到的就是它那一份。
    # 同样只取声明了 `SESSION_STORE` 的那些——一条只读命令不该因为「模型还没配」而失败。
    plan, _ = plan_external(
        discover_plugins(
            loaded.config,
            layout,
            bus,
            entry_points=GlobalPluginHome.resolve(env=env, home=home).entry_points,
        ),
        loaded.config,
        layout,
        loaded.workspace_root,
        bus,
        builtins,
    )
    external = tuple(
        manifest
        for manifest in plan.manifests
        if any(decl.kind is CapabilityKind.SESSION_STORE for decl in manifest.capabilities)
    )
    resources = StartupResources()
    try:
        wiring = await wire_all(
            (*selected, *external),
            loaded.config,
            layout,
            loaded.workspace_root,
            bus,
            PluginRuntime(),
            env,
            resources.contexts,
            external_ids=[manifest.id for manifest in external],
        )
        wiring.report.raise_if_failed()
        yield loaded, require_sessions(wiring.registry)
    finally:
        await resources.rollback(timeout_ms=loaded.config.plugins.stop_timeout_ms)
