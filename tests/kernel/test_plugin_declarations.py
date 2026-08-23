"""注册意图投影的校验测试（`D16`；`EDG-102`）。

`CapabilityDeclaration` 与 `LoadRequest` 是纯数据，但它们的校验有实际作用：声明表是
Host 判定「这个注册合法吗」的唯一依据，一条形状非法的声明会让 `overrides` 的解码推迟到
覆盖解析阶段才炸，那时已经看不出是哪份 manifest 写错了。

校验一律**借用契约层的既有实现**（`CapabilityRef` 校验名字、`parse_capability_target`
解码覆盖目标），因此「manifest 通过了但 kernel 拒绝」不可能发生——两侧是同一份代码。
"""

from __future__ import annotations

import pytest

from nucleamind.contracts import (
    Builtin,
    CapabilityKind,
    ErrorCode,
    NucleaError,
)
from nucleamind.kernel.plugins import CapabilityDeclaration, LoadRequest


def test_a_declaration_carries_its_slot() -> None:
    declaration = CapabilityDeclaration(kind=CapabilityKind.TOOL, name="fs.read")
    assert declaration.slot == (CapabilityKind.TOOL, "fs.read")
    assert declaration.priority is None
    assert declaration.overrides is None


@pytest.mark.parametrize("name", ["", " ", "a" * 300, "bad\x00name"], ids=repr)
def test_a_malformed_capability_name_is_rejected(name: str) -> None:
    """名字的形状借 `CapabilityRef` 校验——与 manifest 侧同一份实现。"""
    with pytest.raises(NucleaError):
        CapabilityDeclaration(kind=CapabilityKind.TOOL, name=name)


def test_a_malformed_override_target_is_rejected() -> None:
    """覆盖目标只用 `parse_capability_target()` 解码，不在这里另写正则。"""
    with pytest.raises(NucleaError) as excinfo:
        CapabilityDeclaration(kind=CapabilityKind.TOOL, name="fs.read", overrides="垃圾串")
    assert excinfo.value.code is ErrorCode.INPUT_MALFORMED


def test_a_well_formed_override_target_survives_as_a_raw_string() -> None:
    """`D06` 的约定：跨层只传原始串，两侧共用一份解码实现。"""
    declaration = CapabilityDeclaration(
        kind=CapabilityKind.TOOL, name="fs.read", overrides="builtin:fs.read"
    )
    assert declaration.overrides == "builtin:fs.read"


def test_a_negative_priority_is_rejected() -> None:
    """与 `Registration.__post_init__` 同一条规则：优先级不得为负。

    在这里也拦一次，是为了让错误指回**声明**而不是指回某次注册——用户要改的是 manifest。
    """
    with pytest.raises(NucleaError) as excinfo:
        CapabilityDeclaration(kind=CapabilityKind.TOOL, name="fs.read", priority=-1)
    assert excinfo.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


def test_zero_priority_is_allowed() -> None:
    """0 是内建基准值，必须可以显式声明。"""
    assert CapabilityDeclaration(kind=CapabilityKind.CONTEXT, name="ctx", priority=0).priority == 0


def test_a_load_request_rejects_duplicate_declarations() -> None:
    """同一提供方重复声明同一能力：manifest 侧已拦，这里是第二道（内建清单不走 manifest）。"""
    duplicate = CapabilityDeclaration(kind=CapabilityKind.TOOL, name="fs.read")
    with pytest.raises(NucleaError) as excinfo:
        LoadRequest(
            plugin_id="probe",
            provider=Builtin(),
            setup="pkg:setup",
            declarations=(duplicate, duplicate),
        )
    assert excinfo.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


def test_the_same_name_under_two_kinds_is_not_a_duplicate() -> None:
    """唯一性键是 `(kind, name)`：一个叫 `status` 的命令与一个叫 `status` 的工具不冲突。"""
    request = LoadRequest(
        plugin_id="probe",
        provider=Builtin(),
        setup="pkg:setup",
        declarations=(
            CapabilityDeclaration(kind=CapabilityKind.TOOL, name="status"),
            CapabilityDeclaration(kind=CapabilityKind.COMMAND, name="status"),
        ),
    )
    assert len(request.declarations) == 2


# --------------------------------------------------------------- 命名空间声明（`D38-A`）


def test_a_namespace_declaration_covers_names_under_its_prefix() -> None:
    decl = CapabilityDeclaration(kind=CapabilityKind.TOOL, name="mcp", namespace=True)
    assert decl.covers(CapabilityKind.TOOL, "mcp.fs.read") is True


@pytest.mark.parametrize("name", ["mcp", "mcpx.read", "other.read"])
def test_a_namespace_declaration_covers_nothing_else(name: str) -> None:
    """前缀比较必须落在分隔符边界上，而前缀本身不是它放行的名字之一。"""
    decl = CapabilityDeclaration(kind=CapabilityKind.TOOL, name="mcp", namespace=True)
    assert decl.covers(CapabilityKind.TOOL, name) is False


def test_a_namespace_declaration_does_not_cross_kinds() -> None:
    decl = CapabilityDeclaration(kind=CapabilityKind.TOOL, name="mcp", namespace=True)
    assert decl.covers(CapabilityKind.COMMAND, "mcp.list") is False


def test_a_plain_declaration_covers_nothing() -> None:
    """精确匹配走 `slot`，`covers()` 恒为假——两条路不该有重叠。"""
    decl = CapabilityDeclaration(kind=CapabilityKind.TOOL, name="mcp")
    assert decl.covers(CapabilityKind.TOOL, "mcp.a") is False
    assert decl.covers(CapabilityKind.TOOL, "mcp") is False


def test_a_namespace_declaration_keeps_its_slot() -> None:
    """`slot` 仍是 `(kind, 前缀)`：`LoadRequest` 的重复检测据此工作。"""
    decl = CapabilityDeclaration(kind=CapabilityKind.TOOL, name="mcp", namespace=True)
    assert decl.slot == (CapabilityKind.TOOL, "mcp")
