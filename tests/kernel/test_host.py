"""唯一 Host 的注册分派测试（`D16` 验收；`SDK-007`、`BAS-005`、`EDG-102`、`EDG-103`）。

四条主线：

- **一个 Host 服务两种身份**。开发方案点名的验收是「用同一个 Host API 分别注册一个 Fake
  builtin 和一个 Fake plugin，断言 registry 结果结构一致，只允许 `ProviderId` 不同」。
  这条测试是「不存在内建专用注册 API」（`SDK-007`）唯一可断言的形态。
- **声明表是全集**。`overrides` 只能来自 manifest（`EDG-102`：覆盖永不由加载顺序决定），
  因此未声明的注册与已声明却没注册都是错误，两者靠 `detail` 区分。
- **HOOK 的命名与优先级**。`on()` 既没有 name 也分不清「作者写了 100」和「什么都没写」，
  两件事各有一条测试钉住结论。
- **Host 不提交批次**。`setup` 的返回时刻在 loader 的作用域里，`EDG-103` 要求提交发生在
  那之后。

`ctx` 一律用 `FakePluginContext`：`D16` 的 Host 只持有并转交它、一个成员都不碰，
因此这些用例不该也不需要构造生产资源服务（那是 `tests/runtime/` 的事）。
"""

from __future__ import annotations

import inspect

import pytest

from nucleamind.contracts import (
    Builtin,
    CapabilityKind,
    CommandSpec,
    ErrorCode,
    HookName,
    NucleaError,
    Plugin,
    PluginId,
    ProviderId,
    ToolSpec,
)
from nucleamind.kernel.plugins import (
    CapabilityDeclaration,
    CapabilityHost,
    RegisteredChannel,
    RegisteredCliEntry,
    RegisteredContextCompactor,
    RegisteredMemoryProvider,
    RegisteredModelProvider,
    RegisteredSessionStore,
)
from nucleamind.kernel.registry import (
    BUILTIN_BASE_PRIORITY,
    PLUGIN_BASE_PRIORITY,
    CapabilityRegistry,
    RegistrationBatch,
)
from nucleamind.kernel.routing import RegisteredCommand
from nucleamind.kernel.turn import RegisteredContextProvider, RegisteredHook, RegisteredTool
from nucleamind.sdk import NucleaAPI
from nucleamind.sdk.testing import (
    ECHO_SPEC,
    EchoTool,
    FakeCliEntry,
    FakeMemoryProvider,
    FakeModelProvider,
    FakePluginContext,
    InMemorySessionStore,
    NullChannel,
    RecordingHook,
    StaticContextCompactor,
    StaticContextProvider,
)

# ------------------------------------------------------------------------------------ 夹具


def declare(kind: CapabilityKind, name: str, **kwargs: object) -> CapabilityDeclaration:
    return CapabilityDeclaration(kind=kind, name=name, **kwargs)  # type: ignore[arg-type]


def make_host(
    *declarations: CapabilityDeclaration,
    provider: ProviderId | None = None,
) -> tuple[CapabilityRegistry, RegistrationBatch, CapabilityHost[FakePluginContext]]:
    registry = CapabilityRegistry()
    batch = registry.batch(provider or Builtin())
    host = CapabilityHost(batch, FakePluginContext(), declarations=declarations)
    return registry, batch, host


def register_everything(host: CapabilityHost[FakePluginContext]) -> None:
    """一个把 10 类能力全注册一遍的 `setup`，两种身份共用它。"""
    host.register_tool(ECHO_SPEC, EchoTool())
    host.register_command(CommandSpec(name="ping", description="ping"), _NullCommand())
    host.register_context_provider("ctx", StaticContextProvider())
    host.register_context_compactor("compact", StaticContextCompactor())
    host.register_model_provider("model", FakeModelProvider())
    host.register_channel("chan", NullChannel())
    host.register_memory_provider("mem", FakeMemoryProvider())
    host.register_session_store("store", InMemorySessionStore())
    host.register_cli_entry("cli", FakeCliEntry())
    host.on(HookName.TURN_START, RecordingHook())


