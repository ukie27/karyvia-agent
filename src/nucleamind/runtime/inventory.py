"""插件清单：把发现出来的候选翻译成已校验的 manifest（技术方案 §7.1–§7.3 阶段 A 前段）。

职责：按 `plugins.enabled` / `plugins.disable` 决定哪些候选值得读，读到的原始数据经
`sdk.parse_manifest()` 变成 `PluginManifest`，再判定 id 一致、平台匹配与 `sdk_range`
兼容；产出可直接喂给诊断的 `PluginInventory`。
不负责：发现候选（`kernel/plugins/discovery.py`）、导入 `setup` 与注册能力
（`D27`，走 `runtime/wiring.py` 那条既有路径）、依赖拓扑、`config_schema` 逐字段校验、
插件运行上下文的构造。

**这是 `R5` 的落点**，与 `wiring.py` / `introspection.py` 同一条理由：manifest 的类型与
校验在 `sdk/`，而 `R2` 禁止 `kernel/` import 它，因此「候选 → manifest」的翻译只能发生在
唯一同时看得见两侧的这一层。`kernel/` 里不出现第二套 manifest 校验（`D06` 的约定）。

**启用判定发生在读取之前**（§7.1 的「发现与启用分离」）：未列入 `plugins.enabled` 的候选
连 manifest 都不读，因此不产生任何导入开销（`NFR-401`、`DST-002`）。可观察的后果是
它们的 `version` 是空串——那不是漏填，正是这条设计的证据。

**已启用但被 `plugins.disable` 关掉的候选是例外**（`D30`）：它的 manifest 仍然被读一次，
只为知道它曾经覆盖过什么（`BAS-004`，见 `_disabled()`）。闸门没有变——`plugins.enabled`
仍然是「会不会被读」的唯一入口，`disable` 只决定「读了之后跑不跑」。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, cast

from nucleamind.contracts import ErrorCode, NucleaError, PluginId
from nucleamind.kernel.observability import PluginState, PluginStatus
from nucleamind.kernel.plugins import (
    EntryPointLister,
    PluginCandidate,
    discover,
    read_candidate,
)
from nucleamind.sdk import PluginManifest, parse_manifest

if TYPE_CHECKING:  # pragma: no cover - 仅为注解。
    from nucleamind.contracts import JsonValue

__all__ = [
    "DiscoveredPlugin",
    "PluginFailure",
    "PluginInventory",
    "SkipReason",
    "SkippedPlugin",
    "build_inventory",
]


class SkipReason(StrEnum):
    """一个候选**不是失败**但也不会被加载的三种原因。

    与失败分开：这三种情况下用户什么都不用修，改配置或换平台即可；而失败意味着某处写错了。
    """

    NOT_ENABLED = "not_enabled"
    DISABLED = "disabled"
    PLATFORM_MISMATCH = "platform_mismatch"


#: 每种跳过原因给用户看的一句话。放一张表而不是散在判定处，是为了让
#: `/plugins` 的输出与这里的判定同源。
_SKIP_REASONS: Mapping[SkipReason, str] = {
    SkipReason.NOT_ENABLED: "未列入 plugins.enabled",
    SkipReason.DISABLED: "已被 plugins.disable 禁用",
    SkipReason.PLATFORM_MISMATCH: "manifest 声明的平台与当前平台不符",
}


@dataclass(frozen=True, slots=True)
class DiscoveredPlugin:
    """一个已启用且 manifest 校验通过的插件。`D27` 的加载计划就从这些开始。"""

    candidate: PluginCandidate
    manifest: PluginManifest


@dataclass(frozen=True, slots=True)
class SkippedPlugin:
    """一个不会被加载的候选。`manifest` 只在读过之后才有（平台不匹配那一档）。"""

    candidate: PluginCandidate
    reason: SkipReason
    manifest: PluginManifest | None = None


@dataclass(frozen=True, slots=True)
class PluginFailure:
    """一条失败。`plugin_id` 为空串表示全局候选目录本身的冲突。"""

    error: NucleaError
    plugin_id: str = ""
    origin: str = ""


@dataclass(frozen=True, slots=True)
class PluginInventory:
    """一次发现 + 阶段 A 前段校验的完整产物。

    三段分开而不是塞进一个 union：诊断要同时回答「哪些会被加载」「哪些没被加载、为什么」
    「哪里写错了」，而这三个问题的补救动作完全不同。
    """

    discovered: tuple[DiscoveredPlugin, ...] = ()
    skipped: tuple[SkippedPlugin, ...] = ()
    failures: tuple[PluginFailure, ...] = ()

    def statuses(self) -> tuple[PluginStatus, ...]:
        """投影成 `Diagnostics.plugins_source` 要的形状，按 id 排序。

        状态只用 `PluginState` 已有的取值（`D12` 定死「不发明第二套生命周期 taxonomy」）：
        已校验但尚未加载仍是 `DISCOVERED`——`LOADED` 要等 `D27` 真的跑过 `setup`。
        """
        rows: list[PluginStatus] = [
            PluginStatus(
                plugin_id=PluginId(item.manifest.id),
                version=item.manifest.version,
                state=PluginState.DISCOVERED,
                capabilities=tuple(
                    f"{decl.kind.value}:{decl.name}" for decl in item.manifest.capabilities
                ),
            )
            for item in self.discovered
        ]
        rows.extend(
            PluginStatus(
                plugin_id=PluginId(item.candidate.plugin_id),
                version="" if item.manifest is None else item.manifest.version,
                state=PluginState.DISABLED,
                reason=_SKIP_REASONS[item.reason],
            )
            for item in self.skipped
        )
        rows.extend(
            PluginStatus(
                plugin_id=PluginId(item.plugin_id),
                version="",
                state=PluginState.FAILED,
                failure=item.error,
                failed_phase="discovery",
            )
            for item in self.failures
            if item.plugin_id
        )
        return tuple(sorted(rows, key=lambda row: (str(row.plugin_id), row.state.value)))

    def to_json(self) -> dict[str, JsonValue]:
        """`nm plugins --json` 与诊断快照用。失败里不含未归属项以外的额外结构。"""
        return {
            "discovered": [item.manifest.id for item in self.discovered],
            "skipped": [
                {"plugin_id": item.candidate.plugin_id, "reason": item.reason.value}
                for item in self.skipped
            ],
            "failures": [
                {
                    "plugin_id": item.plugin_id,
                    "origin": item.origin,
                    "code": item.error.code.value,
                    "message": item.error.user_message,
                }
                for item in self.failures
            ],
        }


def _as_manifest(candidate: PluginCandidate, raw: object) -> PluginManifest:
    """把候选交回来的东西变成 manifest。

    两种形态都接受：entry point 可以交出 `Mapping`，也可以交出插件自己构造好的
    `PluginManifest`。后者已经过 pydantic 校验，原样采纳；前者一律走
    `parse_manifest()`——那是不可信输入的**唯一**入口（`CMP-001`：失败带字段路径）。
    """
    if isinstance(raw, PluginManifest):
        return raw
    if isinstance(raw, Mapping):
        # TOML 交回来的键必为 str，但静态上它只是 `Mapping[Unknown, Unknown]`。
        # `isinstance` 是这里的运行时检查，`cast` 才有支撑（`AGENTS.md` 原则 6）。
        items = cast("Mapping[object, object]", raw).items()
        return parse_manifest(
            {str(key): value for key, value in items}, origin=candidate.origin
        )
    raise NucleaError(
        ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED,
        "manifest 必须是 PluginManifest 或一份映射。",
        detail={
            "plugin_id": candidate.plugin_id,
            "origin": candidate.origin,
            "type": type(raw).__name__,
        },
    )


def _validate(candidate: PluginCandidate, manifest: PluginManifest) -> None:
    """id 一致、`sdk_range` 兼容。**异常约定**：不满足即抛 `NucleaError`。

    **id 必须等于候选名**（entry point 的 name / 目录名 / 文件名），不是「以 manifest
    为准」：`plugins.enabled` 写的是候选名，静默采纳 manifest 里的另一个 id 会让启用清单
    指不到任何东西，而用户看到的现象是「我明明启用了它」。
    """
    if manifest.id != candidate.plugin_id:
        raise NucleaError(
            ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED,
            "manifest 里的 id 与它的来源名不一致。",
            detail={
                "plugin_id": candidate.plugin_id,
                "origin": candidate.origin,
                "manifest_id": manifest.id,
                "source": "global_entry_point",
            },
        )
    if not manifest.sdk_compatible:
        raise NucleaError(
            ErrorCode.PLUGIN_SDK_INCOMPATIBLE,
            "插件声明的 sdk_range 与当前 SDK 不兼容；不带病加载。",
            detail={
                "plugin_id": manifest.id,
                "origin": candidate.origin,
                "sdk_range": manifest.sdk_range,
            },
        )


def _disabled(
    candidate: PluginCandidate, failures: list[PluginFailure], *, enabled: set[str]
) -> SkippedPlugin:
    """一个被 `plugins.disable` 关掉的候选。**已启用的那些仍然读一次 manifest**（`D30`）。

    读它只为一件事：知道它**曾经覆盖过**什么。`BAS-004` 不允许「禁用覆盖者之后内建
    悄悄复活」，而那条隐式回退之所以看起来自然，正是因为不读 manifest 就没人知道发生过
    覆盖。判定在 `runtime/plugin_disable.py`，本函数只负责把 manifest 交出去。

    **只读已启用的那些**，因此 §7.1 的「未启用即零导入开销」一个字都没松动：
    `plugins.enabled` 仍然是「会不会被读」的唯一闸门，`disable` 只决定「读了之后跑不跑」。
    一个只写在 `disable` 里、从来没被启用过的候选不可能覆盖过任何东西，读它没有意义。

    **读不出来不是启动失败**：一份坏掉的 manifest 记进 `failures`（`/plugins` 会印出来），
    插件仍然按禁用处理。它的代价是这一个插件的 `on_disable` 判定做不了——如实记着，
    比为了一个已经被关掉的插件让实例起不来更合理。
    """
    manifest: PluginManifest | None = None
    if candidate.plugin_id in enabled:
        try:
            manifest = _as_manifest(candidate, read_candidate(candidate))
            _validate(candidate, manifest)
        except NucleaError as error:
            failures.append(
                PluginFailure(
                    error=error, plugin_id=candidate.plugin_id, origin=candidate.origin
                )
            )
            manifest = None
    return SkippedPlugin(candidate=candidate, reason=SkipReason.DISABLED, manifest=manifest)


def build_inventory(
    *,
    enabled: Sequence[str] = (),
    disabled: Sequence[str] = (),
    entry_points: EntryPointLister = lambda: (),
    platform: str | None = None,
) -> PluginInventory:
    """发现 → 按启用清单筛 → 读 manifest → 校验，产出清单。

    生产路径把全局安装目录作为 ``entry_points`` 传入。
    `platform` 交给 `matches_platform()`，默认 `sys.platform`——平台矩阵测试需要在一个
    平台上断言另一个平台的插件会被跳过。

    **异常约定**：不抛。一份写错的插件不该让实例起不来；本函数把每一条问题如实记进
    `failures`，与 `validate_config()` 的「一次报全」同构。
    """
    scan = discover(entry_points=entry_points)
    failures = [PluginFailure(error=error) for error in scan.failures]
    discovered: list[DiscoveredPlugin] = []
    skipped: list[SkippedPlugin] = []

    enabled_ids = set(enabled)
    disabled_ids = set(disabled)
    candidate_ids = {candidate.plugin_id for candidate in scan.candidates}
    for plugin_id in sorted(enabled_ids - candidate_ids):
        failures.append(
            PluginFailure(
                error=NucleaError(
                    ErrorCode.PLUGIN_LOAD_FAILED,
                    "实例启用了尚未安装的插件。",
                    detail={"plugin_id": plugin_id},
                ),
                plugin_id=plugin_id,
            )
        )
    for candidate in scan.candidates:
        # 顺序是「先筛后读」，这正是「未启用即零导入开销」的实现方式。`disable` 压过
        # `enabled`：两张表都写了它时，禁用是那个更晚、更明确的意图。
        if candidate.plugin_id in disabled_ids:
            skipped.append(_disabled(candidate, failures, enabled=enabled_ids))
            continue
        if candidate.plugin_id not in enabled_ids:
            skipped.append(SkippedPlugin(candidate=candidate, reason=SkipReason.NOT_ENABLED))
            continue
        try:
            manifest = _as_manifest(candidate, read_candidate(candidate))
            _validate(candidate, manifest)
        except NucleaError as error:
            failures.append(
                PluginFailure(
                    error=error, plugin_id=candidate.plugin_id, origin=candidate.origin
                )
            )
            continue
        if not manifest.matches_platform(platform):
            skipped.append(
                SkippedPlugin(
                    candidate=candidate,
                    reason=SkipReason.PLATFORM_MISMATCH,
                    manifest=manifest,
                )
            )
            continue
        discovered.append(DiscoveredPlugin(candidate=candidate, manifest=manifest))

    return PluginInventory(
        discovered=tuple(discovered), skipped=tuple(skipped), failures=tuple(failures)
    )
