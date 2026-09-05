"""登记、事务性批次与冻结的测试（`D06` 验收表第 7、8 行 + 注册表机制）。

`EDG-103`「批次中途抛异常，registry 无残留」与「冻结后写入抛 `KERNEL_INTERNAL`」是本文件
的两条主线；其余用例覆盖批次状态机、查找契约与索引正确性。

冲突语义（同名重复、覆盖、arity）全部在 `test_resolution.py`——注册表本身不判冲突，
这个分工正是 `EDG-102`「覆盖永不由加载顺序决定」的落地方式。
"""

from __future__ import annotations

import pytest

from karyvia.contracts import (
    Builtin,
    CapabilityKind,
    CapabilityRef,
    ErrorCategory,
    ErrorCode,
    KaryviaError,
    Plugin,
    PluginId,
)
from karyvia.kernel.registry import (
    BUILTIN_BASE_PRIORITY,
    PLUGIN_BASE_PRIORITY,
    BatchState,
    CapabilityRegistry,
    Registration,
    base_priority_for,
    resolve_into,
)

ACME = Plugin(PluginId("acme"))
TOOL_REF = CapabilityRef(kind=CapabilityKind.TOOL, name="fs.read", provider=Builtin())


def frozen_registry(*, tools: tuple[str, ...] = ("fs.read",)) -> CapabilityRegistry:
    """建一个已解析冻结的注册表，供查找类用例使用。"""
    registry = CapabilityRegistry()
    with registry.batch(Builtin()) as batch:
        for name in tools:
            batch.add(CapabilityKind.TOOL, name, f"impl:{name}")
    resolve_into(registry)
    return registry


# --------------------------------------------------------------------------- 批次


def test_staged_registrations_are_invisible_until_commit() -> None:
    """暂存区对 registry 不可见——这是「注册先入暂存区」的可观测形态。"""
    registry = CapabilityRegistry()
    batch = registry.batch(Builtin())
    batch.add(CapabilityKind.TOOL, "fs.read", "impl")

    assert batch.staged != ()
    assert registry.registrations == ()

    batch.commit()
    assert len(registry.registrations) == 1


def test_rollback_leaves_no_residue() -> None:
    """`D06` 验收表第 7 行：回滚后 registry 状态与批次开始前一致。"""
    registry = CapabilityRegistry()
    with registry.batch(Builtin()) as first:
        first.add(CapabilityKind.TOOL, "fs.read", "impl")
    before = registry.registrations

    batch = registry.batch(ACME)
    batch.add(CapabilityKind.TOOL, "http.get", "impl")
    batch.rollback()

    assert registry.registrations == before
    assert batch.state is BatchState.ROLLED_BACK
    assert batch.staged == ()


def test_exception_inside_batch_rolls_back_and_propagates() -> None:
    """`EDG-103`：`setup(api)` 中途抛异常时不得留下半注册状态，且异常不被吞掉。"""
    registry = CapabilityRegistry()
    sentinel = RuntimeError("setup 失败")

    with pytest.raises(RuntimeError) as excinfo:
        with registry.batch(ACME) as batch:
            batch.add(CapabilityKind.TOOL, "fs.read", "impl")
            raise sentinel

    assert excinfo.value is sentinel
    assert registry.registrations == ()
    assert batch.state is BatchState.ROLLED_BACK


def test_clean_exit_commits() -> None:
    """正常退出即提交，因此调用方不需要在每条正常路径上记得写 `commit()`。"""
    registry = CapabilityRegistry()
    with registry.batch(ACME) as batch:
        batch.add(CapabilityKind.TOOL, "fs.read", "impl")

    assert batch.state is BatchState.COMMITTED
    assert len(registry.registrations) == 1


def test_empty_batch_commits_cleanly() -> None:
    """不注册任何能力的批次是合法的：manifest 校验才是「插件必须有能力」的强制点。"""
    registry = CapabilityRegistry()
    with registry.batch(ACME) as batch:
        pass

    assert batch.state is BatchState.COMMITTED
    assert registry.registrations == ()


def test_add_after_commit_is_invariant_violation() -> None:
    """`KaryviaAPI` 的异常约定：批次已提交后再注册抛 `KERNEL_INVARIANT_VIOLATED`。"""
    registry = CapabilityRegistry()
    batch = registry.batch(ACME)
    batch.commit()

    with pytest.raises(KaryviaError) as excinfo:
        batch.add(CapabilityKind.TOOL, "fs.read", "impl")

    assert excinfo.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


def test_committed_batch_cannot_roll_back() -> None:
    """提交之后 registry 可能已被解析冻结，「撤回」没有可定义的语义。"""
    registry = CapabilityRegistry()
    batch = registry.batch(ACME)
    batch.commit()

    with pytest.raises(KaryviaError) as excinfo:
        batch.rollback()

    assert excinfo.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


def test_rollback_is_idempotent() -> None:
    """清理路径不该自己制造第二个错误。"""
    registry = CapabilityRegistry()
    batch = registry.batch(ACME)
    batch.rollback()
    batch.rollback()

    assert batch.state is BatchState.ROLLED_BACK


def test_duplicate_slot_within_one_batch_is_rejected() -> None:
    """一个提供方给同一槽位交两份实现是它自己的错误，不必等到解析阶段。"""
    registry = CapabilityRegistry()
    batch = registry.batch(ACME)
    batch.add(CapabilityKind.TOOL, "fs.read", "first")

    with pytest.raises(KaryviaError) as excinfo:
        batch.add(CapabilityKind.TOOL, "fs.read", "second")

    assert excinfo.value.code is ErrorCode.PLUGIN_REGISTRATION_CONFLICT