ALL_DECLARATIONS = (
    declare(CapabilityKind.TOOL, ECHO_SPEC.name),
    declare(CapabilityKind.COMMAND, "ping"),
    declare(CapabilityKind.CONTEXT, "ctx"),
    declare(CapabilityKind.COMPACTOR, "compact"),
    declare(CapabilityKind.MODEL, "model"),
    declare(CapabilityKind.CHANNEL, "chan"),
    declare(CapabilityKind.MEMORY, "mem"),
    declare(CapabilityKind.SESSION_STORE, "store"),
    declare(CapabilityKind.CLI_ENTRY, "cli"),
    declare(CapabilityKind.HOOK, HookName.TURN_START.value),
)


class _NullCommand:
    async def handle(self, invocation: object, cancel: object) -> object:  # pragma: no cover
        raise NotImplementedError


# --------------------------------------------------------------------------- 与 SDK 表面一致


def test_host_satisfies_the_nuclea_api_protocol() -> None:
    """结构化子类型：Host 不继承 `NucleaAPI`（`R2` 禁止 kernel import sdk）。

    这里的 `isinstance` 只是诊断回声——真正的证明是 `runtime/wiring.py` 里那句类型标注，
    因为 basedpyright 的 `exclude = ["**/tests"]` 让测试验不了签名兼容性。
    """
    _, _, host = make_host()
    assert isinstance(host, NucleaAPI)


def test_ten_methods_cover_ten_kinds() -> None:
    """10 个注册方法与 `CapabilityKind` 的 10 个取值一一对应，不多不少。"""
    registry, batch, host = make_host(*ALL_DECLARATIONS)
    register_everything(host)
    batch.commit()
    assert {item.ref.kind for item in registry.registrations} == set(CapabilityKind)


def test_ctx_is_handed_back_untouched() -> None:
    """Host 只持有并转交 ctx，不解释它。"""
    ctx = FakePluginContext("acme")
    registry = CapabilityRegistry()
    host = CapabilityHost(registry.batch(Builtin()), ctx)
    assert host.ctx is ctx


# --------------------------------------------------------- 同一个 Host 服务内建与插件（点名验收）


def test_the_same_host_gives_builtin_and_plugin_identical_structure() -> None:
    """开发方案点名的验收：结构一致，只允许 `ProviderId` 与由它派生的 priority 不同。

    「不存在内建专用注册 API」（`SDK-007`）没有别的可断言形态——两条路径产出的登记必须
    逐字段相同，否则「同一条注册契约」就只是一句话。
    """
    plugin: ProviderId = Plugin(PluginId("acme"))
    shapes: dict[str, list[tuple[CapabilityKind, str, str, str | None]]] = {}
    priorities: dict[str, set[int]] = {}
    for label, provider in (("builtin", Builtin()), ("plugin", plugin)):
        registry, batch, host = make_host(*ALL_DECLARATIONS, provider=provider)
        register_everything(host)
        batch.commit()
        shapes[label] = [
            (item.ref.kind, item.ref.name, type(item.payload).__name__, item.overrides)
            for item in registry.registrations
        ]
        priorities[label] = {item.priority for item in registry.registrations}
        # 提供方在每一条登记上都如实记着，且只可能是这一个。
        assert {item.ref.provider for item in registry.registrations} == {provider}

    assert shapes["builtin"] == shapes["plugin"]
    # 唯一被允许的差别：priority 基准值由提供方决定（§6.1 规则 1）。
    assert priorities["builtin"] == {BUILTIN_BASE_PRIORITY}
    assert priorities["plugin"] == {PLUGIN_BASE_PRIORITY}


