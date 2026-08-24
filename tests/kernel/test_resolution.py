"""覆盖解析的测试（`D06` 验收表第 1–6 行 + 报告序列化与排序稳定性）。

验收表逐条对齐，每条一个测试：

| 场景 | 测试 |
| --- | --- |
| 同 kind 同 name 重复注册且无 overrides | `test_duplicate_name_without_override_fails` |
| `overrides` 目标不存在 | `test_override_target_missing_*` |
| 两个插件覆盖同一目标 | `test_two_plugins_overriding_same_target_conflict` |
| SINGLETON kind 注册两个实现 | `test_singleton_with_two_implementations_fails` |
| CONTEXT / HOOK 同名并存 | `test_multi_kinds_coexist_sorted` |
| 覆盖成功 | `test_successful_override_records_shadowed` |
| 批次中途抛异常 | `test_registry.py`（属于注册表机制） |
| 冻结后写入 | `test_registry.py` |
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from nucleamind.contracts import (
    Builtin,
    CapabilityKind,
    CapabilityRef,
    ErrorCode,
    NucleaError,
    Plugin,
    PluginId,
    ProviderId,
)
from nucleamind.kernel.registry import (
    CapabilityRegistry,
    Registration,
    ResolutionReport,
    resolve,
    resolve_into,
)

ACME = Plugin(PluginId("acme"))
OTHER = Plugin(PluginId("other"))
ZULU = Plugin(PluginId("zulu"))


def make(
    kind: CapabilityKind,
    name: str,
    provider: ProviderId = Builtin(),
    *,
    priority: int = 100,
    overrides: str | None = None,
) -> Registration:
    """构造一条登记。`payload` 用 `ref.target` 便于在断言里认出是哪一条。"""
    ref = CapabilityRef(kind=kind, name=name, provider=provider)
    return Registration(ref=ref, payload=ref.target, priority=priority, overrides=overrides)


def codes(report: ResolutionReport) -> list[str]:
    """报告里全部失败码，便于逐条断言。"""
    return [error.code.value for error in report.failures]


def targets(refs: Sequence[CapabilityRef]) -> list[str]:
    """一组 `CapabilityRef` 的覆盖目标串。"""
    return [ref.target for ref in refs]


# ----------------------------------------------------------------- 第 1 行：同名重复


def test_duplicate_name_without_override_fails() -> None:
    """同 kind 同 name 重复注册且无 overrides —— 启动错误。"""
    resolution = resolve(
        [
            make(CapabilityKind.TOOL, "fs.read", Builtin()),
            make(CapabilityKind.TOOL, "fs.read", ACME),
        ]
    )

    assert not resolution.report.ok
    assert codes(resolution.report) == [ErrorCode.PLUGIN_REGISTRATION_CONFLICT.value]


def test_conflicting_parties_are_both_inactive() -> None:
    """冲突双方都不生效：选任何一边都是在替用户做决定。"""
    resolution = resolve(
        [
            make(CapabilityKind.TOOL, "fs.read", Builtin()),
            make(CapabilityKind.TOOL, "fs.read", ACME),
            make(CapabilityKind.TOOL, "fs.write", Builtin()),
        ]
    )

    assert targets(resolution.report.active) == ["builtin:fs.write"]


def test_failure_names_all_claimants() -> None:
    """失败必须点名冲突各方，否则用户不知道该去卸载哪个插件。"""
    resolution = resolve(
        [
            make(CapabilityKind.TOOL, "fs.read", Builtin()),
            make(CapabilityKind.TOOL, "fs.read", ACME),
        ]
    )

    detail = resolution.report.failures[0].detail
    assert detail["claimants"] == ["builtin:fs.read", "plugin:acme:fs.read"]


def test_same_name_across_different_kinds_is_not_a_conflict() -> None:
    """唯一性是 kind 内的：同名的工具与命令互不相干。"""
    resolution = resolve(
        [
            make(CapabilityKind.TOOL, "search", Builtin()),
            make(CapabilityKind.COMMAND, "search", Builtin()),
        ]
    )

    assert resolution.report.ok
    assert len(resolution.report.active) == 2


# --------------------------------------------------------- 第 2 行：覆盖目标不存在


def test_override_target_missing_reports_dedicated_code() -> None:
    """`overrides` 目标不存在 —— `capability.override_target_missing`。"""
    resolution = resolve(
        [make(CapabilityKind.TOOL, "fs.read", ACME, overrides="builtin:fs.read")]
    )

    assert codes(resolution.report) == [ErrorCode.CAPABILITY_OVERRIDE_TARGET_MISSING.value]


def test_override_target_missing_does_not_degrade_to_new_registration() -> None:
    """§6.1 规则 2：不静默降级为新增注册——否则用户以为覆盖成功了。"""
    resolution = resolve(
        [make(CapabilityKind.TOOL, "fs.read", ACME, overrides="builtin:fs.read")]
    )

    assert resolution.report.active == ()
    assert resolution.active == ()


def test_override_target_must_match_kind() -> None:
    """覆盖目标串不带 kind，kind 取自声明方；跨 kind 的同名目标不算命中。"""
    resolution = resolve(
        [
            make(CapabilityKind.COMMAND, "fs.read", Builtin()),
            make(CapabilityKind.TOOL, "fs.read", ACME, overrides="builtin:fs.read"),
        ]
    )

    assert codes(resolution.report) == [ErrorCode.CAPABILITY_OVERRIDE_TARGET_MISSING.value]


def test_disabled_provider_counts_as_missing_target() -> None:
    """覆盖一个被禁用的目标等同于目标不存在，不回退成新增注册。"""
    resolution = resolve(
        [
            make(CapabilityKind.TOOL, "fs.read", Builtin()),
            make(CapabilityKind.TOOL, "fs.read", ACME, overrides="builtin:fs.read"),
        ],
        disabled={Builtin(): "配置禁用"},
    )

    assert codes(resolution.report) == [ErrorCode.CAPABILITY_OVERRIDE_TARGET_MISSING.value]
    assert targets([ref for ref, _ in resolution.report.disabled]) == ["builtin:fs.read"]


# ------------------------------------------------------------- 第 3 行：覆盖冲突


def test_two_plugins_overriding_same_target_conflict() -> None:
    """两个插件覆盖同一目标 —— `capability.override_conflict`。"""
    resolution = resolve(
        [
            make(CapabilityKind.TOOL, "fs.read", Builtin()),
            make(CapabilityKind.TOOL, "fs.read", ACME, overrides="builtin:fs.read"),
            make(CapabilityKind.TOOL, "fs.read", OTHER, overrides="builtin:fs.read"),
        ]
    )

    assert codes(resolution.report) == [ErrorCode.CAPABILITY_OVERRIDE_CONFLICT.value]
    detail = resolution.report.failures[0].detail
    assert detail["claimants"] == ["plugin:acme:fs.read", "plugin:other:fs.read"]


def test_override_conflict_leaves_target_active() -> None:
    """抢覆盖的都出局，被抢的目标继续生效——实例不会因此丢掉这个能力。"""
    resolution = resolve(
        [
            make(CapabilityKind.TOOL, "fs.read", Builtin()),
            make(CapabilityKind.TOOL, "fs.read", ACME, overrides="builtin:fs.read"),
            make(CapabilityKind.TOOL, "fs.read", OTHER, overrides="builtin:fs.read"),
        ]
    )

    assert targets(resolution.report.active) == ["builtin:fs.read"]
    assert resolution.report.shadowed == ()


# --------------------------------------------------------------- 第 4 行：SINGLETON


def test_singleton_with_two_implementations_fails() -> None:
    """SINGLETON kind 注册两个实现 —— 启动错误，即使两者名字不同。"""
    resolution = resolve(
        [
            make(CapabilityKind.SESSION_STORE, "jsonl", Builtin()),
            make(CapabilityKind.SESSION_STORE, "sqlite", ACME),
        ]
    )

    assert codes(resolution.report) == [ErrorCode.PLUGIN_REGISTRATION_CONFLICT.value]
    assert resolution.report.active == ()


def test_singleton_replacement_by_explicit_override_succeeds() -> None:
    """换名字替换 SINGLETON 是允许的，前提是显式声明覆盖。"""
    resolution = resolve(
        [
            make(CapabilityKind.SESSION_STORE, "jsonl", Builtin()),
            make(CapabilityKind.SESSION_STORE, "sqlite", ACME, overrides="builtin:jsonl"),
        ]
    )

    assert resolution.report.ok
    assert targets(resolution.report.active) == ["plugin:acme:sqlite"]
    assert resolution.report.shadowed == ((
        CapabilityRef(kind=CapabilityKind.SESSION_STORE, name="jsonl", provider=Builtin()),
        CapabilityRef(kind=CapabilityKind.SESSION_STORE, name="sqlite", provider=ACME),
    ),)


def test_cli_entry_is_singleton_too() -> None:
    """`CLI_ENTRY` 与 `SESSION_STORE` 同为 SINGLETON，走同一条判定。"""
    resolution = resolve(
        [
            make(CapabilityKind.CLI_ENTRY, "cli", Builtin()),
            make(CapabilityKind.CLI_ENTRY, "fancy", ACME),
        ]
    )

    assert codes(resolution.report) == [ErrorCode.PLUGIN_REGISTRATION_CONFLICT.value]


# ------------------------------------------------------- 第 5 行：MULTI 同名并存


@pytest.mark.parametrize("kind", [CapabilityKind.CONTEXT, CapabilityKind.HOOK])
def test_multi_kinds_coexist_sorted(kind: CapabilityKind) -> None:
    """CONTEXT / HOOK 同名并存 —— 全部生效，按 `(priority, provider)` 排序。"""
    resolution = resolve(
        [
            make(kind, "brief", ZULU, priority=50),
            make(kind, "brief", Builtin(), priority=100),
            make(kind, "brief", ACME, priority=100),
        ]
    )

    assert resolution.report.ok
    assert targets(resolution.report.active) == [
        "plugin:zulu:brief",
        "builtin:brief",
        "plugin:acme:brief",
    ]


@pytest.mark.parametrize("kind", [CapabilityKind.CONTEXT, CapabilityKind.HOOK])
def test_builtin_baseline_sorts_ahead_of_plugin_defaults(kind: CapabilityKind) -> None:
    """§6.1 规则 1 的可观测形态：都不声明 priority 时内建在前（基准 0 对 100）。

    这条同时锁住 §10.2 的裁剪顺序——「其余按 priority 逆序丢弃」意味着基准 0 的内建
    上下文在预算压力下最后被丢。
    """
    registry = CapabilityRegistry()
    with registry.batch(ACME) as batch:
        batch.add(kind, "brief", "acme")
    with registry.batch(Builtin()) as batch:
        batch.add(kind, "brief", "builtin")
    report = resolve_into(registry)

    assert targets(report.active) == ["builtin:brief", "plugin:acme:brief"]
    assert [item.payload for item in registry.lookup_all(kind, "brief")] == [
        "builtin",
        "acme",
    ]


def test_plugin_may_declare_priority_zero_but_loses_the_tie() -> None:
    """插件可以声明 0 与内建同级，但同 priority 按 provider 字典序，内建仍在前。"""
    resolution = resolve(
        [
            make(CapabilityKind.CONTEXT, "brief", ACME, priority=0),
            make(CapabilityKind.CONTEXT, "brief", Builtin(), priority=0),
        ]
    )

    assert targets(resolution.report.active) == ["builtin:brief", "plugin:acme:brief"]


def test_multi_kind_same_provider_multiple_names() -> None:
    """同一提供方注册多个 CONTEXT 是正常用法，不构成冲突。"""
    resolution = resolve(
        [
            make(CapabilityKind.CONTEXT, "b", Builtin()),
            make(CapabilityKind.CONTEXT, "a", Builtin()),
        ]
    )

    assert resolution.report.ok
    assert targets(resolution.report.active) == ["builtin:a", "builtin:b"]


def test_multi_kind_lookup_all_returns_every_implementation() -> None:
    """注册表的查找面也必须还原「全部生效」，否则排序结论在运行期丢失。"""
    registry = CapabilityRegistry()
    with registry.batch(Builtin()) as batch:
        batch.add(CapabilityKind.CONTEXT, "brief", "builtin", priority=100)
    with registry.batch(ACME) as batch:
        batch.add(CapabilityKind.CONTEXT, "brief", "acme", priority=10)
    resolve_into(registry)

    found = registry.lookup_all(CapabilityKind.CONTEXT, "brief")
    assert [item.payload for item in found] == ["acme", "builtin"]


# ----------------------------------------------------------- 第 6 行：覆盖成功


def test_successful_override_records_shadowed() -> None:
    """覆盖成功 —— 被覆盖项进入 `shadowed`，报告中可见（`NFR-502`）。"""
    resolution = resolve(
        [
            make(CapabilityKind.TOOL, "fs.read", Builtin()),
            make(CapabilityKind.TOOL, "fs.read", ACME, overrides="builtin:fs.read"),
        ]
    )

    assert resolution.report.ok
    assert targets(resolution.report.active) == ["plugin:acme:fs.read"]
    hidden, winner = resolution.report.shadowed[0]
    assert hidden.target == "builtin:fs.read"
    assert winner.target == "plugin:acme:fs.read"


def test_override_chain_plugin_over_plugin() -> None:
    """插件也可以覆盖插件，目标串是 `plugin:<id>:<name>`。"""
    resolution = resolve(
        [
            make(CapabilityKind.TOOL, "fs.read", ACME),
            make(CapabilityKind.TOOL, "fs.read", OTHER, overrides="plugin:acme:fs.read"),
        ]
    )

    assert resolution.report.ok
    assert targets(resolution.report.active) == ["plugin:other:fs.read"]


def test_active_payload_follows_the_override_winner() -> None:
    """生效集合带的是覆盖者的 payload——报告说谁赢，装配就得用谁。"""
    resolution = resolve(
        [
            make(CapabilityKind.TOOL, "fs.read", Builtin()),
            make(CapabilityKind.TOOL, "fs.read", ACME, overrides="builtin:fs.read"),
        ]
    )

    assert [item.payload for item in resolution.active] == ["plugin:acme:fs.read"]


# ------------------------------------------------------------------- 报告与排序


def test_report_serializes_all_four_sections() -> None:
    """`ResolutionReport` 可序列化为 JSON，`active/shadowed/disabled/failures` 齐全。"""
    resolution = resolve(
        [
            make(CapabilityKind.TOOL, "fs.read", Builtin()),
            make(CapabilityKind.TOOL, "fs.read", ACME, overrides="builtin:fs.read"),
            make(CapabilityKind.TOOL, "http.get", OTHER),
            make(CapabilityKind.TOOL, "gone", ZULU, overrides="builtin:gone"),
            make(CapabilityKind.CHANNEL, "tg", ZULU),
        ],
        disabled={OTHER: "用户在配置中禁用"},
    )
    payload = resolution.report.to_json()

    assert set(payload) == {"active", "shadowed", "disabled", "failures"}
    # 往返一次证明它真的是 JSON，而不是「看起来像 JSON 的字典」。
    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload

    # 生效集合按 `(priority, provider, name)` 排序，kind 不参与——同 priority 下
    # `plugin:acme` 字典序在 `plugin:zulu` 之前。
    assert payload["active"] == [
        {"kind": "tool", "name": "fs.read", "provider": "plugin:acme", "version": "0"},
        {"kind": "channel", "name": "tg", "provider": "plugin:zulu", "version": "0"},
    ]
    assert payload["shadowed"] == [
        {
            "capability": {
                "kind": "tool",
                "name": "fs.read",
                "provider": "builtin",
                "version": "0",
            },
            "overridden_by": {
                "kind": "tool",
                "name": "fs.read",
                "provider": "plugin:acme",
                "version": "0",
            },
        }
    ]
    assert payload["disabled"] == [
        {
            "capability": {
                "kind": "tool",
                "name": "http.get",
                "provider": "plugin:other",
                "version": "0",
            },
            "reason": "用户在配置中禁用",
        }
    ]
    assert [entry["code"] for entry in payload["failures"]] == [
        ErrorCode.CAPABILITY_OVERRIDE_TARGET_MISSING.value
    ]


def test_report_ok_and_raise_if_failed() -> None:
    """「启动错误」由调用方对报告调用 `raise_if_failed()` 兑现。"""
    clean = resolve([make(CapabilityKind.TOOL, "fs.read", Builtin())]).report
    clean.raise_if_failed()
    assert clean.ok

    broken = resolve(
        [make(CapabilityKind.TOOL, "fs.read", ACME, overrides="builtin:fs.read")]
    ).report
    assert not broken.ok
    with pytest.raises(NucleaError) as excinfo:
        broken.raise_if_failed()
    assert excinfo.value.code is ErrorCode.CAPABILITY_OVERRIDE_TARGET_MISSING


def test_all_conflicts_are_reported_at_once() -> None:
    """一次解析报出全部冲突：改一条配置才看到下一条冲突不是可接受的启动体验。"""
    resolution = resolve(
        [
            make(CapabilityKind.TOOL, "a", Builtin()),
            make(CapabilityKind.TOOL, "a", ACME),
            make(CapabilityKind.TOOL, "b", ACME, overrides="builtin:b"),
            make(CapabilityKind.SESSION_STORE, "s1", Builtin()),
            make(CapabilityKind.SESSION_STORE, "s2", ACME),
        ]
    )

    assert sorted(codes(resolution.report)) == sorted(
        [
            ErrorCode.CAPABILITY_OVERRIDE_TARGET_MISSING.value,
            ErrorCode.PLUGIN_REGISTRATION_CONFLICT.value,
            ErrorCode.PLUGIN_REGISTRATION_CONFLICT.value,
        ]
    )


def test_sort_is_stable_across_input_permutations() -> None:
    """排序稳定性：同 priority 按 provider 字典序，输入顺序不影响结论。"""
    registrations = [
        make(CapabilityKind.CONTEXT, "brief", ZULU),
        make(CapabilityKind.CONTEXT, "brief", Builtin()),
        make(CapabilityKind.CONTEXT, "brief", ACME),
        make(CapabilityKind.CONTEXT, "brief", OTHER),
    ]
    expected = targets(resolve(registrations).report.active)

    assert expected == [
        "builtin:brief",
        "plugin:acme:brief",
        "plugin:other:brief",
        "plugin:zulu:brief",
    ]
    for rotation in range(1, len(registrations)):
        rotated = registrations[rotation:] + registrations[:rotation]
        assert targets(resolve(rotated).report.active) == expected


def test_priority_beats_provider_order() -> None:
    """priority 是第一关键字，provider 字典序只在同 priority 时才起作用。"""
    resolution = resolve(
        [
            make(CapabilityKind.HOOK, "h", Builtin(), priority=200),
            make(CapabilityKind.HOOK, "h", ZULU, priority=1),
        ]
    )

    assert targets(resolution.report.active) == ["plugin:zulu:h", "builtin:h"]


def test_resolve_into_freezes_and_returns_report() -> None:
    """启动期的正常入口：解析 + 冻结 + 返回报告。"""
    registry = CapabilityRegistry()
    with registry.batch(Builtin()) as batch:
        batch.add(CapabilityKind.TOOL, "fs.read", "impl")

    report = resolve_into(registry)

    assert registry.frozen
    assert report.ok
    assert len(registry) == 1


def test_resolve_into_freezes_even_when_conflicted() -> None:
    """有冲突也冻结：诊断路径需要读到生效集合，那正是出问题时最该看的东西。"""
    registry = CapabilityRegistry()
    with registry.batch(Builtin()) as batch:
        batch.add(CapabilityKind.SESSION_STORE, "a", "impl")
    with registry.batch(ACME) as batch:
        batch.add(CapabilityKind.SESSION_STORE, "b", "impl")

    report = resolve_into(registry)

    assert registry.frozen
    assert not report.ok
    assert len(registry) == 0


def test_resolve_does_not_mutate_input() -> None:
    """`resolve()` 是纯函数：同一份输入连解两次结论一致。"""
    registrations = [
        make(CapabilityKind.TOOL, "fs.read", Builtin()),
        make(CapabilityKind.TOOL, "fs.read", ACME, overrides="builtin:fs.read"),
    ]
    first = resolve(registrations).report.to_json()
    second = resolve(registrations).report.to_json()

    assert first == second
    assert len(registrations) == 2


def test_empty_registry_resolves_to_empty_report() -> None:
    """空实例是合法的：没有任何能力不是错误，只是什么都做不了。"""
    resolution = resolve([])

    assert resolution.report.ok
    assert resolution.report.to_json() == {
        "active": [],
        "shadowed": [],
        "disabled": [],
        "failures": [],
    }