def test_batch_fills_provider_from_itself() -> None:
    """`add()` 没有 provider 参数：插件不能以别人的名义注册能力（`PLG-006`）。"""
    registry = CapabilityRegistry()
    with registry.batch(ACME) as batch:
        registration = batch.add(CapabilityKind.TOOL, "fs.read", "impl")

    assert registration.ref.provider == ACME
    assert registration.ref.target == "plugin:acme:fs.read"


# --------------------------------------------------------------------------- 冻结


def test_write_after_freeze_raises_kernel_internal() -> None:
    """`D06` 验收表第 8 行：冻结后写入抛 `KERNEL_INTERNAL` 类错误。"""
    registry = frozen_registry()

    with pytest.raises(KaryviaError) as excinfo:
        registry.batch(ACME)

    assert excinfo.value.category is ErrorCategory.KERNEL_INTERNAL
    assert excinfo.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


def test_absorb_after_freeze_raises() -> None:
    """绕过批次直接并入也拦得住——冻结是注册表自己的不变量，不依赖调用路径。"""
    registry = frozen_registry()
    registration = Registration(ref=TOOL_REF, payload="impl")

    with pytest.raises(KaryviaError) as excinfo:
        registry.absorb([registration])

    assert excinfo.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


def test_double_freeze_raises() -> None:
    registry = frozen_registry()

    with pytest.raises(KaryviaError) as excinfo:
        resolve_into(registry)

    assert excinfo.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


def test_lookup_before_freeze_raises() -> None:
    """未定案的注册表不可查：让它可查就是在鼓励调用方缓存随后被覆盖掉的实现。"""
    registry = CapabilityRegistry()
    with registry.batch(Builtin()) as batch:
        batch.add(CapabilityKind.TOOL, "fs.read", "impl")

    with pytest.raises(KaryviaError) as excinfo:
        registry.lookup(CapabilityKind.TOOL, "fs.read")

    assert excinfo.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


# --------------------------------------------------------------------------- 查找


def test_lookup_returns_payload() -> None:
    registry = frozen_registry()
    found = registry.lookup(CapabilityKind.TOOL, "fs.read")

    assert found is not None
    assert found.payload == "impl:fs.read"


def test_lookup_missing_returns_none() -> None:
    """缺失返回 `None` 而不是抛异常：「没有这个工具」是调用方要处理的正常分支。"""
    registry = frozen_registry()

    assert registry.lookup(CapabilityKind.TOOL, "nope") is None
    assert registry.lookup_all(CapabilityKind.TOOL, "nope") == ()


def test_lookup_rejects_multi_kinds() -> None:
    """MULTI 类能力用只取第一个的接口访问必然静默丢实现，因此直接拒绝。"""
    registry = frozen_registry()

    with pytest.raises(KaryviaError) as excinfo:
        registry.lookup(CapabilityKind.CONTEXT, "anything")

    assert excinfo.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


def test_of_kind_and_iteration() -> None:
    registry = frozen_registry(tools=("fs.read", "fs.write"))

    assert len(registry) == 2
    assert [item.ref.name for item in registry] == ["fs.read", "fs.write"]
    assert len(registry.of_kind(CapabilityKind.TOOL)) == 2
    assert registry.of_kind(CapabilityKind.CHANNEL) == ()


# --------------------------------------------------------------------------- 登记项


def test_negative_priority_is_rejected() -> None:
    with pytest.raises(KaryviaError) as excinfo:
        Registration(ref=TOOL_REF, payload="impl", priority=-1)

    assert excinfo.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


def test_self_override_is_rejected() -> None:
    """覆盖自身在解析阶段会变成「目标存在但也是自己」的死结，因此在构造时就拦掉。"""
    with pytest.raises(KaryviaError) as excinfo:
        Registration(ref=TOOL_REF, payload="impl", overrides=TOOL_REF.target)

    assert excinfo.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


def test_base_priority_follows_provider() -> None:
    """§6.1 规则 1：内建基准 0、插件基准 100，由 registry 而不是调用方决定。"""
    registry = CapabilityRegistry()
    with registry.batch(Builtin()) as batch:
        builtin = batch.add(CapabilityKind.CONTEXT, "brief", "impl")
    with registry.batch(ACME) as batch:
        plugin = batch.add(CapabilityKind.CONTEXT, "brief", "impl")

    assert builtin.priority == BUILTIN_BASE_PRIORITY == 0
    assert plugin.priority == PLUGIN_BASE_PRIORITY == 100
    assert base_priority_for(Builtin()) == 0
    assert base_priority_for(ACME) == 100


def test_explicit_priority_overrides_baseline() -> None:
    """显式传值一律优先，包括插件传 0——基准值是默认值，不是下限。"""
    registry = CapabilityRegistry()
    with registry.batch(Builtin()) as batch:
        builtin = batch.add(CapabilityKind.CONTEXT, "brief", "impl", priority=500)
    with registry.batch(ACME) as batch:
        plugin = batch.add(CapabilityKind.CONTEXT, "brief", "impl", priority=0)

    assert builtin.priority == 500
    assert plugin.priority == 0


def test_explicit_commit_inside_with_block_is_not_double_committed() -> None:
    """显式用法与 `with` 用法可以混用——`__exit__` 对已终结的批次不再动作。"""
    registry = CapabilityRegistry()
    with registry.batch(ACME) as batch:
        batch.add(CapabilityKind.TOOL, "fs.read", "impl")
        batch.commit()

    assert batch.state is BatchState.COMMITTED
    assert len(registry.registrations) == 1


def test_batch_exposes_its_provider() -> None:
    registry = CapabilityRegistry()
    batch = registry.batch(ACME)

    assert batch.provider == ACME
