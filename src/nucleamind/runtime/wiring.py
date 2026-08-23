"""组装根：把 manifest 清单经唯一 Host 注册进能力表（技术方案 §10.1 步骤 3、7）。

职责：把 `PluginManifest` 翻译成 kernel 认识的 `LoadRequest`，驱动
`kernel.plugins.load_into`，再解析覆盖并冻结 registry；产出 `Wiring`。
不负责：读配置、取实例锁、构造生产级 `PluginContext`、装 `OrchestratorDeps`、启动
长生命周期服务或决定失败后果；这些都在 bootstrap。

**这是 `R5` 的落点**：全项目只有 `runtime/` 可以同时 import `kernel/` 与 `sdk/`
（外加 `builtins/`），因此 manifest 到 `LoadRequest` 的翻译只能在这里。这不是权宜——
`kernel/` 里不出现第二套 Manifest 校验，保证声明语义只有一个来源。

**顺带地，这里是「Host 真的满足 `NucleaAPI`」的唯一证明地**。`pyproject.toml` 的
basedpyright 配置是 `include = ["src/nucleamind"]` + `exclude = ["**/tests"]`，所以测试
承担不了这个角色；而 `contracts/protocols.py` 写死了 `runtime_checkable` 只作诊断、
永不参与控制流，`isinstance` 也证明不了签名兼容。`_host_for()` 里那句
`host: NucleaAPI = ...` 的类型标注就是全部证明——它一旦不成立，严格模式当场报错。

**内建与外部插件走同一个函数**：`wire_capabilities()` 只是默认 `manifests=BUILTIN_MANIFESTS`
且默认“一切都是 `Builtin()`”。外部 Manifest 与 `Plugin(PluginId(...))` 也传入同一函数，
不需要第二条注册路径（`SDK-007`）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

from nucleamind.builtins.registry import BUILTIN_MANIFESTS
from nucleamind.contracts import Builtin, ProviderId
from nucleamind.kernel.plugins import (
    CapabilityDeclaration,
    CapabilityHost,
    LoadOutcome,
    LoadRequest,
    SetupFn,
    import_setup,
    load_into,
)
from nucleamind.kernel.registry import (
    CapabilityRegistry,
    RegistrationBatch,
    ResolutionReport,
    SuppressedCapabilities,
    resolve_into,
)
from nucleamind.sdk import CapabilityDecl, NucleaAPI, PluginContext, PluginManifest

__all__ = [
    "CapabilityFilter",
    "Wiring",
    "builtin_provider",
    "to_declaration",
    "to_load_request",
    "wire_capabilities",
]

#: 「这条能力声明这次还算数吗」。装配根按配置回答，`to_load_request()` 据此裁剪声明
#: （`TOL-006`，见那里的 docstring）。拿到整份 manifest 而不只是 `CapabilityDecl`，
#: 是因为回答这个问题需要知道是谁的配置块。
CapabilityFilter = Callable[[PluginManifest, CapabilityDecl], bool]


@dataclass(frozen=True, slots=True)
class Wiring:
    """一次装配的产物：冻结的能力表、覆盖解析报告与逐提供方的加载结果。

    **不在这里 `raise_if_failed()`**：失败后果（`critical`、`on_override_failure`、
    CLI 入口强制回落）是 bootstrap 的策略，装配只负责如实交出发生了什么。
    """

    registry: CapabilityRegistry
    report: ResolutionReport
    outcomes: tuple[LoadOutcome, ...] = ()


def builtin_provider(manifest: PluginManifest) -> ProviderId:
    """内建清单里的一切都以 `Builtin()` 身份注册（priority 基准值 0）。"""
    del manifest
    return Builtin()


def to_declaration(decl: CapabilityDecl) -> CapabilityDeclaration:
    """把一条 manifest 能力声明翻成 kernel 投影。

    **`priority` 只在作者显式写过时才传下去**。`CapabilityDecl.priority` 的默认值是 100
    （技术方案 §7.2），照搬会让每一项内建能力都拿到 100，而 §6.1 规则 1 定的内建基准是
    **0**。pydantic 的 `model_fields_set` 恰好能回答「作者到底写没写」，因此留 `None`
    让 `base_priority_for()` 决定——内建 0、插件 100，规则仍只有一处实现。
    """
    stated = "priority" in decl.model_fields_set
    return CapabilityDeclaration(
        kind=decl.kind,
        name=decl.name,
        overrides=decl.overrides,
        priority=decl.priority if stated else None,
        namespace=decl.namespace,
    )


def to_load_request(
    manifest: PluginManifest,
    provider: ProviderId,
    *,
    keep: CapabilityFilter | None = None,
) -> LoadRequest:
    """把一份 manifest 投影成 kernel 认识的加载请求。

    `critical` 是提供方级的，Host 会把它灌进 `RegisteredHook` 与
    `RegisteredContextProvider`——`kernel/` 不认识 manifest，而 `CTX-005`/`PLG-004`
    的分叉必须在 kernel 里判。

    **`keep` 是 `TOL-006` 的落点**。`CapabilityHost` 要求 manifest 声明的每一项
    都真的被注册，而「按名字单独禁用一个工具」要求被禁用的那项从 registry 里消失——静态
    manifest 无法按配置少声明一项，于是由这里裁掉。裁剪与 `setup()` 的注册决定必须**同源
    于同一份配置**（`builtins/tools_fs` 导出 `enabled_tool_names()` 正是为此），否则
    `finish()` 会以 `PLUGIN_LOAD_FAILED` 报「声明了却没注册」——那个报错是对的。

    刻意**不**在这里读配置：装配根才知道每个提供方的配置块长什么样，而 `runtime/wiring.py`
    的职责是翻译而不是决策。`keep` 为 `None` 时保留全部声明。
    """
    declarations = manifest.capabilities
    if keep is not None:
        declarations = tuple(decl for decl in declarations if keep(manifest, decl))
    return LoadRequest(
        plugin_id=manifest.id,
        provider=provider,
        setup=manifest.setup,
        declarations=tuple(to_declaration(decl) for decl in declarations),
        dependencies=manifest.dependencies,
        critical=manifest.critical,
    )


async def wire_capabilities(
    *,
    manifests: Sequence[PluginManifest] = BUILTIN_MANIFESTS,
    context_for: Callable[[PluginManifest], PluginContext],
    provider_for: Callable[[PluginManifest], ProviderId] = builtin_provider,
    resolve_setup: Callable[[str], SetupFn] = import_setup,
    disabled: Mapping[ProviderId, str] | None = None,
    suppressed: SuppressedCapabilities | None = None,
    keep: CapabilityFilter | None = None,
    halt_on_critical: bool = True,
) -> Wiring:
    """注册全部 manifest 声明的能力 → 解析覆盖 → 冻结，返回装配产物。

    `manifests` 默认取 `BUILTIN_MANIFESTS`。`context_for` 必填；给它一个空的默认桩会
    让测试对象悄悄进入生产路径。生产实现位于 `runtime/plugin_context.py`。

    **`context_for` 按 manifest 而不是按 `ProviderId` 索引**：全部内建共用
    一个 `Builtin()`，按提供方索引会让七份内建拿到同一个配置块与同一个状态目录——
    `session-jsonl` 会读到 `model-openai` 的配置。manifest 才是「这是谁」的唯一答案。

    `keep` 按配置裁掉本次不生效的能力声明（`TOL-006`，见 `to_load_request()`）。它对
    每一份 Manifest 一视同仁，不存在某个内建工具专用的裁剪路径。

    `suppressed` 按**能力**抑制（`on_disable=leave_missing`，见
    `runtime/plugin_disable.py`）。与 `keep` 的区别是它作用在解析而不是注册上：那项能力
    照常注册、照常出现在报告里，只是标着「被禁用」而不生效。这是刻意的——`nm capabilities`
    要答得出「它为什么不在」，而一项从未注册过的能力在报告里连一行都没有。

    `halt_on_critical=False` 把每一份请求都当作非关键，供只读诊断路径使用：
    `nm capabilities` 要在**凭据还没导出**时也答得出「哪项能力由谁提供」，而
    `model-openai` 是 `critical=True` 且它的 `setup()` 会去取密钥——照常抛出会让最需要
    看一眼能力表的那一刻恰好看不到。失败照样进 `Wiring.outcomes`，由调用方印出来。
    **这是「失败的后果由装配根决定」（`PLG-004`）的一次应用**，不是把 `critical` 改掉：
    那个标志在 manifest 里一个字都没动，只是这一次调用不据它中止。

    **异常约定**：`critical=True` 的提供方加载失败时原样抛出（`halt_on_critical=False`
    时不抛）；其余失败记进 `Wiring.outcomes`。覆盖冲突不抛，进 `report.failures`
    由调用方处置。
    """
    registry = CapabilityRegistry()
    requests: list[LoadRequest] = []
    # `LoadRequest` 不带 manifest（Kernel 不认识 SDK Manifest），因此这里
    # 自己记一张回查表。按对象身份索引：同一份 manifest 出现两次是调用方的错，不该在
    # 这里被静默合并成一个。
    origin: dict[int, PluginManifest] = {}
    for manifest in manifests:
        request = to_load_request(manifest, provider_for(manifest), keep=keep)
        if not halt_on_critical:
            request = replace(request, critical=False)
        origin[id(request)] = manifest
        requests.append(request)

    def host_for(batch: RegistrationBatch, request: LoadRequest) -> CapabilityHost[PluginContext]:
        host = CapabilityHost(
            batch,
            context_for(origin[id(request)]),
            declarations=request.declarations,
            critical=request.critical,
        )
        # 「Host 满足 `NucleaAPI`」的唯一证明点，见模块 docstring。
        conformance: NucleaAPI = host
        del conformance
        return host

    outcomes = await load_into(
        registry, requests, host_for=host_for, resolve_setup=resolve_setup
    )
    report = resolve_into(registry, disabled=disabled, suppressed=suppressed)
    return Wiring(registry=registry, report=report, outcomes=outcomes)
