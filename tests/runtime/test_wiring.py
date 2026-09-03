"""组装根的测试（`D16`；`R5`、`SDK-007`、`EDG-101`、§6.1 规则 1）。

三条主线：

- **内建与外部插件走同一个函数**。`wire_capabilities()` 只在「谁产出 `LoadRequest`」上
  区分两者，注册路径完全一致（`SDK-007`）。
- **`priority` 的默认值陷阱**。`CapabilityDecl.priority` 的默认值是 100（技术方案 §7.2），
  而 §6.1 规则 1 定的内建基准是 0——两条方案彼此打架。结论是「作者没写就当没写」，
  靠 pydantic 的 `model_fields_set` 判定。这条如果错了，内建能力会全部落在 100，
  §10.2 的「内建最后被裁」随之失效，而且不会有任何报错。
- **空清单可装配**（`EDG-101`）：`wire_capabilities(manifests=())` 必须跑完并冻结。
  `D17` 起 `BUILTIN_MANIFESTS` 不再是空元组，因此这条显式传空清单——要测的性质是「零内建
  可装配」，不是「默认清单恰好是空的」。

另有一条 AST 断言盯着「Host 一致性的证明还在」——证明本身由 basedpyright 完成，
但 `exclude = ["**/tests"]` 意味着测试验不了它，能验的只有「那句标注没被人删掉或改成
`cast`」。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from nucleamind.builtins.registry import BUILTIN_MANIFESTS
from nucleamind.contracts import (
    Builtin,
    CapabilityKind,
    Plugin,
    PluginId,
    ToolSpec,
)
from nucleamind.kernel.plugins import SetupFn, model_providers_from
from nucleamind.kernel.registry import BUILTIN_BASE_PRIORITY, PLUGIN_BASE_PRIORITY
from nucleamind.runtime import wiring
from nucleamind.runtime.wiring import to_declaration, to_load_request, wire_capabilities
from nucleamind.sdk import CapabilityDecl, PluginContext, PluginManifest, parse_manifest
from nucleamind.sdk.testing import EchoTool, FakeModelProvider, FakePluginContext

# ------------------------------------------------------------------------------------ 夹具


def manifest(plugin_id: str = "probe", *, priority: int | None = None) -> PluginManifest:
    """一份最小 manifest。`priority` 为 `None` 时**根本不写这个字段**——那正是要测的。"""
    capability: dict[str, object] = {"kind": "model", "name": "probe-model"}
    if priority is not None:
        capability["priority"] = priority
    return parse_manifest(
        {
            "id": plugin_id,
            "version": "1.0.0",
            "sdk_range": ">=0.1.0",
            "setup": "probe.module:setup",
            "capabilities": [capability],
        },
        origin="test",
    )


def context_for(source: PluginManifest) -> PluginContext:
    """`D23` 起按 **manifest** 索引：全部内建共用一个 `Builtin()`，按提供方索引会让
    九份内建拿到同一个配置块。"""
    return FakePluginContext(source.id)


def setup_model(api: object) -> None:
    assert hasattr(api, "register_model_provider")
    api.register_model_provider("probe-model", FakeModelProvider())  # type: ignore[attr-defined]


def resolver(setup: SetupFn = setup_model) -> object:
    def resolve(target: str) -> SetupFn:
        del target
        return setup

    return resolve


# ------------------------------------------------------------------------------ 空清单可装配


async def test_every_builtin_manifest_leaves_priority_unset() -> None:
    """`D17` 起 `BUILTIN_MANIFESTS` 不再为空，这条随之变成对每一项内建的棘轮。

    内建的 priority 基准是 0，而 `CapabilityDecl.priority` 的默认值是 100：在 manifest 里
    写了它（哪怕写的正好是 100）就会被原样采纳，§10.2 的「内建最后被裁」随之静默失效。
    """
    assert BUILTIN_MANIFESTS, "内建清单为空——`D17` 之后这说明有人把它清掉了"
    for builtin in BUILTIN_MANIFESTS:
        for declaration in builtin.capabilities:
            assert "priority" not in declaration.model_fields_set, builtin.id


async def test_wiring_with_no_manifests_produces_an_empty_frozen_registry() -> None:
    """零内建可装配（`EDG-101`）：显式传空清单，而不是指望默认清单恰好是空的。"""
    result = await wire_capabilities(manifests=(), context_for=context_for)
    assert result.outcomes == ()
    assert result.report.ok
    assert result.registry.frozen
    assert model_providers_from(result.registry) == ()


# -------------------------------------------------------------------- 内建与插件共用一条路径


async def test_builtin_and_plugin_go_through_the_same_function() -> None:
    """`SDK-007`：换掉 `provider_for` 就是外部插件，注册路径一个字都不改。"""
    manifests = [manifest()]
    as_builtin = await wire_capabilities(
        manifests=manifests, context_for=context_for, resolve_setup=resolver()
    )
    as_plugin = await wire_capabilities(
        manifests=manifests,
        context_for=context_for,
        provider_for=lambda m: Plugin(PluginId(m.id)),
        resolve_setup=resolver(),
    )
    assert model_providers_from(as_builtin.registry)[0].owner == Builtin()
    assert model_providers_from(as_plugin.registry)[0].owner == Plugin(PluginId("probe"))


# ------------------------------------------------------------------------------ priority 判定


def test_an_unstated_priority_is_not_forwarded() -> None:
    """作者没写 `priority` 时必须留 `None`，让 `base_priority_for()` 决定。"""
    decl = manifest().capabilities[0]
    assert decl.priority == PLUGIN_BASE_PRIORITY  # pydantic 的默认值确实是 100
    assert to_declaration(decl).priority is None


def test_an_explicitly_written_priority_is_forwarded() -> None:
    """哪怕写的就是默认值 100，也要原样传下去——「写了」和「没写」是两件事。"""
    decl = manifest(priority=PLUGIN_BASE_PRIORITY).capabilities[0]
    assert to_declaration(decl).priority == PLUGIN_BASE_PRIORITY
    assert to_declaration(manifest(priority=5).capabilities[0]).priority == 5


async def test_a_builtin_capability_lands_on_the_builtin_baseline() -> None:
    """这条是上面那个判定的后果：内建必须是 0，否则 §10.2 的裁剪顺序失效。"""
    result = await wire_capabilities(
        manifests=[manifest()], context_for=context_for, resolve_setup=resolver()
    )
    assert model_providers_from(result.registry)[0].priority == BUILTIN_BASE_PRIORITY


# ---------------------------------------------------------------------------- manifest 翻译


def test_manifest_identity_and_declarations_travel_into_the_load_request() -> None:
    request = to_load_request(manifest(), Builtin())
    assert request.setup == "probe.module:setup"
    assert request.declarations[0].kind is CapabilityKind.MODEL


def test_overrides_travel_as_a_raw_string() -> None:
    """`D06` 定的约定：跨层只传原始串，两侧共用 `parse_capability_target()` 解码。"""
    decl = CapabilityDecl(kind=CapabilityKind.TOOL, name="fs.read", overrides="builtin:fs.read")
    assert to_declaration(decl).overrides == "builtin:fs.read"


# ------------------------------------------------------------------------------ 失败的传播


async def test_a_provider_failure_is_reported_but_still_freezes() -> None:
    """提供方不能中断装配；失败如实交给 Runtime 做最终能力校验。"""

    def boom(api: object) -> None:
        del api
        raise RuntimeError("boom")

    result = await wire_capabilities(
        manifests=[manifest()], context_for=context_for, resolve_setup=resolver(boom)
    )
    assert result.outcomes[0].error is not None
    assert result.registry.frozen


# -------------------------------------------------------------- Host 一致性的证明必须还在


def test_the_host_conformance_annotation_is_still_there() -> None:
    """证明由 basedpyright 完成，但测试验不了它（`exclude = ["**/tests"]`）。

    能验的只有「那句 `conformance: NucleaAPI = host` 还在」——有人把它删掉或改成
    `cast(NucleaAPI, host)`，Host 与 SDK 表面的一致性就再没有任何东西盯着了。
    """
    source = Path(inspect.getsourcefile(wiring) or "").read_text(encoding="utf-8")
    annotated = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.annotation, ast.Name)
        and node.annotation.id == "NucleaAPI"
    ]
    assert annotated, "runtime/wiring.py 里必须有一句 `x: NucleaAPI = host` 作为一致性证明"
    assert "cast" not in source, "一致性证明不得用 cast 绕过——那会让类型检查器闭嘴"


async def test_each_manifest_gets_its_own_context() -> None:
    """`D23` 改的那条：`context_for` 按 manifest 索引，因此配置块不会串。

    按 `ProviderId` 索引时这条会失败——内建全是 `Builtin()`，七份 manifest 拿到的是同一个
    ctx，`session-jsonl` 会读到 `model-openai` 的配置块。
    """
    seen: list[str] = []

    def spy(source: PluginManifest) -> PluginContext:
        seen.append(source.id)
        return FakePluginContext(source.id)

    first = manifest(plugin_id="alpha")
    second = manifest(plugin_id="beta")
    await wire_capabilities(
        manifests=[first, second], context_for=spy, resolve_setup=resolver()
    )
    assert seen == ["alpha", "beta"]


# --------------------------------------------------------------- 命名空间声明（`D38-A`）


def _namespaced_manifest() -> PluginManifest:
    return parse_manifest(
        {
            "id": "probe",
            "version": "1.0.0",
            "sdk_range": ">=0.1.0",
            "setup": "probe.module:setup",
            "capabilities": [{"kind": "tool", "name": "probe", "namespace": True}],
        },
        origin="test",
    )


def test_the_namespace_flag_crosses_the_layer_boundary() -> None:
    """`R2` 决定翻译只能在 `runtime/wiring.py` 一处——漏掉这个字段的后果是插件在
    `setup()` 里注册第一条工具时就被判成「未声明」。"""
    decl = _namespaced_manifest().capabilities[0]
    assert to_declaration(decl).namespace is True


def test_a_plain_declaration_stays_non_namespaced() -> None:
    assert to_declaration(manifest().capabilities[0]).namespace is False


async def test_a_namespaced_provider_can_register_names_it_never_declared() -> None:
    """整条路的端到端形态：manifest 只声明一个前缀，`setup()` 注册两条派生名，
    两条都进 registry 且都归这个提供方。"""

    def setup_two(api: object) -> None:
        for name in ("probe.alpha", "probe.beta"):
            api.register_tool(  # type: ignore[attr-defined]
                ToolSpec(name=name, description="x", parameters={"type": "object"}),
                EchoTool(),
            )

    wiring = await wire_capabilities(
        manifests=(_namespaced_manifest(),),
        context_for=context_for,
        provider_for=lambda source: Plugin(PluginId(source.id)),
        resolve_setup=resolver(setup_two),  # type: ignore[arg-type]
    )

    active = {entry["name"] for entry in wiring.report.to_json()["active"]}
    assert {"probe.alpha", "probe.beta"} <= active


async def test_a_namespaced_provider_that_registers_nothing_still_loads() -> None:
    """外部服务连不上时该插件注册零条工具，那不是「声明了却没注册」。"""

    def setup_nothing(api: object) -> None:
        del api

    wiring = await wire_capabilities(
        manifests=(_namespaced_manifest(),),
        context_for=context_for,
        provider_for=lambda source: Plugin(PluginId(source.id)),
        resolve_setup=resolver(setup_nothing),  # type: ignore[arg-type]
    )

    assert all(outcome.error is None for outcome in wiring.outcomes)
