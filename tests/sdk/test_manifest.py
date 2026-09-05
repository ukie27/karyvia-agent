"""manifest 的校验矩阵（技术方案 §7.2、`SDK-005`、`CMP-001`、`PLG-001`）。

两条被反复验证的性质：**每一条校验失败都给出字段路径**，以及**失败一律是
`NucleaError`**——缺兼容字段直接判定失败并指出位置，不做兜底猜测。
"""

from __future__ import annotations

from typing import Final

import pytest
from pydantic import ValidationError

from nucleamind.contracts import (
    Builtin,
    CapabilityArity,
    CapabilityKind,
    ErrorCode,
    NucleaError,
)
from nucleamind.sdk import CapabilityDecl, PluginManifest, parse_manifest
from nucleamind.sdk.version import SDK_VERSION

#: 一份最小可用 manifest。各用例在它上面打补丁，只改一处，失败原因因此无歧义。
VALID: Final[dict[str, object]] = {
    "id": "memory-sqlite",
    "version": "0.1.0",
    "sdk_range": ">=5.0,<6.0",
    "setup": "nucleamind_plugin_memory_sqlite.plugin:setup",
    "capabilities": [{"kind": "memory", "name": "sqlite"}],
}


def parse(**patch: object) -> PluginManifest:
    return parse_manifest({**VALID, **patch}, origin="test")


# ------------------------------------------------------------------------------ 正例


def test_minimal_manifest_parses() -> None:
    manifest = parse()
    assert manifest.id == "memory-sqlite"
    assert manifest.capabilities[0].kind is CapabilityKind.MEMORY
    # 未声明的字段取默认值，而不是 None——加载器不必到处判空。
    assert manifest.dependencies == ()
    assert manifest.state_version == 1


def test_removed_critical_field_is_rejected() -> None:
    """SDK 4 不再接受由插件决定宿主是否中断的旧字段。"""
    with pytest.raises(NucleaError) as excinfo:
        parse(critical=True)
    assert "critical" in str(excinfo.value.detail)


def test_manifest_is_frozen() -> None:
    """manifest 是数据：解析之后没有人可以改它，包括加载器自己。"""
    manifest = parse()
    with pytest.raises(ValidationError):
        manifest.id = "other"  # type: ignore[misc]


def test_override_target_decodes_via_the_shared_parser() -> None:
    """覆盖目标只有一套编解码（`CapabilityRef.target` / `parse_capability_target`）。"""
    manifest = parse(
        capabilities=[{"kind": "tool", "name": "fs.read", "overrides": "builtin:fs.read"}]
    )
    assert manifest.capabilities[0].override_target == (Builtin(), "fs.read")


def test_no_override_means_none() -> None:
    assert parse().capabilities[0].override_target is None


def test_sdk_compatibility_is_evaluated_against_the_current_version() -> None:
    assert parse().sdk_compatible is True
    assert parse(sdk_range=">=99.0").sdk_compatible is False, f"当前 SDK 是 {SDK_VERSION}"


def test_platforms_empty_means_every_platform() -> None:
    assert parse().matches_platform("win32") is True
    assert parse().matches_platform("linux") is True


def test_platforms_are_matched_exactly() -> None:
    manifest = parse(platforms=["linux"])
    assert manifest.matches_platform("linux") is True
    assert manifest.matches_platform("win32") is False


def test_capability_decl_slot_is_kind_plus_name() -> None:
    decl = CapabilityDecl(kind=CapabilityKind.TOOL, name="fs.read")
    assert decl.slot == (CapabilityKind.TOOL, "fs.read")
    assert decl.priority == 100


# ------------------------------------------------------------------------------ 反例

#: (补丁, 期望出现在诊断里的字段路径)。每条一个失败点。
INVALID_CASES: Final[list[tuple[dict[str, object], str]]] = [
    ({"id": "Memory_SQLite"}, "id"),
    ({"id": "-leading"}, "id"),
    ({"version": "not-a-version!"}, "version"),
    ({"sdk_range": ">>=1"}, "sdk_range"),
    ({"setup": "no-colon"}, "setup"),
    ({"setup": "pkg.module:not an identifier"}, "setup"),
    ({"capabilities": []}, "capabilities"),
    ({"capabilities": [{"kind": "tool", "name": "NOT LOWER"}]}, "capabilities.name"),
    ({"capabilities": [{"kind": "tool", "name": "fs.read", "overrides": "??"}]}, "overrides"),
    ({"capabilities": [{"kind": "tool", "name": "fs.read", "priority": -1}]}, "priority"),
    (
        {"capabilities": [{"kind": "tool", "name": "a.b"}, {"kind": "tool", "name": "a.b"}]},
        "capabilities",
    ),
    ({"dependencies": ["memory-sqlite"]}, "dependencies"),
    ({"dependencies": ["a", "a"]}, "value"),
    ({"state_version": 0}, "state_version"),
]


