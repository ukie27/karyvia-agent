"""内建 Model Provider `model_openai`：OpenAI 兼容 Chat Completions。

职责：作为本内建能力的公开门面，导出 `setup`（注册入口）、`OpenAIModelProvider`（实现）、
配置键常量与线格式翻译。
不负责：实现细节（在 `provider.py` / `wire.py` / `settings.py` / `faults.py`）、声明自己
（manifest 在 `builtins/registry.py`，那是内建能力唯一的发现来源）、续写与重试策略
。

**选 OpenAI 兼容协议的依据**：覆盖面最广——OpenAI、Azure、本地
vLLM / Ollama / LM Studio 与多数中转服务都兼容， 的「配置一份凭据就能用」因此
对最多用户成立；协议本身简单，工具调用语义稳定。Anthropic 原生等其余 provider 走插件。

**按  显式列出不支持项**：默认只声明 `tool_calls` 与 `streaming`。图像/音频输入、
结构化输出、扩展 thinking 与 prompt caching 都需要本实现没有的线格式支持，因此从
`capabilities` 里**缺席**——缺席即由 Kernel 报能力缺失，绝不静默降级后假装支持。
"""

from __future__ import annotations

from .faults import (
    CONTEXT_OVERFLOW_ERROR_CODES,
    QUOTA_ERROR_CODES,
    error_for_status,
    error_for_transport,
    retry_after_ms,
)
from .provider import (
    CHAT_COMPLETIONS_PATH,
    OpenAIModelProvider,
    is_local_endpoint,
    read_credential,
    setup,
)
from .settings import (
    AUTH_MODES,
    CAPABILITY_NAME,
    CONFIG_AUTH_KEY,
    CONFIG_BASE_URL_KEY,
    CONFIG_CAPABILITIES_KEY,
    CONFIG_DEFAULT_CONTEXT_WINDOW_KEY,
    CONFIG_DEFAULT_MAX_OUTPUT_KEY,
    CONFIG_INCLUDE_USAGE_KEY,
    CONFIG_MAX_TOKENS_FIELD_KEY,
    CONFIG_MODELS_KEY,
    CONFIG_REQUEST_TIMEOUT_KEY,
    CONFIG_STREAM_IDLE_TIMEOUT_KEY,
    CONFIG_SUPPORTS_TEMPERATURE_KEY,
    DEFAULT_BASE_URL,
    MODEL_ENTRY_KEYS,
    PROVIDER_NAME,
    SECRET_NAME,
    ModelEntry,
    OpenAISettings,
    resolve_settings,
)
from .wire import (
    MAX_COMPLETION_TOKENS_FIELD,
    MAX_TOKENS_FIELD,
    StreamDecoder,
    ToolCallAccumulator,
    ToolNameMap,
    build_payload,
    decode_response,
    decode_stop_reason,
    decode_usage,
    encode_messages,
    encode_tools,
    parse_sse_data,
    strip_lone_surrogates,
)

__all__ = [
    "AUTH_MODES",
    "CAPABILITY_NAME",
    "CHAT_COMPLETIONS_PATH",
    "CONFIG_AUTH_KEY",
    "CONFIG_BASE_URL_KEY",
    "CONFIG_CAPABILITIES_KEY",
    "CONFIG_DEFAULT_CONTEXT_WINDOW_KEY",
    "CONFIG_DEFAULT_MAX_OUTPUT_KEY",
    "CONFIG_INCLUDE_USAGE_KEY",
    "CONFIG_MAX_TOKENS_FIELD_KEY",
    "CONFIG_MODELS_KEY",
    "CONFIG_REQUEST_TIMEOUT_KEY",
    "CONFIG_STREAM_IDLE_TIMEOUT_KEY",
    "CONFIG_SUPPORTS_TEMPERATURE_KEY",
    "CONTEXT_OVERFLOW_ERROR_CODES",
    "DEFAULT_BASE_URL",
    "MAX_COMPLETION_TOKENS_FIELD",
    "MAX_TOKENS_FIELD",
    "MODEL_ENTRY_KEYS",
    "PROVIDER_NAME",
    "QUOTA_ERROR_CODES",
    "SECRET_NAME",
    "ModelEntry",
    "OpenAIModelProvider",
    "OpenAISettings",
    "StreamDecoder",
    "ToolCallAccumulator",
    "ToolNameMap",
    "build_payload",
    "decode_response",
    "decode_stop_reason",
    "decode_usage",
    "encode_messages",
    "encode_tools",
    "error_for_status",
    "error_for_transport",
    "is_local_endpoint",
    "parse_sse_data",
    "read_credential",
    "resolve_settings",
    "retry_after_ms",
    "setup",
    "strip_lone_surrogates",
]