@pytest.mark.parametrize(
    ("kind", "shape"),
    [
        (CapabilityKind.TOOL, RegisteredTool),
        (CapabilityKind.COMMAND, RegisteredCommand),
        (CapabilityKind.CONTEXT, RegisteredContextProvider),
        (CapabilityKind.COMPACTOR, RegisteredContextCompactor),
        (CapabilityKind.MODEL, RegisteredModelProvider),
        (CapabilityKind.CHANNEL, RegisteredChannel),
        (CapabilityKind.MEMORY, RegisteredMemoryProvider),
        (CapabilityKind.SESSION_STORE, RegisteredSessionStore),
        (CapabilityKind.CLI_ENTRY, RegisteredCliEntry),
        (CapabilityKind.HOOK, RegisteredHook),
    ],
    ids=lambda value: value.value if isinstance(value, CapabilityKind) else value.__name__,
)
def test_each_kind_gets_its_declared_payload_shape(kind: CapabilityKind, shape: type) -> None:
    """载荷形状由注册方定死，取回函数会当场核对——这里断言 Host 交出的就是那个形状。"""
    registry, batch, host = make_host(*ALL_DECLARATIONS)
    register_everything(host)
    batch.commit()
    payloads = [item.payload for item in registry.registrations if item.ref.kind is kind]
    assert payloads and all(isinstance(payload, shape) for payload in payloads)


# ------------------------------------------------------------------------------- 声明表是全集


def test_registering_an_undeclared_capability_is_rejected() -> None:
    """放行未声明的注册，manifest 的 `capabilities` 就成了一份没有约束力的文档。"""
    _, _, host = make_host(declare(CapabilityKind.TOOL, "other.tool"))
    with pytest.raises(NucleaError) as excinfo:
        host.register_tool(ECHO_SPEC, EchoTool())
    assert excinfo.value.code is ErrorCode.PLUGIN_LOAD_FAILED
    assert excinfo.value.detail["capability"] == f"tool:{ECHO_SPEC.name}"


def test_a_declared_capability_that_is_never_registered_fails_at_finish() -> None:
    """声明了却没注册说明 manifest 骗过了阶段 A——用户会看到一项查得到却不存在的能力。"""
    _, _, host = make_host(
        declare(CapabilityKind.TOOL, ECHO_SPEC.name),
        declare(CapabilityKind.MODEL, "model"),
    )
    host.register_tool(ECHO_SPEC, EchoTool())
    with pytest.raises(NucleaError) as excinfo:
        host.finish()
    assert excinfo.value.code is ErrorCode.PLUGIN_LOAD_FAILED
    assert excinfo.value.detail["unfulfilled"] == ["model:model"]


def test_finish_passes_when_every_declaration_is_fulfilled() -> None:
    _, _, host = make_host(*ALL_DECLARATIONS)
    register_everything(host)
    host.finish()


def test_overrides_come_from_the_declaration_not_from_the_call() -> None:
    """`EDG-102`：覆盖只能显式声明。注册调用本身没有表达覆盖的参数，这是刻意的。"""
    registry, batch, host = make_host(
        declare(CapabilityKind.TOOL, ECHO_SPEC.name, overrides="builtin:fs.read"),
        provider=Plugin(PluginId("acme")),
    )
    host.register_tool(ECHO_SPEC, EchoTool())
    batch.commit()
    assert registry.registrations[0].overrides == "builtin:fs.read"


# ---------------------------------------------------------------------------------- priority


def test_declared_priority_wins_over_the_provider_baseline() -> None:
    registry, batch, host = make_host(declare(CapabilityKind.CONTEXT, "ctx", priority=7))
    host.register_context_provider("ctx", StaticContextProvider())
    batch.commit()
    assert registry.registrations[0].priority == 7


