"""官方插件 `anthropic`：Anthropic 原生 Messages API 的 Model Provider（开发方案 `D32`）。

职责：声明一条 `MODEL` 能力，把 `ModelRequest` 翻成 `POST /v1/messages`、把响应与 SSE
翻回 `ModelResponse` / `ModelChunk`。
不负责：执行 turn、裁剪上下文、重试与故障转移（分别是 `kernel/turn/` 与编排层的事）。

**它与内建 `model-openai` 并存，不是取代它**（manifest 里因此没有 `overrides`）。
OpenAI 兼容层能连到 Anthropic 的中转，但 prompt caching 的断点、thinking 的四种形态与
`stop_sequence` 这个终止原因在那条路上表达不出来——本插件存在的理由就是这三样。

**它取代的是被 `D32` 删掉的 `legacy/providers/anthropic_provider.py`**，但不是移植：

- 旧实现有四张按模型名版本号 gating 的表（哪些模型只认 adaptive thinking、哪些拒绝
  `temperature`……）。**一张都没搬。** `D19` 拒过同类的 slug 表，理由不变：表只会越滚越大，
  而用户换一个新模型要等我们发版。这里改成 `thinking.mode` / `supports_temperature` /
  `effort` 三个配置项。
- 旧实现自带指数退避重试引擎。**没搬**：重试是编排层的策略（技术方案 §6.2.2），
  provider 只把 `retryable` 与 `retry_after_ms` 如实标在 `NucleaError` 上。两处都做会叠成
  一个放大器——旧实现给 SDK 传 `max_retries=0` 正是被这个坑过。
- 旧实现用 `anthropic` 官方 SDK。这里直接用 httpx，因此可以注入 `httpx.MockTransport`
  让整套用例零真实网络地跑，宿主发行版也不必再依赖那个 SDK。

**thinking 块的多轮回放在 `D45` 补上了。** `D32`–`D44` 期间它是相对旧实现的一处真实能力
回退：Anthropic 要求同一模型的多轮续写把 `thinking` 块（含 `signature`）原样回传，而契约的
`ModelMessage` 没有放 provider 私有块的槽位，因此 thinking 与工具调用**不能同时用**。
`contracts.OpaqueBlock` 补上了那个槽位（`ModelResponse.provider_blocks` →
`folding.assistant_message()` → `wire.thinking_blocks()`）。

**它只活到本轮 turn 结束**：opaque 块不进 `SessionMessage`，因此跨 turn 拿不回来。这够用——
需要回放的场景全都是同一条 turn 内的工具循环。要跨 turn 得先决定「一份加密的思考签名该不该
成为用户资产」（`SES-006` 一旦发布就是契约），那是另一个决定。

**三条如实记着的边界**，写在这里而不是留给用户发现：

- **不回放别家产出的 opaque 块**（`OpaqueBlock.owned_by`，`EDG-305`），也不回放缺
  `signature` 的 thinking 块——Anthropic 拒绝无签名的思考块，留一半比不留更糟。
- **不支持图像与文档输入。** `ModelMessage.content` 是纯字符串，契约层没有多模态位置，
  旧实现的 `_convert_image_block` 因此没有搬运源。
- **不声明任何 server tool**（web_search / code_execution 等）。它们会绕过 `ToolExecutor`，
  等于给模型开一条不受主 Turn、`TurnLimits` 与取消链约束的副作用通道。
"""

from __future__ import annotations

from typing import Final

from nucleamind.contracts import CapabilityKind
from nucleamind.sdk import (
    CapabilityDecl,
    ManifestJsonSchema,
    PluginManifest,
)

from .decode import StreamDecoder, decode_response, decode_stop_reason, decode_usage
from .faults import (
    CONTEXT_OVERFLOW_ERROR_TYPES,
    error_for_event,
    error_for_status,
    error_for_transport,
)
from .provider import AnthropicModelProvider, read_credential, setup
from .settings import (
    CACHING_KEYS,
    CAPABILITY_NAME,
    MODEL_ENTRY_KEYS,
    SECRET_NAME,
    THINKING_KEYS,
    AnthropicSettings,
    ModelEntry,
    resolve_settings,
)
from .wire import (
    CACHE_TTLS,
    EFFORT_LEVELS,
    THINKING_MODES,
    CachingSpec,
    ThinkingSpec,
    build_payload,
    decode_tool_name,
    encode_tool_name,
)

