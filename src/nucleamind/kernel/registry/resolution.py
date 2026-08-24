"""覆盖解析：从登记集合算出谁最终生效（技术方案 §6.1，需求 `EDG-102`、`EDG-107`、`NFR-502`）。

职责：定义 `ResolutionReport`（`active` / `shadowed` / `disabled` / `failures` 四段可序列化
报告）与 `Resolution`，实现纯函数 `resolve()` 及把结论写回注册表并冻结的 `resolve_into()`。
不负责：登记与存储（`capability.py`）、读配置决定谁被禁用（`kernel/config/`，`D10`）、
按 `on_override_failure` 回落到内建（`runtime/`，`D27`）。本模块不读文件、不访问网络。

解析是**一次性全量**计算，不是逐条判定：冲突语义（同名重复、覆盖目标缺失、两个插件抢
同一目标、SINGLETON 多实现）只有在看到全部登记之后才能判定。逐条判定必然退化成
「先注册的赢」，那正是 `EDG-102` 要堵的路。

四条本模块自己定的规则，规格没有明写但必须固定下来：

- **失败不抛出，进 `failures`**。一次解析要把全部冲突都报出来，用户改一条配置就能看到
  下一条冲突。是否要求整份报告零冲突由调用方决定；实例启动只检查最终必需能力是否齐全，
  只读检查或更严格的调用方仍可使用 `raise_if_failed()`。
- **冲突的双方都不生效**。同名重复且无覆盖声明时，选任何一边都是在替用户做决定；
  两边都退出，用户会立刻看到能力缺失并去解决冲突，而不是运行在一个「大概是对的」实例上。
- **覆盖目标被禁用等同于不存在**。目标不在生效候选里就报 `override_target_missing`，
  不静默把覆盖降级为新增注册（§6.1 规则 2）。`D10` 引入按能力禁用后若要放宽，
  应在那里显式论证，而不是在这里留一条隐式回退。
- **SINGLETON 的分组键是 kind 本身**，与 name 无关。`register_session_store("sqlite", …)`
  和 `register_session_store("jsonl", …)` 是同一个槽位的两份实现，不是两个槽位——
  按 `(kind, name)` 分组会让它们各自「唯一」，SINGLETON 也就名存实亡。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from nucleamind.contracts import (
    CapabilityArity,
    CapabilityKind,
    CapabilityRef,
    ErrorCode,
    JsonValue,
    NucleaError,
    ProviderId,
    provider_sort_key,
)

from .capability import CapabilityRegistry, Registration

__all__ = [
    "Resolution",
    "ResolutionReport",
    "SuppressedCapabilities",
    "resolve",
    "resolve_into",
]

#: 覆盖目标的定位键：`(kind, 目标提供方, 目标能力名)`。kind 取自**声明覆盖的那一方**，
#: 因为覆盖目标串里不带 kind（技术方案 §7.2：重复编码只会多出一处可以对不上的信息）。
_TargetKey = tuple[CapabilityKind, ProviderId, str]

#: 「按能力禁用」的输入形状：定位键 -> 原因。与按提供方禁用共用 `_TargetKey`，因为要指认
#: 的是同一个东西——一项具体能力。`D30` 引入，用于 `on_disable=leave_missing`
#: （`BAS-004`）：被禁用的覆盖者留下的空缺不该由内建**隐式**补上。
SuppressedCapabilities = Mapping[_TargetKey, str]

#: 唯一性分组键：`(kind 值, 分组名)`。SINGLETON 的分组名恒为空串，见模块 docstring。
_SlotKey = tuple[str, str]


def _slot_key(ref: CapabilityRef) -> _SlotKey:
    """唯一性分组键。SINGLETON 按 kind 整体分组，其余按 `(kind, name)`。"""
    if ref.kind.arity is CapabilityArity.SINGLETON:
        return (ref.kind.value, "")
    return (ref.kind.value, ref.name)


def _ref_json(ref: CapabilityRef) -> dict[str, JsonValue]:
    """`CapabilityRef` 的 JSON 形态。`provider` 用 `str()`，与覆盖目标串同一套编码。"""
    return {
        "kind": ref.kind.value,
        "name": ref.name,
        "provider": str(ref.provider),
        "version": ref.version,
    }


def _error_json(error: NucleaError) -> dict[str, JsonValue]:
    """`NucleaError` 的 JSON 形态。`detail` 构造时已脱敏，可直接输出。"""
    return {
        "code": error.code.value,
        "category": error.category.value,
        "message": error.user_message,
        "detail": dict(error.detail),
    }


class ResolutionReport:
    """解析结论，可序列化，供 `nm capabilities` 与诊断接口输出（`NFR-502`、`PLG-006`）。

    四段是互斥且完备的：每一条登记恰好落在 `active`、`shadowed` 的被覆盖侧、`disabled`
    或某条 `failures` 所述的冲突里。用类而不是 dataclass 是因为四个字段都要在构造时定序，
    而 `__post_init__` 里改 frozen dataclass 的字段要走 `object.__setattr__`，
    那比直接写构造函数更难读。
    """

    __slots__ = ("_active", "_disabled", "_failures", "_shadowed")

    def __init__(
        self,
        *,
        active: Sequence[CapabilityRef] = (),
        shadowed: Sequence[tuple[CapabilityRef, CapabilityRef]] = (),
        disabled: Sequence[tuple[CapabilityRef, str]] = (),
        failures: Sequence[NucleaError] = (),
    ) -> None:
        self._active = tuple(active)
        self._shadowed = tuple(shadowed)
        self._disabled = tuple(disabled)
        self._failures = tuple(failures)

    @property
    def active(self) -> tuple[CapabilityRef, ...]:
        """最终生效的能力，按 `(priority, provider, name)` 排序。"""
        return self._active

    @property
    def shadowed(self) -> tuple[tuple[CapabilityRef, CapabilityRef], ...]:
        """`(被覆盖者, 覆盖者)`。被覆盖的实现仍然可见，只是不生效（§6.1 规则 4）。"""
        return self._shadowed

    @property
    def disabled(self) -> tuple[tuple[CapabilityRef, str], ...]:
        """`(能力, 原因)`。来自调用方传入的禁用集合，不是解析自己的判定。"""
        return self._disabled

    @property
    def failures(self) -> tuple[NucleaError, ...]:
        """全部冲突。冲突各方不生效，但不自动代表整个实例不能启动。"""
        return self._failures

    @property
    def ok(self) -> bool:
        """没有任何冲突。"""
        return not self._failures

    def raise_if_failed(self) -> None:
        """有冲突就抛第一条，供要求整份报告零冲突的调用方使用。

        只抛第一条是刻意的：完整清单在 `failures` 里，启动流程应当先把它整份打印出来
        再中止。异常只负责中止，不负责报告——把 N 条冲突塞进一个异常消息，
        用户在终端里看到的是一团无法逐条定位的文本。
        """
        if self._failures:
            raise self._failures[0]

    def to_json(self) -> dict[str, JsonValue]:
        """整份报告的 JSON 形态（`NFR-502`：报告必须可序列化）。"""
        return {
            "active": [_ref_json(ref) for ref in self._active],
            "shadowed": [
                {"capability": _ref_json(hidden), "overridden_by": _ref_json(winner)}
                for hidden, winner in self._shadowed
            ],
            "disabled": [
                {"capability": _ref_json(ref), "reason": reason}
                for ref, reason in self._disabled
            ],
            "failures": [_error_json(error) for error in self._failures],
        }

    def __repr__(self) -> str:
        return (
            f"ResolutionReport(active={len(self._active)}, shadowed={len(self._shadowed)}, "
            f"disabled={len(self._disabled)}, failures={len(self._failures)})"
        )


class Resolution:
    """解析的完整产物：给人看的 `report` 和给注册表用的 `active`。

    两者分开是因为它们的受众不同：`report` 只含 `CapabilityRef`（可序列化、可打印），
    `active` 带着 `payload`（不可序列化，但装配时要用）。把 payload 塞进报告会让
    `to_json()` 需要为任意对象定义序列化规则，那是一条没有尽头的路。
    """

    __slots__ = ("_active", "_report")

    def __init__(self, report: ResolutionReport, active: Sequence[Registration]) -> None:
        self._report = report
        self._active = tuple(active)

    @property
    def report(self) -> ResolutionReport:
        """可序列化的结论。"""
        return self._report

    @property
    def active(self) -> tuple[Registration, ...]:
        """生效的登记（含 payload），按 `(priority, provider, name)` 排序。"""
        return self._active


def _partition_disabled(
    registrations: Sequence[Registration],
    disabled: Mapping[ProviderId, str],
    suppressed: SuppressedCapabilities,
) -> tuple[list[tuple[int, Registration]], list[tuple[CapabilityRef, str]]]:
    """按提供方与按能力切出被禁用的登记。返回 `(存活项, 禁用记录)`。

    两种粒度合在一处判定，因为它们的后果完全相同——被切掉的登记既不生效也不参与冲突
    判定，但都留在报告的 `disabled` 段里。提供方级先判：一个整体被关掉的提供方，再逐条
    说明它的哪一项能力「另外还被抑制了一次」只会让诊断更难读。
    """
    live: list[tuple[int, Registration]] = []
    disabled_out: list[tuple[CapabilityRef, str]] = []
    for index, registration in enumerate(registrations):
        ref = registration.ref
        reason = disabled.get(ref.provider) or suppressed.get((ref.kind, ref.provider, ref.name))
        if reason is None:
            live.append((index, registration))
        else:
            disabled_out.append((ref, reason))
    return live, disabled_out


def _apply_overrides(
    live: Sequence[tuple[int, Registration]],
) -> tuple[set[int], list[tuple[CapabilityRef, CapabilityRef]], list[NucleaError]]:
    """处理全部 `overrides` 声明。返回 `(应排除的下标, 遮蔽关系, 失败)`。

    排除的来源有三种：覆盖目标缺失的孤儿、抢同一目标的多个覆盖者、被成功覆盖的目标。
    """
    by_target: dict[_TargetKey, tuple[int, Registration]] = {}
    for index, registration in live:
        key = (registration.ref.kind, registration.ref.provider, registration.ref.name)
        by_target.setdefault(key, (index, registration))

    excluded: set[int] = set()
    failures: list[NucleaError] = []
    claims: dict[_TargetKey, list[tuple[int, Registration]]] = {}

    for index, registration in live:
        target = registration.override_target
        if target is None:
            continue
        target_provider, target_name = target
        key = (registration.ref.kind, target_provider, target_name)
        if key not in by_target:
            excluded.add(index)
            failures.append(
                NucleaError(
                    ErrorCode.CAPABILITY_OVERRIDE_TARGET_MISSING,
                    "覆盖目标不存在；覆盖不会降级为新增注册。",
                    detail={
                        "capability": registration.ref.target,
                        "kind": registration.ref.kind.value,
                        "target": registration.overrides,
                    },
                    capability=registration.ref,
                )
            )
            continue
        claims.setdefault(key, []).append((index, registration))

    shadowed: list[tuple[CapabilityRef, CapabilityRef]] = []
    for key in sorted(claims, key=lambda item: (item[0].value, provider_sort_key(item[1]), item[2])):
        group = claims[key]
        target_index, target_registration = by_target[key]
        if len(group) > 1:
            excluded.update(index for index, _ in group)
            failures.append(
                NucleaError(
                    ErrorCode.CAPABILITY_OVERRIDE_CONFLICT,
                    "多个提供方声明覆盖同一目标；请在配置中显式选择其一。",
                    detail={
                        "target": target_registration.ref.target,
                        "kind": key[0].value,
                        "claimants": sorted(reg.ref.target for _, reg in group),
                    },
                    capability=target_registration.ref,
                )
            )
            continue
        _, winner = group[0]
        excluded.add(target_index)
        shadowed.append((target_registration.ref, winner.ref))

    return excluded, shadowed, failures


def _apply_arity(
    remaining: Sequence[tuple[int, Registration]],
) -> tuple[list[Registration], list[NucleaError]]:
    """按 arity 裁决剩余登记。返回 `(生效项, 失败)`。

    MULTI 全部生效；MULTI_UNIQUE 与 SINGLETON 只允许一份，多于一份则双方都不生效。
    """
    groups: dict[_SlotKey, list[Registration]] = {}
    for _, registration in remaining:
        groups.setdefault(_slot_key(registration.ref), []).append(registration)

    active: list[Registration] = []
    failures: list[NucleaError] = []
    for slot in sorted(groups):
        group = groups[slot]
        kind = group[0].ref.kind
        if kind.arity is CapabilityArity.MULTI or len(group) == 1:
            active.extend(group)
            continue
        failures.append(
            NucleaError(
                ErrorCode.PLUGIN_REGISTRATION_CONFLICT,
                "同一能力槽位有多份实现，且没有任何一方声明覆盖；冲突各方均不生效。",
                detail={
                    "kind": kind.value,
                    "arity": kind.arity.value,
                    "slot": slot[1] or kind.value,
                    "claimants": sorted(item.ref.target for item in group),
                },
                capability=group[0].ref,
            )
        )
    return active, failures


def resolve(
    registrations: Sequence[Registration],
    *,
    disabled: Mapping[ProviderId, str] | None = None,
    suppressed: SuppressedCapabilities | None = None,
) -> Resolution:
    """从登记集合算出生效集合与报告。纯函数，不改动任何入参。

    `disabled` 是「按提供方禁用」的输入（provider -> 原因），由配置层在 `D10` 填充。
    `suppressed` 是「按能力禁用」的输入（`(kind, provider, name)` -> 原因），由 `D30` 的
    `on_disable=leave_missing` 填充。被禁用的登记既不生效也不参与冲突判定，但会出现在
    报告的 `disabled` 段里——「装了但没启用」和「没装」对排查是两回事。

    **按能力禁用不是给覆盖开的后门**：它只能让一项能力消失，不能让某一方赢。谁覆盖谁
    仍然只由 manifest 的 `overrides` 决定（`EDG-102`）。
    """
    live, disabled_out = _partition_disabled(
        registrations, dict(disabled or {}), dict(suppressed or {})
    )
    excluded, shadowed, override_failures = _apply_overrides(live)
    remaining = [(index, item) for index, item in live if index not in excluded]
    active, arity_failures = _apply_arity(remaining)

    ordered = sorted(active, key=lambda item: item.sort_key)
    report = ResolutionReport(
        active=[item.ref for item in ordered],
        shadowed=sorted(shadowed, key=lambda pair: (pair[0].sort_key, pair[1].sort_key)),
        disabled=sorted(disabled_out, key=lambda pair: pair[0].sort_key),
        failures=[*override_failures, *arity_failures],
    )
    return Resolution(report, ordered)


def resolve_into(
    registry: CapabilityRegistry,
    *,
    disabled: Mapping[ProviderId, str] | None = None,
    suppressed: SuppressedCapabilities | None = None,
) -> ResolutionReport:
    """解析注册表并**冻结**它，返回报告。这是启动期的正常入口。

    有冲突时依然冻结：注册表冻结与实例能否启动是两件事，调用方拿到报告后可继续使用
    没有冲突的能力，或显式调用 `raise_if_failed()` 采用严格策略。冻结失败的注册表反而会
    让诊断路径也读不到生效集合，而那正是出问题时最需要看的东西。

    **异常约定**：registry 已冻结抛 `KERNEL_INVARIANT_VIOLATED`（由 `freeze()` 抛出）。
    """
    resolution = resolve(registry.registrations, disabled=disabled, suppressed=suppressed)
    registry.freeze(resolution.active)
    return resolution.report