def test_an_unstated_priority_falls_back_to_the_provider_baseline() -> None:
    """声明里没写就该落到基准值——内建 0、插件 100，规则只有 `base_priority_for()` 一处。"""
    registry, batch, host = make_host(declare(CapabilityKind.CONTEXT, "ctx"))
    host.register_context_provider("ctx", StaticContextProvider())
    batch.commit()
    assert registry.registrations[0].priority == BUILTIN_BASE_PRIORITY


# -------------------------------------------------------------------------------- HOOK 的特殊性


def test_on_default_priority_equals_the_plugin_baseline() -> None:
    """把 `on()` 的签名默认值钉住：Host 的「等于基准值即视为未声明」全靠这个前提。

    SDK 改了默认值而这里没改，内建 Hook 的排序会静默出错——那正是这条测试要拦的。
    """
    assert inspect.signature(NucleaAPI.on).parameters["priority"].default == PLUGIN_BASE_PRIORITY


def test_the_sdk_default_priority_is_treated_as_unstated() -> None:
    """内建 Hook 必须落到基准 0，否则「内建排在插件前」与「内建最后被裁」同时失效。"""
    registry, batch, host = make_host(declare(CapabilityKind.HOOK, HookName.TURN_START.value))
    host.on(HookName.TURN_START, RecordingHook())
    batch.commit()
    assert registry.registrations[0].priority == BUILTIN_BASE_PRIORITY


def test_an_explicit_hook_priority_overrides_the_declaration() -> None:
    registry, batch, host = make_host(
        declare(CapabilityKind.HOOK, HookName.TURN_START.value, priority=5)
    )
    host.on(HookName.TURN_START, RecordingHook(), priority=42)
    batch.commit()
    assert registry.registrations[0].priority == 42


def test_two_handlers_on_one_hook_get_distinct_registry_names() -> None:
    """HOOK 是 MULTI，同一提供方绑两个 handler 合法；但批次内 `(kind, name)` 必须唯一。

    两条登记共享同一条声明——声明表始终按基名回查。
    """
    registry, batch, host = make_host(declare(CapabilityKind.HOOK, HookName.TURN_START.value))
    host.on(HookName.TURN_START, RecordingHook())
    host.on(HookName.TURN_START, RecordingHook())
    batch.commit()
    assert [item.ref.name for item in registry.registrations] == ["turn_start", "turn_start.2"]


# ------------------------------------------------------------------------------ 批次的所有权


def test_the_host_does_not_commit_the_batch() -> None:
    """提交归 loader：`setup` 的返回时刻在它的作用域里，`EDG-103` 要求提交发生在那之后。"""
    registry, _, host = make_host(declare(CapabilityKind.TOOL, ECHO_SPEC.name))
    host.register_tool(ECHO_SPEC, EchoTool())
    host.finish()
    assert registry.registrations == ()


def test_registering_after_commit_raises() -> None:
    _, batch, host = make_host(
        declare(CapabilityKind.TOOL, ECHO_SPEC.name),
        declare(CapabilityKind.MODEL, "model"),
    )
    host.register_tool(ECHO_SPEC, EchoTool())
    batch.commit()
    with pytest.raises(NucleaError) as excinfo:
        host.register_model_provider("model", FakeModelProvider())
    assert excinfo.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


# --------------------------------------------------------------- 命名空间声明（`D38-A`）
#
# 这套机制是为「能力名要连上外部服务才知道」开的（MCP 的远端工具名只有 `list_tools`
# 之后才可知，而 manifest 是静态的）。形状照 `on()` 那条已有的先例：一条声明、N 次注册。


def namespace(kind: CapabilityKind, prefix: str, **kwargs: object) -> CapabilityDeclaration:
    return CapabilityDeclaration(kind=kind, name=prefix, namespace=True, **kwargs)  # type: ignore[arg-type]


def _tool(name: str) -> ToolSpec:
    return ToolSpec(name=name, description="x", parameters={"type": "object"})