__all__ = [
    "CACHING_KEYS",
    "CAPABILITY_NAME",
    "CONTEXT_OVERFLOW_ERROR_TYPES",
    "ENTRY_PROPERTIES",
    "MODEL_ENTRY_KEYS",
    "MANIFEST",
    "SECRET_NAME",
    "THINKING_KEYS",
    "AnthropicModelProvider",
    "AnthropicSettings",
    "CachingSpec",
    "ModelEntry",
    "StreamDecoder",
    "ThinkingSpec",
    "build_payload",
    "decode_response",
    "decode_stop_reason",
    "decode_tool_name",
    "decode_usage",
    "encode_tool_name",
    "error_for_event",
    "error_for_status",
    "error_for_transport",
    "read_credential",
    "resolve_settings",
    "setup",
]

#: `models.<id>` 条目允许的键。与下面 `config_schema` 里那份由测试对照——两处都「自洽」
#: 而对不上时，一个写对了的配置会在阶段 A 被拒，且错误指向的是 schema 而不是这张表。
ENTRY_PROPERTIES: Final[ManifestJsonSchema] = {
    "context_window_tokens": {"type": "integer", "minimum": 1},
    "max_output_tokens": {"type": "integer", "minimum": 1},
    "capabilities": {"type": "array", "items": {"type": "string"}},
    "supports_temperature": {"type": "boolean"},
    "thinking": {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": sorted(THINKING_MODES)},
            "budget_tokens": {"type": "integer", "minimum": 1},
            "display": {"type": "string", "enum": ["omitted", "summarized"]},
        },
        "additionalProperties": False,
    },
    "effort": {"type": "string", "enum": sorted(EFFORT_LEVELS)},
    "prompt_caching": {
        "type": "object",
        "properties": {
            "enabled": {"type": "boolean"},
            "ttl": {"type": "string", "enum": sorted(CACHE_TTLS)},
            "breakpoints": {
                "type": "object",
                "properties": {
                    "system": {"type": "boolean"},
                    "tools": {"type": "boolean"},
                    "history": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
        },
        "additionalProperties": False,
    },
}

#: 插件配置块的 JSON Schema。它由 `D27` 的阶段 A 在**加载之前**校验一次，
#: `settings.py` 在 `setup()` 里再按语义校验一次——前者挡形状，后者挡取值之间的关系
#: （例如 `budget_tokens` 与 `max_output_tokens` 的大小）。
#:
#: 标注成 `ManifestJsonSchema` 而不是 `contracts.JsonSchema`：契约那个类型进不了
#: pydantic 模型（会 `RecursionError`），细节与另外两个被否掉的候选见
#: `sdk/manifest.py::ManifestJsonValue`。`D41` 之前这里刻意不标注，因为当时的字段
#: 类型是 pydantic 的 `JsonValue`，`dict` 值不变导致嵌套字面量怎么标都不成子类型。
CONFIG_SCHEMA: Final[ManifestJsonSchema] = {
    "type": "object",
    "properties": {
        "base_url": {"type": "string"},
        "auth": {"type": "string", "enum": ["x_api_key", "bearer", "none"]},
        "anthropic_version": {"type": "string"},
        "beta_headers": {"type": "array", "items": {"type": "string"}},
        "models": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": ENTRY_PROPERTIES,
                "additionalProperties": False,
            },
        },
        "default_context_window_tokens": {"type": "integer", "minimum": 1},
        "default_max_output_tokens": {"type": "integer", "minimum": 1},
        "capabilities": ENTRY_PROPERTIES["capabilities"],
        "supports_temperature": ENTRY_PROPERTIES["supports_temperature"],
        "thinking": ENTRY_PROPERTIES["thinking"],
        "effort": ENTRY_PROPERTIES["effort"],
        "prompt_caching": ENTRY_PROPERTIES["prompt_caching"],
        "request_timeout_ms": {"type": "integer", "minimum": 1},
        "stream_idle_timeout_ms": {"type": "integer", "minimum": 1},
    },
    "additionalProperties": False,
}

MANIFEST: Final = PluginManifest(
    id="anthropic",
    version="0.1.0",
    sdk_range=">=4.0.0,<5.0.0",
    setup="nucleamind_plugin_anthropic:setup",
    # **不写 `overrides`**：本插件与内建 `openai` 并存而不是取代它。
    # **也不写 `priority`**：默认值 100 会被原样采纳，而内建基准是 0（`D16` 记的坑）。
    capabilities=(CapabilityDecl(kind=CapabilityKind.MODEL, name=CAPABILITY_NAME),),
    config_schema=CONFIG_SCHEMA,
)
