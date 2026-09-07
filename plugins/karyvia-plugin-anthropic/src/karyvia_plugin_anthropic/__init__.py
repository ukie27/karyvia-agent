"""官方 Anthropic 插件：Anthropic Messages API 的原生 Model Provider。

职责：把 `ModelRequest` 编码为 `/v1/messages` 请求，并把普通响应与 SSE 解码为 Karyvia
模型契约。不负责：Turn 执行、Context 裁剪、重试或故障转移。

该 Provider 与内建 OpenAI 兼容 Provider 并存，提供原生 prompt caching、thinking 块和
`stop_sequence` 语义。模型差异通过显式配置表达，不维护按模型名称猜测的版本表；HTTP 使用
可注入 transport，Provider 本身不重试。thinking 块以 `OpaqueBlock` 在同一 Turn 的工具循环
中原样回传，但不写入 Session。其他 Provider 的 opaque 块、缺少签名的 thinking 块、图像与
文档输入以及 server tools 均不受支持。
"""

from __future__ import annotations

from typing import Final

from karyvia.contracts import CapabilityKind
from karyvia.sdk import (
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
#: 而对不上时，一个写对了的配置会在加载前校验 被拒，且错误指向的是 schema 而不是这张表。
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

#: 插件配置块的 JSON Schema。它由的加载前校验 在**加载之前**校验一次，
#: `settings.py` 在 `setup()` 里再按语义校验一次——前者挡形状，后者挡取值之间的关系
#: （例如 `budget_tokens` 与 `max_output_tokens` 的大小）。
#:
#: 标注成 `ManifestJsonSchema` 而不是 `contracts.JsonSchema`：契约那个类型进不了
#: pydantic 模型（会 `RecursionError`），细节与另外两个被否掉的候选见
#: `sdk/manifest.py::ManifestJsonValue`。这里必须显式标注，因为字段
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
    sdk_range=">=5.0.0,<6.0.0",
    setup="karyvia_plugin_anthropic:setup",
    # **不写 `overrides`**：本插件与内建 `openai` 并存而不是取代它。
    # **也不写 `priority`**：默认值 100 会被原样采纳，而内建基准是 0。
    capabilities=(CapabilityDecl(kind=CapabilityKind.MODEL, name=CAPABILITY_NAME),),
    config_schema=CONFIG_SCHEMA,
)