def test_a_namespace_declaration_admits_any_name_under_the_prefix() -> None:
    registry, batch, host = make_host(namespace(CapabilityKind.TOOL, "mcp"))
    host.register_tool(_tool("mcp.fs.read"), EchoTool())
    host.register_tool(_tool("mcp.git.status"), EchoTool())
    host.finish()
    batch.commit()
    assert sorted(r.ref.name for r in registry.registrations) == ["mcp.fs.read", "mcp.git.status"]


def test_a_namespace_does_not_admit_the_bare_prefix() -> None:
    """前缀本身不是它放行的名字之一——要注册 `mcp` 就再写一条精确声明。"""
    _, _, host = make_host(namespace(CapabilityKind.TOOL, "mcp"))
    with pytest.raises(NucleaError) as excinfo:
        host.register_tool(_tool("mcp"), EchoTool())
    assert excinfo.value.code is ErrorCode.PLUGIN_LOAD_FAILED


def test_the_prefix_comparison_lands_on_a_separator() -> None:
    """否则 `mcpx.read` 会被判成 `mcp` 的后代（`WorkspaceGuard` 的路径前缀同一条道理）。"""
    _, _, host = make_host(namespace(CapabilityKind.TOOL, "mcp"))
    with pytest.raises(NucleaError):
        host.register_tool(_tool("mcpx.read"), EchoTool())


def test_a_namespace_only_admits_its_own_kind() -> None:
    _, _, host = make_host(namespace(CapabilityKind.TOOL, "mcp"))
    with pytest.raises(NucleaError):
        host.register_command(CommandSpec(name="mcp.list", description="x"), _NullCommand())


def test_an_exact_declaration_wins_over_a_namespace() -> None:
    """两条都匹配时静默挑一个，就等于让「哪条声明生效」取决于表的遍历顺序。"""
    registry, batch, host = make_host(
        namespace(CapabilityKind.TOOL, "mcp"),
        declare(CapabilityKind.TOOL, "mcp.probe", priority=7),
    )
    host.register_tool(_tool("mcp.probe"), EchoTool())
    host.finish()
    batch.commit()
    assert registry.registrations[0].priority == 7


def test_two_namespaces_matching_the_same_name_is_an_error() -> None:
    """`mcp` 与 `mcp.remote` 都能放行 `mcp.remote.read`，而它们的 `priority` 可能不同。"""
    _, _, host = make_host(
        namespace(CapabilityKind.TOOL, "mcp"),
        namespace(CapabilityKind.TOOL, "mcp.remote"),
    )
    with pytest.raises(NucleaError) as excinfo:
        host.register_tool(_tool("mcp.remote.read"), EchoTool())
    assert excinfo.value.code is ErrorCode.PLUGIN_LOAD_FAILED
    assert excinfo.value.detail["namespaces"] == ["mcp", "mcp.remote"]


def test_a_namespace_that_registers_nothing_still_passes_finish() -> None:
    """一个 MCP server 连不上时该插件注册零条工具，那是它如实反映外部状态。"""
    _, _, host = make_host(namespace(CapabilityKind.TOOL, "mcp"))
    host.finish()


def test_an_exact_declaration_alongside_a_namespace_is_still_required() -> None:
    """豁免只给命名空间那一条，精确声明仍然必须兑现。"""
    _, _, host = make_host(
        namespace(CapabilityKind.TOOL, "mcp"),
        declare(CapabilityKind.TOOL, "mcp.probe"),
    )
    with pytest.raises(NucleaError) as excinfo:
        host.finish()
    assert excinfo.value.detail["unfulfilled"] == ["tool:mcp.probe"]


def test_a_namespaced_registration_inherits_the_declared_priority() -> None:
    registry, batch, host = make_host(namespace(CapabilityKind.TOOL, "mcp", priority=3))
    host.register_tool(_tool("mcp.a"), EchoTool())
    host.finish()
    batch.commit()
    assert registry.registrations[0].priority == 3
