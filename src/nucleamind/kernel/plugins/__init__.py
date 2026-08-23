"""插件运行时（宿主侧）：唯一的注册分派与静态清单 bootstrap（技术方案 §6.1、§7.3、§7.5）。

职责：re-export `declarations`（注册意图的 kernel 投影）、`discovery`（两条显式来源的
插件发现）、`host`（唯一的 Host `NucleaAPI` 实现）、`capabilities`（五个单值 kind 的载荷
形状与取回函数）、`loader`（阶段 A 的依赖拓扑、配置校验与状态版本）与 `builtin_loader`
（把一批 `LoadRequest` 跑成注册）与 `lifecycle`（阶段状态机、
停止顺序与停止超时）的公开表面。
不负责：校验 manifest、构造 `PluginContext`、实现被守卫的资源门面、决定谁被启用——
那些分别在 `sdk/manifest.py`、`runtime/plugin_context.py`、`runtime/access/` 与 `runtime/`；
本包不读配置、不访问网络（`loader.py` 只读写
插件状态目录里的版本标记）。

包内依赖单向：`declarations`、`capabilities`、`discovery` 与 `loader` 互不相识，
`host` 用前两个，`builtin_loader` 用 `host`。

**本包不 import `sdk/`**（规则 `R2`）。因此 manifest 到 `LoadRequest` 的翻译不在这里，
而在 `runtime/wiring.py`——这也正是内建与外部插件共用同一条注册路径的落点：两者只在
「谁产出 `LoadRequest`」上不同，注册接口完全一致（`SDK-007`）。
"""

from __future__ import annotations

from .builtin_loader import LoadOutcome, RegistrationHost, SetupFn, import_setup, load_into
from .capabilities import (
    CapabilityBinding,
    ChannelBinding,
    CliEntryBinding,
    ContextCompactorBinding,
    MemoryProviderBinding,
    ModelProviderBinding,
    RegisteredChannel,
    RegisteredCliEntry,
    RegisteredContextCompactor,
    RegisteredMemoryProvider,
    RegisteredModelProvider,
    RegisteredSessionStore,
    SessionStoreBinding,
    channels_from,
    cli_entry_from,
    context_compactors_from,
    memory_providers_from,
    model_providers_from,
    session_store_from,
)
from .declarations import CapabilityDeclaration, LoadRequest
from .discovery import (
    ENTRY_POINT_GROUP,
    Discovery,
    EntryPointLister,
    PluginCandidate,
    discover,
    read_candidate,
)
from .host import CapabilityHost
from .lifecycle import (
    DEFAULT_STOP_TIMEOUT_MS,
    PHASE_STATES,
    PHASE_TRANSITIONS,
    PluginLifecycle,
    PluginPhase,
    StopAction,
    StopOutcome,
    StopUnit,
    stop_order,
    stop_plugins,
    units_for,
)
from .loader import (
    STATE_FILE,
    STATE_VERSION_KEY,
    LoadPlan,
    PlanFailure,
    PlanNode,
    check_state_version,
    plan_load_order,
    validate_plugin_config,
)

__all__ = [
    "DEFAULT_STOP_TIMEOUT_MS",
    "ENTRY_POINT_GROUP",
    "PHASE_STATES",
    "PHASE_TRANSITIONS",
    "STATE_FILE",
    "STATE_VERSION_KEY",
    "CapabilityBinding",
    "CapabilityDeclaration",
    "CapabilityHost",
    "ChannelBinding",
    "CliEntryBinding",
    "ContextCompactorBinding",
    "Discovery",
    "EntryPointLister",
    "LoadOutcome",
    "LoadPlan",
    "LoadRequest",
    "MemoryProviderBinding",
    "ModelProviderBinding",
    "PlanFailure",
    "PlanNode",
    "PluginCandidate",
    "PluginLifecycle",
    "PluginPhase",
    "RegisteredChannel",
    "RegisteredCliEntry",
    "RegisteredContextCompactor",
    "RegisteredMemoryProvider",
    "RegisteredModelProvider",
    "RegisteredSessionStore",
    "RegistrationHost",
    "SessionStoreBinding",
    "SetupFn",
    "StopAction",
    "StopOutcome",
    "StopUnit",
    "channels_from",
    "check_state_version",
    "cli_entry_from",
    "context_compactors_from",
    "discover",
    "import_setup",
    "load_into",
    "memory_providers_from",
    "model_providers_from",
    "plan_load_order",
    "read_candidate",
    "session_store_from",
    "stop_order",
    "stop_plugins",
    "units_for",
    "validate_plugin_config",
]
