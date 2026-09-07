"""第 1 层：公开数据契约。

职责：定义 Kernel、内建能力与插件之间传递的纯数据类型与窄 Protocol，并统一提供
递归类型别名 `JsonValue`。
不负责：实现任何行为、依赖 karyvia 内的其他层、执行 IO。

三条不变量：契约对象一律不可变；契约层不出现 `Any`；契约层不出现 IO。

子模块按职责分为三组：

- 基础标识与诊断：`ids` / `errors` / `events`；
- 消息与执行数据：`metadata` / `message` / `session` / `context` / `tool` / `model`；
- 能力边界：`command` / `capability` / `protocols` / `compaction`。

模块间的依赖方向（子模块只在 `TYPE_CHECKING` 下反向从包根导入 `JsonValue`，
运行时不成环）：

```text
errors ← ids ← metadata ← {message, session, context, tool} ← {model, command}
       ← capability ← protocols
```
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TypeAlias, Union

#: JSON 可表达的全部值。契约层用它代替 `Any`——形状在边界固定，不向核心泄漏。
JsonValue: TypeAlias = Union[
    str,
    int,
    float,
    bool,
    None,
    Sequence["JsonValue"],
    Mapping[str, "JsonValue"],
]

#: JSON Schema 文档。语义上就是 `Mapping[str, JsonValue]`，单独命名只为签名可读。
JsonSchema: TypeAlias = Mapping[str, JsonValue]

# 子模块的注解引用上面的 `JsonValue`，因此定义必须先于导入；子模块只在
# `TYPE_CHECKING` 下反向导入它，运行时不成环。
from .capability import (  # noqa: E402
    CAPABILITY_ARITY,
    HOOK_KINDS,
    HOOK_REQUIRED_SLOTS,
    Builtin,
    CapabilityArity,
    CapabilityKind,
    CapabilityRef,
    HookAction,
    HookContext,
    HookKind,
    HookName,
    HookOutcome,
    Plugin,
    ProviderId,
    parse_capability_target,
    parse_provider,
    provider_sort_key,
)
from .command import (  # noqa: E402
    CommandInvocation,
    CommandParam,
    CommandResult,
    CommandSpec,
    Disposition,
)
from .compaction import (  # noqa: E402
    TurnCompactionRequest,
    TurnCompactionResult,
    TurnContextUnit,
    TurnContextUnitKind,
)
from .context import (  # noqa: E402
    UNTRUSTED_DATA_PREFIX,
    ContextFragment,
    FragmentKind,
    FragmentScope,
    Sensitivity,
    TrustLevel,
    wrap_untrusted,
)
from .errors import (  # noqa: E402
    CODE_CATEGORIES,
    ErrorCategory,
    ErrorCode,
    KaryviaError,
    SecretStr,
    redact,
    scrub,
)
from .events import EventFamily, EventName, RuntimeEvent  # noqa: E402
from .ids import (  # noqa: E402
    Correlation,
    InstanceId,
    PluginId,
    SessionKey,
    TurnId,
    validate_identifier,
)
from .message import (  # noqa: E402
    AttachmentRef,
    AttachmentSource,
    InboundMessage,
    OutboundMessage,
    Sender,
    StreamState,
)
from .metadata import EMPTY_METADATA, normalize_metadata  # noqa: E402
from .model import (  # noqa: E402
    ChunkKind,
    ModelCapability,
    ModelChunk,
    ModelInfo,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    OpaqueBlock,
    SamplingParams,
    StopReason,
    TokenUsage,
)
from .protocols import (  # noqa: E402
    CancelSignal,
    Channel,
    CliEntry,
    CommandHandler,
    CompactionModel,
    ContextProvider,
    HookHandler,
    InstanceView,
    MemoryProvider,
    ModelProvider,
    SessionStore,
    ToolHandler,
    TurnContextCompactor,
    TurnControl,
)
from .session import (  # noqa: E402
    SESSION_SCHEMA_VERSION,
    CancelReason,
    Role,
    SessionMessage,
    SessionSnapshot,
    TurnOutcome,
    TurnStatus,
)
from .tool import (  # noqa: E402
    ArtifactRef,
    Concurrency,
    RiskLevel,
    SideEffect,
    ToolCall,
    ToolInvocation,
    ToolResult,
    ToolSpec,
)

__all__ = [
    "CAPABILITY_ARITY",
    "CODE_CATEGORIES",
    "EMPTY_METADATA",
    "HOOK_KINDS",
    "HOOK_REQUIRED_SLOTS",
    "SESSION_SCHEMA_VERSION",
    "UNTRUSTED_DATA_PREFIX",
    "ArtifactRef",
    "AttachmentRef",
    "AttachmentSource",
    "Builtin",
    "CancelReason",
    "CancelSignal",
    "CapabilityArity",
    "CapabilityKind",
    "CapabilityRef",
    "Channel",
    "ChunkKind",
    "CliEntry",
    "CommandHandler",
    "CommandInvocation",
    "CommandParam",
    "CommandResult",
    "CommandSpec",
    "CompactionModel",
    "Concurrency",
    "ContextFragment",
    "ContextProvider",
    "Correlation",
    "Disposition",
    "ErrorCategory",
    "ErrorCode",
    "EventFamily",
    "EventName",
    "FragmentKind",
    "FragmentScope",
    "HookAction",
    "HookContext",
    "HookHandler",
    "HookKind",
    "HookName",
    "HookOutcome",
    "InboundMessage",
    "InstanceId",
    "InstanceView",
    "JsonSchema",
    "JsonValue",
    "MemoryProvider",
    "ModelCapability",
    "ModelChunk",
    "ModelInfo",
    "ModelMessage",
    "ModelProvider",
    "ModelRequest",
    "ModelResponse",
    "OpaqueBlock",
    "KaryviaError",
    "OutboundMessage",
    "Plugin",
    "PluginId",
    "ProviderId",
    "Role",
    "RiskLevel",
    "RuntimeEvent",
    "SamplingParams",
    "SecretStr",
    "Sender",
    "Sensitivity",
    "SessionKey",
    "SessionMessage",
    "SessionSnapshot",
    "SessionStore",
    "SideEffect",
    "StopReason",
    "StreamState",
    "TokenUsage",
    "ToolCall",
    "ToolHandler",
    "ToolInvocation",
    "ToolResult",
    "ToolSpec",
    "TrustLevel",
    "TurnControl",
    "TurnCompactionRequest",
    "TurnCompactionResult",
    "TurnContextCompactor",
    "TurnContextUnit",
    "TurnContextUnitKind",
    "TurnId",
    "TurnOutcome",
    "TurnStatus",
    "normalize_metadata",
    "parse_capability_target",
    "parse_provider",
    "provider_sort_key",
    "redact",
    "scrub",
    "validate_identifier",
    "wrap_untrusted",
]
