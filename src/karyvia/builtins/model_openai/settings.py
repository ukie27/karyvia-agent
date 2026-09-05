"""`model_openai` 的配置读取与校验（`CFG-002`：插件只看得见自己那一块）。

职责：把 `ctx.config` 校验成一份不可变的 `OpenAISettings`，并由它派生每个模型的
`ModelInfo`。全部校验在 `setup()` 时发生一次。
不负责：读凭据（那是 `ctx.secret("api_key")`，在 `provider.py`）、发起任何请求、
决定重试策略。

三条决定了本模块形状的规则：

- **坏配置在加载期暴露，而不是让第一次 turn 失败**。若没有其他可用模型，Runtime
  随后的基础能力校验会拒绝启动。
- **`max_tokens_field` 与 `supports_temperature` 是配置项，不是按模型名猜的表。**
  gpt-5 / o1 / o3 / o4 只认 `max_completion_tokens` 且拒绝 `temperature`，旧实现为此维护
  了一张靠 slug 匹配、越滚越大的厂商特例表。做成配置意味着用户换一个新模型只需改一行，
  不必等我们发版——这正是「显式优于魔法」。
- **`describe()` 不得发网络请求**（契约写死：它在预算推导路径上）。因此模型窗口只能来自
  配置，`models` / `default_*` 因此存在。`models` 非空即视为**白名单**：运维一旦列举了
  自己在用的模型，一个拼错的 model_id 就该被当场指出来，而不是拿默认窗口蒙混过去。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from karyvia.contracts import (
    ErrorCode,
    JsonValue,
    KaryviaError,
    ModelCapability,
    ModelInfo,
)
from karyvia.sdk import PluginContext

from .wire import MAX_COMPLETION_TOKENS_FIELD, MAX_TOKENS_FIELD

__all__ = [
    "AUTH_MODES",
    "CAPABILITY_NAME",
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
    "DEFAULT_BASE_URL",
    "MODEL_ENTRY_KEYS",
    "PROVIDER_NAME",
    "SECRET_NAME",
    "ModelEntry",
    "OpenAISettings",
    "resolve_settings",
]

#: 本内建的能力名。MODEL 是 MULTI_UNIQUE，因此它在 kind 内唯一。
CAPABILITY_NAME: Final = "openai"

#: `ModelInfo.provider`，诊断里「这个回答是谁生成的」的答案。
PROVIDER_NAME: Final = "openai_compatible"

#: 凭据名，固定不可配置，使配置路径与 `ctx.secret()` 的调用点保持同源。
SECRET_NAME: Final = "api_key"

DEFAULT_BASE_URL: Final = "https://api.openai.com/v1"

#: 三种认证形态。`none` 是本地 vLLM / Ollama / LM Studio 的常态，
#: `api_key_header` 是 Azure OpenAI 的 `api-key:` 头。
AUTH_MODES: Final[frozenset[str]] = frozenset({"bearer", "api_key_header", "none"})

CONFIG_BASE_URL_KEY: Final = "base_url"
CONFIG_AUTH_KEY: Final = "auth"
CONFIG_MODELS_KEY: Final = "models"
CONFIG_DEFAULT_CONTEXT_WINDOW_KEY: Final = "default_context_window_tokens"
CONFIG_DEFAULT_MAX_OUTPUT_KEY: Final = "default_max_output_tokens"
CONFIG_CAPABILITIES_KEY: Final = "capabilities"
CONFIG_MAX_TOKENS_FIELD_KEY: Final = "max_tokens_field"
CONFIG_SUPPORTS_TEMPERATURE_KEY: Final = "supports_temperature"
CONFIG_REQUEST_TIMEOUT_KEY: Final = "request_timeout_ms"
CONFIG_STREAM_IDLE_TIMEOUT_KEY: Final = "stream_idle_timeout_ms"
CONFIG_INCLUDE_USAGE_KEY: Final = "include_usage"

#: `models` 条目里窗口与上限的键名。顶层是 `default_` 前缀版，条目里没有那个前缀——
#: 条目本身已经限定了「哪个模型」，再叫 default 就名不副实。
_CONTEXT_WINDOW_ENTRY_KEY: Final = "context_window_tokens"
_MAX_OUTPUT_ENTRY_KEY: Final = "max_output_tokens"

#: `models` 表里每个条目允许的键。与 manifest 的 `config_schema` 由测试对照。
MODEL_ENTRY_KEYS: Final[frozenset[str]] = frozenset(
    {
        _CONTEXT_WINDOW_ENTRY_KEY,
        _MAX_OUTPUT_ENTRY_KEY,
        CONFIG_CAPABILITIES_KEY,
        CONFIG_MAX_TOKENS_FIELD_KEY,
        CONFIG_SUPPORTS_TEMPERATURE_KEY,
    }
)

_DEFAULT_CONTEXT_WINDOW: Final = 128_000
_DEFAULT_MAX_OUTPUT: Final = 4_096
_DEFAULT_REQUEST_TIMEOUT_MS: Final = 120_000
_DEFAULT_STREAM_IDLE_TIMEOUT_MS: Final = 60_000

#: 默认声明的能力。**只列真正做到的两项**（`MOD-005`）：图像输入、结构化输出、
#: reasoning、prompt caching 都需要本实现没有的线格式支持，缺席即报缺失，不静默降级。
_DEFAULT_CAPABILITIES: Final[frozenset[ModelCapability]] = frozenset(
    {ModelCapability.TOOL_CALLS, ModelCapability.STREAMING}
)

_MAX_TOKENS_FIELDS: Final[frozenset[str]] = frozenset(
    {MAX_TOKENS_FIELD, MAX_COMPLETION_TOKENS_FIELD}
)

_UNKNOWN_MODEL: Final = (
    "配置里列举了 models，因此它是白名单：这个模型不在其中。"
    "要接受任意模型标识，请把 models 留空。"
)
_MODELS_NOT_OBJECT: Final = "models 必须是对象。"
_MODEL_ENTRY_NOT_OBJECT: Final = "models 的每一项都必须是对象。"
_BASE_URL_SCHEME: Final = "端点地址必须以 http:// 或 https:// 开头。"


def _invalid(message: str, **detail: object) -> KaryviaError:
    return KaryviaError(ErrorCode.CONFIG_INVALID, message, detail=detail)


def _read_bool(config: Mapping[str, JsonValue], key: str, *, default: bool) -> bool:
    value = config.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise _invalid("该配置项必须是布尔值。", key=key, actual_type=type(value).__name__)
    return value


def _read_positive_int(config: Mapping[str, JsonValue], key: str, *, default: int) -> int:
    value = config.get(key)
    if value is None:
        return default
    # `bool` 是 `int` 的子类，但 `"request_timeout_ms": true` 是配置写错了。
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise _invalid("该配置项必须是正整数。", key=key, actual_type=type(value).__name__)
    return value


def _read_choice(config: Mapping[str, JsonValue], key: str, *, default: str, allowed: frozenset[str]) -> str:
    value = config.get(key)
    if value is None:
        return default
    if not isinstance(value, str) or value not in allowed:
        raise _invalid(
            "该配置项的取值受限。",
            key=key,
            allowed=sorted(allowed),
            actual=value if isinstance(value, str) else type(value).__name__,
        )
    return value


def _read_capabilities(config: Mapping[str, JsonValue], key: str, *, default: frozenset[ModelCapability]) -> frozenset[ModelCapability]:
    """读能力声明。**未知能力名报错而不是忽略**——写错一个名字就静默少一项能力，
    用户只会看到「模型不支持工具调用」而找不到原因。"""
    value = config.get(key)
    if value is None:
        return default
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise _invalid("能力声明必须是字符串数组。", key=key, actual_type=type(value).__name__)
    known = {item.value for item in ModelCapability}
    names: list[str] = []
    for item in value:
        if not isinstance(item, str) or item not in known:
            raise _invalid(
                "能力声明里出现了未知的能力名。",
                key=key,
                allowed=sorted(known),
                actual=item if isinstance(item, str) else type(item).__name__,
            )
        names.append(item)
    return frozenset(ModelCapability(name) for name in names)


class ModelEntry:
    """`models` 表里的一条：一个模型的窗口、上限与线格式偏好。"""

    __slots__ = (
        "_capabilities",
        "_context_window",
        "_max_output",
        "_max_tokens_field",
        "_supports_temperature",
    )

    def __init__(
        self,
        *,
        context_window_tokens: int,
        max_output_tokens: int,
        capabilities: frozenset[ModelCapability],
        max_tokens_field: str,
        supports_temperature: bool,
    ) -> None:
        self._context_window = context_window_tokens
        self._max_output = max_output_tokens
        self._capabilities = capabilities
        self._max_tokens_field = max_tokens_field
        self._supports_temperature = supports_temperature

    @property
    def context_window_tokens(self) -> int:
        return self._context_window

    @property
    def max_output_tokens(self) -> int:
        return self._max_output

    @property
    def capabilities(self) -> frozenset[ModelCapability]:
        return self._capabilities

    @property
    def max_tokens_field(self) -> str:
        return self._max_tokens_field

    @property
    def supports_temperature(self) -> bool:
        return self._supports_temperature


class OpenAISettings:
    """一份已校验的配置。构造后不可变，每次请求直接读它。

    与 `BasicContextSettings` 同一种做法：刻意不是 dataclass，让「校验只发生一次」在类型
    上就成立——没有一个能绕过 `resolve_settings()` 的公开构造路径。
    """

    __slots__ = ("_auth", "_base_url", "_default", "_include_usage", "_models", "_request_timeout_ms", "_stream_idle_timeout_ms")

    def __init__(
        self,
        *,
        base_url: str,
        auth: str,
        default_entry: ModelEntry,
        models: Mapping[str, ModelEntry],
        request_timeout_ms: int,
        stream_idle_timeout_ms: int,
        include_usage: bool,
    ) -> None:
        self._base_url = base_url
        self._auth = auth
        self._default = default_entry
        self._models = dict(models)
        self._request_timeout_ms = request_timeout_ms
        self._stream_idle_timeout_ms = stream_idle_timeout_ms
        self._include_usage = include_usage

    @property
    def base_url(self) -> str:
        """端点根，**原样使用不追加 `/v1`**：各家后缀差异极大（OpenVINO 是 `/v3`，
        Gemini-compat 带尾斜杠，vLLM 根本没有约定），替用户拼一段只会拼错。"""
        return self._base_url

    @property
    def auth(self) -> str:
        return self._auth

    @property
    def requires_credential(self) -> bool:
        return self._auth != "none"

    @property
    def request_timeout_ms(self) -> int:
        return self._request_timeout_ms

    @property
    def stream_idle_timeout_ms(self) -> int:
        return self._stream_idle_timeout_ms

    @property
    def include_usage(self) -> bool:
        return self._include_usage

    @property
    def model_ids(self) -> frozenset[str]:
        """被显式列举的模型。为空表示不设白名单。"""
        return frozenset(self._models)

    def entry_for(self, model_id: str) -> ModelEntry:
        """取一个模型的设置。

        **异常约定**：`models` 非空且不含该模型时抛 `CAPABILITY_MISSING`
        （契约要求 `describe()` 对不存在的模型报这个类别）。
        """
        entry = self._models.get(model_id)
        if entry is not None:
            return entry
        if self._models:
            raise KaryviaError(
                ErrorCode.CAPABILITY_MISSING,
                _UNKNOWN_MODEL,
                detail={"model_id": model_id, "configured": sorted(self._models)},
            )
        return self._default

    def describe(self, model_id: str) -> ModelInfo:
        """`ModelProvider.describe()` 的实现体。纯查询，不发网络请求。"""
        entry = self.entry_for(model_id)
        return ModelInfo(
            model_id=model_id,
            provider=PROVIDER_NAME,
            capabilities=entry.capabilities,
            context_window_tokens=entry.context_window_tokens,
            max_output_tokens=entry.max_output_tokens,
        )


def _read_model_entry(raw: JsonValue, *, pointer: str, fallback: ModelEntry) -> ModelEntry:
    if not isinstance(raw, Mapping):
        raise _invalid(_MODEL_ENTRY_NOT_OBJECT, key=pointer, actual_type=type(raw).__name__)
    unknown = sorted(set(raw) - MODEL_ENTRY_KEYS)
    if unknown:
        raise KaryviaError(
            ErrorCode.CONFIG_UNKNOWN_FIELD,
            "models 条目里出现了未知字段。",
            detail={"key": pointer, "unknown": unknown, "allowed": sorted(MODEL_ENTRY_KEYS)},
        )
    return ModelEntry(
        context_window_tokens=_read_positive_int(
            raw, _CONTEXT_WINDOW_ENTRY_KEY, default=fallback.context_window_tokens
        ),
        max_output_tokens=_read_positive_int(
            raw, _MAX_OUTPUT_ENTRY_KEY, default=fallback.max_output_tokens
        ),
        capabilities=_read_capabilities(
            raw, CONFIG_CAPABILITIES_KEY, default=fallback.capabilities
        ),
        max_tokens_field=_read_choice(
            raw,
            CONFIG_MAX_TOKENS_FIELD_KEY,
            default=fallback.max_tokens_field,
            allowed=_MAX_TOKENS_FIELDS,
        ),
        supports_temperature=_read_bool(
            raw, CONFIG_SUPPORTS_TEMPERATURE_KEY, default=fallback.supports_temperature
        ),
    )


def _read_base_url(config: Mapping[str, JsonValue]) -> str:
    value = config.get(CONFIG_BASE_URL_KEY)
    if value is None:
        return DEFAULT_BASE_URL
    if not isinstance(value, str) or not value.strip():
        raise _invalid(
            "端点地址必须是非空字符串。",
            key=CONFIG_BASE_URL_KEY,
            actual_type=type(value).__name__,
        )
    url = value.strip()
    if not url.startswith(("http://", "https://")):
        raise _invalid(_BASE_URL_SCHEME, key=CONFIG_BASE_URL_KEY)
    return url.rstrip("/")


def resolve_settings(ctx: PluginContext) -> OpenAISettings:
    """把 `ctx.config` 校验成一份设置。

    **异常约定**：类型或取值不对抛 `CONFIG_INVALID`；`models` 条目里出现未知字段抛
    `CONFIG_UNKNOWN_FIELD`（那是「你多写了一个键」而不是「你的值写错了」，补救动作不同）。
    校验在 `setup()` 时发生一次，不拖到第一次 turn。
    """
    config = ctx.config
    default_entry = ModelEntry(
        context_window_tokens=_read_positive_int(
            config, CONFIG_DEFAULT_CONTEXT_WINDOW_KEY, default=_DEFAULT_CONTEXT_WINDOW
        ),
        max_output_tokens=_read_positive_int(
            config, CONFIG_DEFAULT_MAX_OUTPUT_KEY, default=_DEFAULT_MAX_OUTPUT
        ),
        capabilities=_read_capabilities(
            config, CONFIG_CAPABILITIES_KEY, default=_DEFAULT_CAPABILITIES
        ),
        max_tokens_field=_read_choice(
            config, CONFIG_MAX_TOKENS_FIELD_KEY, default=MAX_TOKENS_FIELD, allowed=_MAX_TOKENS_FIELDS
        ),
        supports_temperature=_read_bool(config, CONFIG_SUPPORTS_TEMPERATURE_KEY, default=True),
    )

    raw_models = config.get(CONFIG_MODELS_KEY)
    if raw_models is None:
        models: dict[str, ModelEntry] = {}
    elif isinstance(raw_models, Mapping):
        models = {
            model_id: _read_model_entry(
                raw, pointer=f"{CONFIG_MODELS_KEY}.{model_id}", fallback=default_entry
            )
            for model_id, raw in raw_models.items()
        }
    else:
        raise _invalid(
            _MODELS_NOT_OBJECT,
            key=CONFIG_MODELS_KEY,
            actual_type=type(raw_models).__name__,
        )

    return OpenAISettings(
        base_url=_read_base_url(config),
        auth=_read_choice(config, CONFIG_AUTH_KEY, default="bearer", allowed=AUTH_MODES),
        default_entry=default_entry,
        models=models,
        request_timeout_ms=_read_positive_int(
            config, CONFIG_REQUEST_TIMEOUT_KEY, default=_DEFAULT_REQUEST_TIMEOUT_MS
        ),
        stream_idle_timeout_ms=_read_positive_int(
            config, CONFIG_STREAM_IDLE_TIMEOUT_KEY, default=_DEFAULT_STREAM_IDLE_TIMEOUT_MS
        ),
        include_usage=_read_bool(config, CONFIG_INCLUDE_USAGE_KEY, default=False),
    )