@pytest.mark.parametrize(
    ("patch", "field"), INVALID_CASES, ids=[str(list(p)[0]) + ":" + f for p, f in INVALID_CASES]
)
def test_invalid_manifest_reports_the_offending_field(patch: dict[str, object], field: str) -> None:
    with pytest.raises(NucleaError) as excinfo:
        parse(**patch)
    error = excinfo.value
    assert error.code is ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED
    assert field in str(error.detail), f"诊断里没有字段路径 {field}：{dict(error.detail)}"


#: 结构错误由 pydantic 发现，`parse_manifest()` 把它们连同**字段路径**一起转成
#: `NucleaError`——`errors` 是 `[{"field": ..., "message": ...}]`。
STRUCTURAL_CASES: Final[list[tuple[dict[str, object], str]]] = [
    ({"unknown_field": 1}, "unknown_field"),
    ({"permissions": []}, "permissions"),
    ({"runtime_requires": ["node>=20"]}, "runtime_requires"),
    ({"capabilities": [{"kind": "not-a-kind", "name": "a.b"}]}, "capabilities.0.kind"),
    ({"capabilities": [{"name": "a.b"}]}, "capabilities.0.kind"),
    ({"state_version": "one"}, "state_version"),
]


@pytest.mark.parametrize(("patch", "path"), STRUCTURAL_CASES, ids=[p for _, p in STRUCTURAL_CASES])
def test_structural_errors_carry_a_field_path(patch: dict[str, object], path: str) -> None:
    with pytest.raises(NucleaError) as excinfo:
        parse(**patch)
    detail = dict(excinfo.value.detail)
    assert detail["origin"] == "test"
    errors = detail["errors"]
    assert isinstance(errors, (list, tuple))
    fields = [dict(entry)["field"] for entry in errors]  # type: ignore[arg-type]
    assert path in fields, f"字段路径不含 {path}：{fields}"


def test_missing_required_field_is_rejected_without_guessing() -> None:
    """`CMP-001`：缺少 `sdk_range` 这类兼容字段直接判定失败，不做兜底。"""
    data = {key: value for key, value in VALID.items() if key != "sdk_range"}
    with pytest.raises(NucleaError) as excinfo:
        parse_manifest(data, origin="test")
    assert "sdk_range" in str(excinfo.value.detail)


def test_direct_construction_still_validates_semantics() -> None:
    """插件作者在自己的模块里直接构造时，语义校验同样生效（不只是 `parse_manifest`）。"""
    with pytest.raises(NucleaError):
        PluginManifest(
            id="Bad_Id",
            version="1.0",
            sdk_range=">=0.1",
            setup="a.b:c",
            capabilities=(CapabilityDecl(kind=CapabilityKind.TOOL, name="a.b"),),
        )


# --------------------------------------------------------------- 命名空间声明（`D38-A`）
#
# 它是为「能力名要连上外部服务才知道」开的（MCP 的远端工具名只有 `list_tools` 之后才可知，
# 而 manifest 是静态的）。两条限制在这里判死，kernel 侧只按标志位分派（`R2`）。


@pytest.mark.parametrize(
    "kind", sorted(k for k in CapabilityKind if k.arity is CapabilityArity.MULTI_UNIQUE)
)
def test_every_multi_unique_kind_may_declare_a_namespace(kind: CapabilityKind) -> None:
    """判据取自 `CAPABILITY_ARITY` 而不是一张手写名单——用例也照着那张表遍历，
    因此往表里加 kind 时这条会自动覆盖到它。"""
    assert CapabilityDecl(kind=kind, name="ns", namespace=True).namespace is True


@pytest.mark.parametrize(
    "kind", sorted(k for k in CapabilityKind if k.arity is not CapabilityArity.MULTI_UNIQUE)
)
def test_no_other_kind_may_declare_a_namespace(kind: CapabilityKind) -> None:
    """SINGLETON 的槽位按定义只有一个，给它开前缀等于让「唯一」失去判定对象；
    MULTI 类本来就允许同一提供方注册多条同名能力，不需要这个机制。"""
    with pytest.raises(NucleaError) as caught:
        CapabilityDecl(kind=kind, name="ns", namespace=True)
    assert caught.value.code is ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED


def test_a_namespace_may_not_also_declare_overrides() -> None:
    """一条声明能注册出任意多个名字，哪一个才是覆盖者无从判定——静默挑一个正是
    `EDG-102`「覆盖永不由加载顺序决定」要堵的路。"""
    with pytest.raises(NucleaError) as caught:
        CapabilityDecl(
            kind=CapabilityKind.TOOL,
            name="mcp",
            namespace=True,
            overrides="builtin:fs.read",
        )
    assert caught.value.code is ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED


def test_overrides_without_a_namespace_is_still_fine() -> None:
    decl = CapabilityDecl(
        kind=CapabilityKind.TOOL, name="fs.read", overrides="builtin:fs.read"
    )
    assert decl.namespace is False


def test_the_default_is_not_a_namespace() -> None:
    """加一个默认为真的字段会让每一份既有 manifest 的语义悄悄改变。"""
    assert CapabilityDecl(kind=CapabilityKind.TOOL, name="a.b").namespace is False


def test_a_namespace_prefix_still_obeys_the_capability_name_shape() -> None:
    """前缀也是名字：形状仍由 `CapabilityRef` 校验，不因为它是前缀就放宽。"""
    with pytest.raises(NucleaError):
        CapabilityDecl(kind=CapabilityKind.TOOL, name="Bad Name", namespace=True)


# ------------------------------------------------------- config_schema（`D41`）
#
# 字段的静态类型只精确到最外两层（`ManifestJsonValue`：容器分支的元素是 `object`），
# 因为契约的 `JsonValue` 进不了 pydantic 模型、pydantic 自己的 `JsonValue` 又用不变的
# `list`。递归因此挪进了 `_check_config_schema`——**下面这组用例就是那次搬迁的收据**：
# 少了它们，「深处也必须是 JSON」这条只剩一句注释。


def test_config_schema_accepts_a_normal_document() -> None:
    schema = {
        "type": "object",
        # `sorted()` 是函数调用而不是字面量，双向推断够不着它——这正是 pydantic 的
        # `JsonValue`（不变的 `list`）在七个官方插件上都过不去的那个形状。
        "required": sorted({"b", "a"}),
        "properties": {"a": {"type": "string", "enum": ["x", "y"]}},
        "additionalProperties": False,
    }
    manifest = parse(config_schema=schema)
    assert manifest.config_schema is not None
    assert manifest.config_schema["required"] == ["a", "b"]


def test_config_schema_defaults_to_none() -> None:
    assert parse().config_schema is None
    assert parse().json_schema is None


def test_json_schema_is_the_narrowed_view() -> None:
    """`json_schema` 是交给 `contracts.JsonSchema` 调用方的唯一出口（`plugin_plan` 用它）。"""
    manifest = parse(config_schema={"type": "object"})
    assert manifest.json_schema == manifest.config_schema


@pytest.mark.parametrize(
    ("document", "pointer"),
    [
        ({"a": object()}, "/a"),
        ({"a": {"b": [1, object()]}}, "/a/b/1"),
        ({"a": {1: "x"}}, "/a"),
        ({"a/b": object()}, "/a~1b"),
        ({"a~b": object()}, "/a~0b"),
    ],
)
def test_config_schema_rejects_non_json_values(document: dict[str, object], pointer: str) -> None:
    """报错要指到位置。JSON Pointer 的两个转义（`~0` / `~1`）一并钉住。"""
    with pytest.raises(NucleaError) as caught:
        parse(config_schema=document)
    assert caught.value.code is ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED
    assert caught.value.detail["field"] == "config_schema"
    assert caught.value.detail["at"] == pointer


def test_config_schema_rejects_a_self_referencing_document() -> None:
    """自引用在类型上完全合法，没有深度上界校验器自己会栈溢出。"""
    document: dict[str, object] = {}
    document["self"] = document
    with pytest.raises(NucleaError) as caught:
        parse(config_schema=document)
    assert caught.value.code is ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED


def test_config_schema_keeps_bool_and_int_apart() -> None:
    """`isinstance(True, int)` 为真，所以 `bool` 必须先判——两者都合法，这里验的是不误伤。"""
    manifest = parse(config_schema={"flag": True, "count": 1, "ratio": 1.5, "none": None})
    assert manifest.config_schema == {"flag": True, "count": 1, "ratio": 1.5, "none": None}
