"""外部插件的发现编排与加载计划：阶段 A 的产物（技术方案 §7.3 阶段 A）。

职责：驱动 `D25` 的发现并把结果发成事件；为 `PluginInventory.discovered` 的每一项补上
`A5`（`config_schema` 校验）与 `A7`（`state_version` 与磁盘状态一致），再交给
`kernel.plugins.plan_load_order()` 排出 `A4` 的拓扑序；产出一份有序的 manifest 清单与
失败清单，以及一份**修正过的**（落榜项已从 `discovered` 移进 `failures`）诊断清单。
不负责：读 manifest 与判 id / 平台 / `sdk_range`（`D25` 的 `inventory.py`）、跑 `setup`
与注册（`wiring.py` → `kernel.plugins.load_into`，外部与内建同一条路）
（`bootstrap.approve()` 是唯一调用点）、决定非关键失败之后还做什么（装配根）。

**这是 `R5` 的落点**，与 `inventory.py` / `wiring.py` / `plugin_context.py` 同一条理由：
`PluginManifest` 在 `sdk/`，而 `R2` 禁止 `kernel/` import 它，因此「manifest → `PlanNode`」
的翻译只能发生在唯一同时看得见两侧的这一层。分界线与 `D25` 完全相同：**加一种排序或
校验机制改 `kernel/plugins/loader.py`，加一条 manifest 判定改这里。**

**配置块由调用方交进来**（`config_for`），本模块不认识 `builtin_config_blocks()`：
校验用的那一份必须与 `setup()` 拿到的**同一份**，而只有装配根知道派生默认值怎么合成。

**依赖可以指向内建**：`provided` 里带着本次生效的内建 id，因此一个依赖 `tools-fs` 的插件
不会因为「`tools-fs` 不是外部插件」而落榜。内建在外部插件之前就注册完了，它们不参与排序。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from nucleamind.contracts import ErrorCode, EventName, JsonValue, NucleaError
from nucleamind.kernel.config import InstanceLayout, NucleaConfig
from nucleamind.kernel.observability import EventBus
from nucleamind.kernel.plugins import (
    EntryPointLister,
    PlanNode,
    check_state_version,
    plan_load_order,
    validate_plugin_config,
)
from nucleamind.sdk import PluginManifest

from .inventory import DiscoveredPlugin, PluginFailure, PluginInventory, build_inventory

__all__ = [
    "ExternalPlan",
    "correct_inventory",
    "discover_plugins",
    "plan_external_plugins",
    "plan_plugins",
]


@dataclass(frozen=True, slots=True)
class ExternalPlan:
    """阶段 A 对外部插件的完整结论。

    `manifests` 已按拓扑序排好，可直接接到 `wire_capabilities(manifests=...)` 上；
    `failures` 与 `PluginInventory.failures` 同形，因此能被 `/plugins` 一视同仁地列出来
    ——用户不需要知道一个插件是在发现阶段还是在校验阶段落的榜，他只需要知道它没被加载。
    """

    manifests: tuple[PluginManifest, ...] = ()
    failures: tuple[PluginFailure, ...] = ()
    #: 关键插件的第一条失败。非空即意味着这次启动应当失败（`PLG-004`、`EDG-106`）。
    critical_failure: PluginFailure | None = None


def discover_plugins(
    config: NucleaConfig,
    layout: InstanceLayout,
    bus: EventBus,
    *,
    strict_missing: bool = True,
    entry_points: EntryPointLister = lambda: (),
) -> PluginInventory:
    """§10.1 步骤 3b：发现外部插件并把结果发成事件（`D25`）。

    **发现不导入未启用的插件**：`plugins.enabled` 之外的候选连 manifest 都不会被读，
    因此未启用的插件不产生任何导入开销（`NFR-401`、`DST-002`）。

    候选只来自全局安装目录。实例配置只决定是否启用，不再承担代码发现与安装职责。
    """
    inventory = build_inventory(
        enabled=config.plugins.enabled,
        disabled=config.plugins.disable,
        entry_points=entry_points,
    )
    missing = sorted(
        failure.plugin_id
        for failure in inventory.failures
        if failure.error.code is ErrorCode.PLUGIN_LOAD_FAILED
        and not failure.origin
        and failure.plugin_id in config.plugins.enabled
    )
    if missing and strict_missing:
        raise NucleaError(
            ErrorCode.PLUGIN_LOAD_FAILED,
            "实例启用了尚未全局安装的插件。",
            detail={"plugins": missing, "suggestion": "nm plugins install <来源>"},
        )
    for item in inventory.discovered:
        bus.publish(
            EventName.PLUGIN_DISCOVERED,
            # 载荷的第一个键与内建那次发布同名（`bootstrap.wire_all` 里的 `plugin`）：同一个
            # 事件名出现两种形状，按事件回放的诊断就要为它写两个分支。
            payload={
                "plugin": item.manifest.id,
                "version": item.manifest.version,
                "source": "global_entry_point",
            },
        )
    for failure in inventory.failures:
        bus.publish(
            EventName.PLUGIN_FAILED,
            payload={"plugin": failure.plugin_id, "phase": "discovery"},
            error=failure.error,
        )
    return inventory


def plan_plugins(
    inventory: PluginInventory,
    bus: EventBus,
    *,
    config_for: Callable[[PluginManifest], Mapping[str, JsonValue]],
    state_dir_for: Callable[[str], Path],
    provided: Iterable[str] = (),
) -> tuple[ExternalPlan, PluginInventory]:
    """§10.1 步骤 3c：跑阶段 A 的剩余三步，把结果发成事件（`D27`）。

    交回的第二项是**修正过的**清单：阶段 A 落榜的插件从 `discovered` 移到 `failures`，
    因此 `/plugins` 印出来的「已发现」与真的会被加载的那一批一致——一个配置写错的插件
    显示成 `DISCOVERED` 会让用户以为它在跑。

    **异常约定**：关键插件在阶段 A 失败即原样抛（启动失败，`PLG-004`、`EDG-106`）；
    其余失败发成事件并记进清单，实例继续启动。
    """
    plan = plan_external_plugins(
        inventory.discovered,
        config_for=config_for,
        state_dir_for=state_dir_for,
        provided=provided,
    )
    for failure in plan.failures:
        bus.publish(
            EventName.PLUGIN_FAILED,
            payload={"plugin": failure.plugin_id, "phase": "validate"},
            error=failure.error,
        )
    if plan.critical_failure is not None:
        raise plan.critical_failure.error
    return plan, correct_inventory(inventory, plan)


def correct_inventory(inventory: PluginInventory, plan: ExternalPlan) -> PluginInventory:
    """把阶段 A 落榜的插件从 `discovered` 移进 `failures`。

    单独成函数是因为它有**两个**调用方：`plan_plugins()`（启动路径）与 `D29` 的
    `runtime/inspect.py`（只读诊断路径，它不能用前者——关键插件失败时前者抛异常，
    而一条「列出全部插件及其失败原因」的命令恰恰要把那条失败印出来而不是死掉）。
    两处各写一遍会让「已发现 = 真的会被加载的那一批」在其中一处慢慢失真。
    """
    planned = {manifest.id for manifest in plan.manifests}
    return replace(
        inventory,
        discovered=tuple(item for item in inventory.discovered if item.manifest.id in planned),
        failures=(*inventory.failures, *plan.failures),
    )


def plan_external_plugins(
    discovered: Sequence[DiscoveredPlugin],
    *,
    config_for: Callable[[PluginManifest], Mapping[str, JsonValue]],
    state_dir_for: Callable[[str], Path],
    provided: Iterable[str] = (),
) -> ExternalPlan:
    """跑完阶段 A 的剩余三步，产出有序加载计划。

    `config_for` 交出的是这个插件**最终**看到的配置块（派生默认值 + 用户写的那份），
    与 `setup()` 拿到的必须是同一份——校验一份、执行另一份等于没校验。
    `state_dir_for` 交出 `<instance>/plugins/<id>/`；`check_state_version()` 只在那个目录
    **已经存在**时才做事，不会为一个从未写盘的插件建目录。

    **异常约定**：不抛。每一条问题如实记进 `failures`，后果由装配根按
    `ExternalPlan.critical_failure` 判——与 `build_inventory()` 的「一次报全」同构。
    """
    by_id = {item.manifest.id: item for item in discovered}
    failures: list[PluginFailure] = []
    excluded: list[str] = []

    for plugin_id in sorted(by_id):
        item = by_id[plugin_id]
        manifest = item.manifest
        checks = (
            validate_plugin_config(
                manifest.json_schema,
                config_for(manifest),
                plugin_id=plugin_id,
                pointer=f"/plugins/{plugin_id}/config",
            ),
            check_state_version(
                state_dir_for(plugin_id), manifest.state_version, plugin_id=plugin_id
            ),
        )
        for error in checks:
            if error is None:
                continue
            failures.append(
                PluginFailure(
                    error=error, plugin_id=plugin_id, origin=item.candidate.origin
                )
            )
            # 两项都判、都记（一次报全），但只落榜一次。
            if plugin_id not in excluded:
                excluded.append(plugin_id)

    plan = plan_load_order(
        [
            PlanNode(
                plugin_id=item.manifest.id,
                dependencies=item.manifest.dependencies,
                critical=item.manifest.critical,
            )
            for item in by_id.values()
        ],
        provided=provided,
        excluded=excluded,
    )
    failures.extend(
        PluginFailure(
            error=item.error,
            plugin_id=item.plugin_id,
            origin=by_id[item.plugin_id].candidate.origin,
        )
        for item in plan.failures
    )
    return ExternalPlan(
        manifests=tuple(by_id[plugin_id].manifest for plugin_id in plan.order),
        failures=tuple(failures),
        critical_failure=_first_critical(failures, by_id),
    )


def _first_critical(
    failures: Sequence[PluginFailure], by_id: Mapping[str, DiscoveredPlugin]
) -> PluginFailure | None:
    """第一条关键失败。

    `critical` 从 manifest 上读而不是从失败上读：`PluginFailure` 是诊断形状（与发现阶段
    共用），给它加一个只有阶段 A 才填得上的字段，会让 `/plugins` 那侧多一个恒为 `False`
    的列。
    """
    return next(
        (
            failure
            for failure in failures
            if failure.plugin_id in by_id and by_id[failure.plugin_id].manifest.critical
        ),
        None,
    )
