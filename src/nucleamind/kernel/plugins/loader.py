"""阶段 A 的三项机制：依赖拓扑、配置校验与状态版本（技术方案 §7.3 阶段 A 的 A4/A5/A7）。

职责：把一组 `(id, dependencies)` 排成一份确定的加载顺序并指出缺失与环路；
把一份配置块对着一份 JSON Schema 校验成带字段路径的错误；把一个插件声明的
`state_version` 与它状态目录里已记录的版本比对。
不负责：认识 manifest（`R2` 禁止 `kernel/` import `sdk/`，翻译在 `runtime/plugin_plan.py`）、
发现候选（`discovery.py`）、跑 `setup` 与事务性注册（`builtin_loader.py`），或判断最终
能力集合是否足以运行实例（那是 Runtime 装配根的职责）。

**本模块与 `builtin_loader.py` 的分工就是阶段 A 与阶段 B**：前者只看声明、一个插件模块
都不导入（§7.3 的「不导入插件实现」是阶段 A 的定义性约束），后者才 import `setup`。
把两者写成一个文件会让「校验期没有导入」退化成一条要人遵守的纪律。

**加载顺序不是覆盖顺序**（`EDG-102`）：拓扑序只保证「被依赖者先 `setup`」，谁覆盖谁永远
只由 manifest 的 `overrides` 与 `kernel/registry/resolution.py` 决定。同层内按 id 字典序
定序，只为让诊断与测试可复现。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, cast

from nucleamind.contracts import ErrorCode, JsonValue, NucleaError

__all__ = [
    "STATE_FILE",
    "STATE_VERSION_KEY",
    "LoadPlan",
    "PlanFailure",
    "PlanNode",
    "check_state_version",
    "plan_load_order",
    "validate_plugin_config",
]

#: 插件状态目录里记录 `state_version` 的那个文件。名字带前导点且以 `nucleamind` 起头：
#: 那个目录归插件所有（`EDG-505`），宿主往里放东西必须一眼可辨认出不是插件自己写的。
STATE_FILE: Final = ".nucleamind-state.json"

#: 状态标记文件里的键。
STATE_VERSION_KEY: Final = "state_version"

#: 单次配置校验最多报几条问题，与 `kernel/turn/invoker._MAX_SCHEMA_PROBLEMS` 同一条理由：
#: 全报会让一个拼错的键刷出上百行 detail，而用户需要的是前几条。
_MAX_SCHEMA_PROBLEMS: Final = 8


class _SchemaProblem(Protocol):
    """`jsonschema.ValidationError` 里本模块用到的两个字段（形状同 `invoker._SchemaProblem`）。

    `jsonschema` 没有 `py.typed`，它交出来的一切在类型层都是未知（`AGENTS.md` 原则 6：
    在边界类型化动态数据）。声明一个只含**我们真正读的字段**的 Protocol，
    再在 `_compile()` 一处收口。
    """

    @property
    def absolute_path(self) -> Iterable[object]: ...

    @property
    def message(self) -> str: ...


class _Validator(Protocol):
    """编译好的校验器。同样只声明本模块用到的那一个方法。"""

    def iter_errors(self, instance: object) -> Iterable[_SchemaProblem]: ...


@dataclass(frozen=True, slots=True)
class PlanNode:
    """一个待加载插件在**依赖排序**这件事上的全部事实。

    kernel 侧对 manifest 的投影，与 `LoadRequest` 同构地只留排序需要的两项：`R2` 够不着
    `PluginManifest`，而排序本来也不需要知道它声明了什么能力。
    """

    plugin_id: str
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlanFailure:
    """一个插件在阶段 A 就落榜的原因。"""

    plugin_id: str
    error: NucleaError


@dataclass(frozen=True, slots=True)
class LoadPlan:
    """阶段 A 的产物：有序加载计划 + 失败清单（§7.3 的字面表述）。

    两段都保留而不是让失败者从 `order` 里静默消失：`/plugins` 要回答的是「谁没被加载、
    为什么」，而那正是一次「我明明启用了它」的排查所需要的全部信息。
    """

    order: tuple[str, ...] = ()
    failures: tuple[PlanFailure, ...] = ()


def plan_load_order(
    nodes: Sequence[PlanNode],
    *,
    provided: Iterable[str] = (),
    excluded: Iterable[str] = (),
) -> LoadPlan:
    """把节点排成拓扑序（`A4`，`PLG-003`）。

    `provided` 是「已经在场、不参与排序」的 id：内建提供方就是这样进来的——一个插件依赖
    `tools-fs` 是合法的，而内建在外部插件之前就已经注册完了。

    `excluded` 是「已经在别处判定为落榜」的 id（配置不合 schema、状态版本对不上）。
    它们不进结果、也**不在这里再记一条失败**（调用方已经记过），但它们的依赖方仍然按级联
    落榜——那正是把它们交进来而不是直接从 `nodes` 里删掉的理由：删掉的话，依赖方看到的
    是「依赖不存在」，而事实是「依赖存在但坏了」。

    三种落榜各有各的诊断：

    - **依赖缺失**：`dependencies` 里的 id 既不在本批、也不在 `provided` 里。
    - **依赖成环**：错误里带整条环路（`PLG-003` 明确要求「指出环路」）。
    - **级联**：依赖的插件自己落榜了。仍然加载它等于让 `setup()` 在一个说好会存在的能力
      不存在的前提下跑，那种失败发生在插件代码里，比在这里说清楚难查得多。

    **同层内按 id 字典序**，因此同一份配置每次得到同一个顺序。加载顺序永不决定覆盖
    （`EDG-102`）——定序只为可复现。

    **异常约定**：不抛。落榜也是结论，与 `build_inventory()` 的「一次报全」同构。
    """
    known = {node.plugin_id: node for node in nodes}
    available = set(provided)
    failures: list[PlanFailure] = []
    blocked: set[str] = {plugin_id for plugin_id in excluded if plugin_id in known}
    reported = set(blocked)

    for node in sorted(known.values(), key=lambda item: item.plugin_id):
        missing = [
            dependency
            for dependency in node.dependencies
            if dependency not in known and dependency not in available
        ]
        if missing:
            blocked.add(node.plugin_id)
            failures.append(
                PlanFailure(
                    plugin_id=node.plugin_id,
                    error=NucleaError(
                        ErrorCode.PLUGIN_LOAD_FAILED,
                        "插件依赖的其他插件没有被加载。",
                        detail={
                            "plugin_id": node.plugin_id,
                            "missing": sorted(missing),
                            "suggestion": "把它们装上并列进 plugins.enabled。",
                        },
                    ),
                )
            )

    order = _kahn(known, skip=blocked)
    failures.extend(_cycle_failures(known, ordered=set(order), blocked=blocked))
    failures.extend(
        _cascade_failures(
            known,
            ordered=set(order),
            reported=reported | {item.plugin_id for item in failures},
        )
    )
    return LoadPlan(order=order, failures=tuple(sorted(failures, key=lambda item: item.plugin_id)))


def _kahn(known: Mapping[str, PlanNode], *, skip: set[str]) -> tuple[str, ...]:
    """按 id 字典序取可用节点的 Kahn 排序。`skip` 里的节点及其下游一律不进结果。"""
    # `provided` 里的依赖在这里已经被滤掉：它们不参与排序（内建早已注册完）。
    pending = {
        plugin_id: {dependency for dependency in node.dependencies if dependency in known}
        for plugin_id, node in known.items()
        if plugin_id not in skip
    }
    ordered: list[str] = []
    resolved: set[str] = set()
    while True:
        # 依赖落在 `skip` 里、或落在某个还没被排上的节点上的，永远等不到——
        # 循环因此自然停住，环与级联都留给调用方去分类。
        ready = sorted(
            plugin_id for plugin_id, needs in pending.items() if needs <= resolved
        )
        if not ready:
            return tuple(ordered)
        for plugin_id in ready:
            del pending[plugin_id]
        ordered.extend(ready)
        resolved.update(ready)


def _cycle_failures(
    known: Mapping[str, PlanNode], *, ordered: set[str], blocked: set[str]
) -> list[PlanFailure]:
    """给环里的每个节点各记一条失败，错误里带整条环路。"""
    stranded = {
        plugin_id for plugin_id in known if plugin_id not in ordered and plugin_id not in blocked
    }
    failures: list[PlanFailure] = []
    for plugin_id in sorted(stranded):
        cycle = _find_cycle(known, plugin_id, stranded)
        if cycle is None:
            continue
        failures.append(
            PlanFailure(
                plugin_id=plugin_id,
                error=NucleaError(
                    ErrorCode.PLUGIN_LOAD_FAILED,
                    "插件依赖成环，无法确定加载顺序。",
                    detail={"plugin_id": plugin_id, "cycle": list(cycle)},
                ),
            )
        )
    return failures


def _find_cycle(
    known: Mapping[str, PlanNode], start: str, stranded: set[str]
) -> tuple[str, ...] | None:
    """从 `start` 出发找一条回到自己的路径。找不到说明它只是环的下游（由级联记录）。"""
    stack: list[tuple[str, tuple[str, ...]]] = [(start, (start,))]
    seen: set[str] = set()
    while stack:
        current, path = stack.pop()
        for dependency in sorted(known[current].dependencies):
            if dependency == start:
                return (*path, start)
            if dependency in stranded and dependency not in seen:
                seen.add(dependency)
                stack.append((dependency, (*path, dependency)))
    return None


def _cascade_failures(
    known: Mapping[str, PlanNode], *, ordered: set[str], reported: set[str]
) -> list[PlanFailure]:
    """依赖方随被依赖方一起落榜。"""
    return [
        PlanFailure(
            plugin_id=plugin_id,
            error=NucleaError(
                ErrorCode.PLUGIN_LOAD_FAILED,
                "插件依赖的其他插件自己没能加载。",
                detail={
                    "plugin_id": plugin_id,
                    "blocked_by": sorted(
                        dependency
                        for dependency in known[plugin_id].dependencies
                        if dependency in known and dependency not in ordered
                    ),
                },
            ),
        )
        for plugin_id in sorted(set(known) - ordered - reported)
    ]


def validate_plugin_config(
    schema: Mapping[str, JsonValue] | None,
    config: Mapping[str, JsonValue],
    *,
    plugin_id: str,
    pointer: str,
) -> NucleaError | None:
    """按 manifest 的 `config_schema` 校验一个插件的配置块（`A5`，`CMP-001`）。

    没有 schema 就没有可校验的东西——`None` 时直接放行，而不是「未声明即禁止一切键」：
    `config_schema` 是可选字段，把它当成隐含的空对象会让所有没写它的插件配置全部报错。

    `pointer` 是这个配置块在配置文档里的 JSON Pointer（`/plugins/<id>/config`），
    问题的路径拼在它后面——用户要的是「去 `config.json` 的哪一行改」，
    而不是一个相对于插件私有 schema 的路径。

    **异常约定**：不抛，返回 `NucleaError | None`。失败是阶段 A 的一条记录而不是一次崩溃，
    插件配置写错时实例仍要能起来（`PLG-004`）。

    **`jsonschema` 惰性 import**：这是全项目第二个接触点（另一处是
    `kernel/turn/invoker._compile`），两处都惰性——`import kernel.plugins` 出现在
    `nm plugins` 这类只读路径上，而 `jsonschema` 的导入是几十毫秒（`NFR-405`）。
    """
    if not schema:
        return None
    compiled = _compile(dict(schema), plugin_id=plugin_id)
    if isinstance(compiled, NucleaError):
        return compiled
    try:
        problems = list(compiled.iter_errors(dict(config)))
    except Exception as exc:
        return NucleaError(
            ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED,
            "校验插件配置时校验器自身失败。",
            detail={"plugin_id": plugin_id, "exception": type(exc).__name__},
        )
    if not problems:
        return None
    return NucleaError(
        ErrorCode.CONFIG_INVALID,
        "插件配置不符合它声明的 config_schema。",
        detail={
            "plugin_id": plugin_id,
            "pointer": pointer,
            "problems": [
                {
                    "pointer": pointer
                    + "".join(f"/{part}" for part in problem.absolute_path),
                    "message": problem.message,
                }
                for problem in problems[:_MAX_SCHEMA_PROBLEMS]
            ],
        },
    )


def _compile(schema: Mapping[str, JsonValue], *, plugin_id: str) -> _Validator | NucleaError:
    """编译一份 JSON Schema。这是本模块唯一接触 `jsonschema` 的地方。

    **schema 自己写错了是插件作者的 bug**，因此报 `PLUGIN_MANIFEST_UNSUPPORTED`
    （声明的问题）而不是 `CONFIG_INVALID`（用户配置的问题）——两者的补救动作不同。

    `cast` 有运行时检查支撑（`callable(getattr(...))`），这是 `AGENTS.md` 原则 6 对 `cast`
    的要求：`jsonschema` 没有 `py.typed`，收口必须在这一处完成。
    """
    import jsonschema.validators as js  # boundary: 无类型标注的第三方库，形状在本函数内收口

    document = dict(schema)
    try:
        cls = js.validator_for(document)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        # `check_schema` 必须显式调用，理由同 `invoker._compile`：不调用的话一个写错的
        # schema 要等到校验配置时才炸，异常从 `iter_errors` 里逸出，「约定不抛」当场失效。
        cls.check_schema(document)  # pyright: ignore[reportUnknownMemberType]
        compiled: object = cls(document)
    except Exception as exc:
        return NucleaError(
            ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED,
            "插件声明的 config_schema 本身不是合法的 JSON Schema。",
            detail={"plugin_id": plugin_id, "exception": type(exc).__name__},
        )
    if not callable(getattr(compiled, "iter_errors", None)):
        return NucleaError(
            ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED,
            "jsonschema 交回的对象没有 iter_errors，无法用作校验器。",
            detail={"plugin_id": plugin_id, "type": type(compiled).__name__},
        )
    return cast(_Validator, compiled)


def check_state_version(
    state_dir: Path, declared: int, *, plugin_id: str
) -> NucleaError | None:
    """比对声明的 `state_version` 与状态目录里记着的那个（`A7`，`EDG-503`）。

    三种情形：

    - **状态目录不存在** → 什么都不做，也**不建目录**。一个从未写盘的插件不该因为一次
      校验而在磁盘上留痕（与 `ctx.state_dir` 的惰性创建是同一条约定）。
    - **目录在、标记不在** → 记下当前版本（这是它第一次带着状态被看到）。
    - **标记在但与声明不符** → 失败，且**一个字节都不改写状态**。

    **版本不一致一律拒绝加载，升与降都是**：迁移函数是 P2 的能力（§10.5），在它存在之前
    「让插件带着为另一个版本写的状态跑起来」与「静默改写版本号」都在拿用户数据赌一把，
    而 `EDG-503` 要的恰好是「升级失败时保住旧状态」。补救动作是显式的：清掉状态目录，
    或把插件换回原来的版本。

    **异常约定**：不抛。标记文件读不动、不是 JSON、版本不是整数，一律折成失败返回——
    那份文件本来就在一个插件可以随手写坏的目录里。
    """
    if not state_dir.is_dir():
        return None
    marker = state_dir / STATE_FILE
    if not marker.exists():
        return _write_state_version(marker, declared, plugin_id=plugin_id)
    try:
        document: object = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return NucleaError(
            ErrorCode.PLUGIN_LOAD_FAILED,
            "插件的状态版本标记读不出来。",
            detail={
                "plugin_id": plugin_id,
                "file": str(marker),
                "reason": type(exc).__name__,
                "suggestion": "删掉这个文件让它按当前版本重记，或清空该插件的状态目录。",
            },
        )
    # `json.loads` 交回的东西在类型层是未知，`isinstance` 是这里唯一的运行时检查
    # （`AGENTS.md` 原则 6：cast 必须有运行时检查支撑）。
    recorded = (
        cast("Mapping[object, object]", document).get(STATE_VERSION_KEY)
        if isinstance(document, Mapping)
        else None
    )
    if not isinstance(recorded, int) or isinstance(recorded, bool):
        return NucleaError(
            ErrorCode.PLUGIN_LOAD_FAILED,
            "插件的状态版本标记形状不对。",
            detail={"plugin_id": plugin_id, "file": str(marker)},
        )
    if recorded != declared:
        return NucleaError(
            ErrorCode.PLUGIN_LOAD_FAILED,
            "插件声明的 state_version 与磁盘上的状态不一致；旧状态原样保留。",
            detail={
                "plugin_id": plugin_id,
                "declared": declared,
                "recorded": recorded,
                "state_dir": str(state_dir),
                "suggestion": "P0 还没有状态迁移机制：清空该插件的状态目录，"
                "或换回声明了那个版本的插件。",
            },
        )
    return None


def _write_state_version(marker: Path, version: int, *, plugin_id: str) -> NucleaError | None:
    """记下当前版本。写失败折成失败返回——记不下来就等于下次仍然无从比对。"""
    try:
        marker.write_text(
            json.dumps({STATE_VERSION_KEY: version}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        return NucleaError(
            ErrorCode.PERSISTENCE_WRITE_FAILED,
            "写不进插件的状态版本标记。",
            detail={"plugin_id": plugin_id, "file": str(marker), "reason": type(exc).__name__},
        )
    return None
