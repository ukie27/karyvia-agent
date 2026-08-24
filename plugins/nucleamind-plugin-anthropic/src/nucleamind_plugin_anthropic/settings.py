"""`anthropic` 插件的配置读取与校验（`CFG-002`：插件只看得见自己那一块）。

职责：把 `ctx.config` 校验成一份不可变的 `AnthropicSettings`，并由它派生每个模型的
`ModelInfo`。全部校验在 `setup()` 时发生一次。
不负责：读凭据（那是 `ctx.secret("api_key")`，在 `provider.py`）、发起任何请求、
决定重试策略、把配置翻成线格式（那是 `wire.py`）。

四条决定了本模块形状的规则：

- **一张按模型名版本号 gating 的表都不留。** legacy 的 `anthropic_provider.py` 有四张
  （`_ADAPTIVE_ONLY_MIN_VERSIONS` / `_THINKING_DISABLE_MIN_VERSIONS` /
  `_SAMPLING_DEPRECATED_MODELS` 与那个解析模型名版本号的正则），`D19` 已经拒过同类的
  `max_tokens_field` slug 表，理由不变：表只会越滚越大，而用户换一个新模型要等我们发版。
  这里改成 `thinking.mode` / `supports_temperature` / `effort` 三个配置项，运维改一行即可。
- **能力声明与开关同源。** `describe()` 交出的能力集是「配置里列的基线 ∪ thinking 开着时的
  `reasoning` ∪ 缓存开着时的 `prompt_caching`」；反过来，**声明了 `reasoning` 却没开
  thinking 是 `CONFIG_INVALID`**。两个方向都判死，`MOD-005` 的「缺席即报缺失、绝不静默
  降级」才真的成立——否则一份声明得漂亮的配置会让组装器以为拿得到思考内容。
- **`describe()` 不得发网络请求**（契约写死：它在预算推导路径上）。因此模型窗口只能来自
  配置，`models` / `default_*` 因此存在。`models` 非空即视为**白名单**。
- **坏配置在加载期暴露，而不是拖到第一次 turn**。失败以 `PLUGIN_LOAD_FAILED` 落进
  `nm plugins` 的状态与启动控制台提示里，不影响实例使用其余模型提供方。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from nucleamind.contracts import (
    ErrorCode,
    JsonValue,
    ModelCapability,
    ModelInfo,
    NucleaError,
)
from nucleamind.sdk import PluginContext

from .wire import (
    CACHE_TTLS,
    EFFORT_LEVELS,
    PROVIDER_NAME,
    THINKING_BUDGET,
    THINKING_MODES,
    THINKING_OFF,
    CachingSpec,
    ThinkingSpec,
)

__all__ = [
    "ANTHROPIC_VERSION",
    "AUTH_MODES",
    "CACHING_KEYS",
    "CAPABILITY_NAME",
    "CONFIG_ANTHROPIC_VERSION_KEY",
    "CONFIG_AUTH_KEY",
    "CONFIG_BASE_URL_KEY",
    "CONFIG_BETA_HEADERS_KEY",
    "CONFIG_CACHING_KEY",
    "CONFIG_CAPABILITIES_KEY",
    "CONFIG_DEFAULT_CONTEXT_WINDOW_KEY",
    "CONFIG_DEFAULT_MAX_OUTPUT_KEY",
    "CONFIG_EFFORT_KEY",
    "CONFIG_MODELS_KEY",
    "CONFIG_REQUEST_TIMEOUT_KEY",
    "CONFIG_STREAM_IDLE_TIMEOUT_KEY",
    "CONFIG_SUPPORTS_TEMPERATURE_KEY",
    "CONFIG_THINKING_KEY",
    "DEFAULT_BASE_URL",
    "MODEL_ENTRY_KEYS",
    "PROVIDER_NAME",
    "SECRET_NAME",
    "THINKING_KEYS",
    "AnthropicSettings",
    "ModelEntry",
    "resolve_settings",
]

#: 本插件的能力名。MODEL 是 MULTI_UNIQUE，因此它在 kind 内唯一——内建占的是 `openai`。
CAPABILITY_NAME: Final = "anthropic"

#: `PROVIDER_NAME` 在 `wire.py` 里定义（`D45` 起 `OpaqueBlock.provider` 也用它），
#: 从这里原样再导出——本模块 import `wire`，反过来会成环。

#: 凭据名，**固定不可配置**。配置块和 `ctx.secret()` 必须使用同一个稳定名字。
SECRET_NAME: Final = "api_key"

DEFAULT_BASE_URL: Final = "https://api.anthropic.com/v1"

#: `anthropic-version` 头的默认值。它是**必填**头，给默认值是因为 99% 的人不该关心它；
#: 做成配置项是因为中转会钉一个不同的值。
ANTHROPIC_VERSION: Final = "2023-06-01"

#: 三种认证形态。`x_api_key` 是 Anthropic 官方的 `x-api-key:` 头，`bearer` 给 OAuth 令牌
#: 与多数中转，`none` 给本地 relay（此时**不碰** `ctx.secret()`）。
AUTH_MODES: Final[frozenset[str]] = frozenset({"x_api_key", "bearer", "none"})

CONFIG_BASE_URL_KEY: Final = "base_url"
CONFIG_AUTH_KEY: Final = "auth"
CONFIG_ANTHROPIC_VERSION_KEY: Final = "anthropic_version"
CONFIG_BETA_HEADERS_KEY: Final = "beta_headers"
CONFIG_MODELS_KEY: Final = "models"
CONFIG_DEFAULT_CONTEXT_WINDOW_KEY: Final = "default_context_window_tokens"
CONFIG_DEFAULT_MAX_OUTPUT_KEY: Final = "default_max_output_tokens"
CONFIG_CAPABILITIES_KEY: Final = "capabilities"
CONFIG_SUPPORTS_TEMPERATURE_KEY: Final = "supports_temperature"
CONFIG_THINKING_KEY: Final = "thinking"
CONFIG_EFFORT_KEY: Final = "effort"
CONFIG_CACHING_KEY: Final = "prompt_caching"
CONFIG_REQUEST_TIMEOUT_KEY: Final = "request_timeout_ms"
CONFIG_STREAM_IDLE_TIMEOUT_KEY: Final = "stream_idle_timeout_ms"

_CONTEXT_WINDOW_ENTRY_KEY: Final = "context_window_tokens"
_MAX_OUTPUT_ENTRY_KEY: Final = "max_output_tokens"

#: `models` 表里每个条目允许的键。与 manifest 的 `config_schema` 由测试对照。
MODEL_ENTRY_KEYS: Final[frozenset[str]] = frozenset(
    {
        _CONTEXT_WINDOW_ENTRY_KEY,
        _MAX_OUTPUT_ENTRY_KEY,
        CONFIG_CAPABILITIES_KEY,
        CONFIG_SUPPORTS_TEMPERATURE_KEY,
        CONFIG_THINKING_KEY,
        CONFIG_EFFORT_KEY,
        CONFIG_CACHING_KEY,
    }
)

#: `thinking` / `prompt_caching` 两个子对象允许的键。同样与 `config_schema` 对照。
THINKING_KEYS: Final[frozenset[str]] = frozenset({"mode", "budget_tokens", "display"})
CACHING_KEYS: Final[frozenset[str]] = frozenset({"enabled", "ttl", "breakpoints"})
_BREAKPOINT_KEYS: Final[frozenset[str]] = frozenset({"system", "tools", "history"})
_DISPLAY_MODES: Final[frozenset[str]] = frozenset({"omitted", "summarized"})

#: 默认窗口取 200k 而不是 1M：多数在用的 Claude 是 200k，猜大会让 `CTX-003` 生成超窗请求。
_DEFAULT_CONTEXT_WINDOW: Final = 200_000
_DEFAULT_MAX_OUTPUT: Final = 8_192
_DEFAULT_REQUEST_TIMEOUT_MS: Final = 120_000
_DEFAULT_STREAM_IDLE_TIMEOUT_MS: Final = 60_000

#: 默认声明的能力。**只列真正做到的两项**（`MOD-005`）：`reasoning` 与 `prompt_caching`
#: 由对应开关派生，图像输入与结构化输出这个实现根本没有。
_DEFAULT_CAPABILITIES: Final[frozenset[ModelCapability]] = frozenset(
    {ModelCapability.TOOL_CALLS, ModelCapability.STREAMING}
)

#: 由开关派生、因此**不许**在 `capabilities` 里直接声明的两项。
_DERIVED_CAPABILITIES: Final[Mapping[ModelCapability, str]] = {
    ModelCapability.REASONING: CONFIG_THINKING_KEY,
    ModelCapability.PROMPT_CACHING: CONFIG_CACHING_KEY,
}

_UNKNOWN_MODEL: Final = (
    "配置里列举了 models，因此它是白名单：这个模型不在其中。"
    "要接受任意模型标识，请把 models 留空。"
)
_BASE_URL_SCHEME: Final = "端点地址必须以 http:// 或 https:// 开头。"
_BASE_URL_NOT_STRING: Final = "端点地址必须是非空字符串。"
_VERSION_NOT_STRING: Final = "anthropic_version 必须是非空字符串。"
_MODELS_NOT_OBJECT: Final = "models 必须是对象。"
_MODEL_ENTRY_NOT_OBJECT: Final = "models 的每一项都必须是对象。"
_BUDGET_TOO_LARGE: Final = (
    "thinking.budget_tokens 必须小于该模型的 max_output_tokens。"
    "本实现不会替你把上限抬高——那会让生效的上限不是你配的那个。"
)
_DERIVED_CAPABILITY: Final = (
    "这项能力由对应的开关派生，不能直接写进 capabilities："
    "开关没打开就声明它，等于让组装器以为拿得到一份它拿不到的东西。"
)


def _invalid(message: str, **detail: object) -> NucleaError:
    return NucleaError(ErrorCode.CONFIG_INVALID, message, detail=detail)


def _unknown_fields(raw: Mapping[str, JsonValue], allowed: frozenset[str], *, pointer: str) -> None:
    """未知键 → `CONFIG_UNKNOWN_FIELD`。

    与 `CONFIG_INVALID` 分开是因为补救动作不同：「你多写了一个键」要删，
    「你的值写错了」要改。
    """
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise NucleaError(
            ErrorCode.CONFIG_UNKNOWN_FIELD,
            "配置里出现了未知字段。",
            detail={"pointer": pointer, "unknown": unknown, "allowed": sorted(allowed)},
        )


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


def _read_choice(
    config: Mapping[str, JsonValue], key: str, *, default: str, allowed: frozenset[str]
) -> str:
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


def _read_str_list(config: Mapping[str, JsonValue], key: str) -> tuple[str, ...]:
    value = config.get(key)
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise _invalid("该配置项必须是字符串数组。", key=key, actual_type=type(value).__name__)
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise _invalid("数组里的每一项都必须是非空字符串。", key=key)
        items.append(item.strip())
    return tuple(items)


def _read_object(
    config: Mapping[str, JsonValue], key: str, *, allowed: frozenset[str], pointer: str
) -> Mapping[str, JsonValue]:
    value = config.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise _invalid("该配置项必须是对象。", key=key, actual_type=type(value).__name__)
    _unknown_fields(value, allowed, pointer=pointer)
    return value


def _read_capabilities(
    config: Mapping[str, JsonValue], *, default: frozenset[ModelCapability]
) -> frozenset[ModelCapability]:
    """读能力基线。

    **未知能力名报错而不是忽略**——写错一个名字就静默少一项能力，用户只会看到「模型不支持
    工具调用」而找不到原因。**派生能力也报错**：见模块 docstring 第二条。
    """
    value = config.get(CONFIG_CAPABILITIES_KEY)
    if value is None:
        return default
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise _invalid(
            "能力声明必须是字符串数组。",
            key=CONFIG_CAPABILITIES_KEY,
            actual_type=type(value).__name__,
        )
    known = {item.value for item in ModelCapability}
    selected: set[ModelCapability] = set()
    for item in value:
        if not isinstance(item, str) or item not in known:
            raise _invalid(
                "能力声明里出现了未知的能力名。",
                key=CONFIG_CAPABILITIES_KEY,
                allowed=sorted(known),
                actual=item if isinstance(item, str) else type(item).__name__,
            )
        capability = ModelCapability(item)
        switch = _DERIVED_CAPABILITIES.get(capability)
        if switch is not None:
            raise _invalid(_DERIVED_CAPABILITY, key=CONFIG_CAPABILITIES_KEY, capability=item, switch=switch)
        selected.add(capability)
    return frozenset(selected)


def _read_thinking(
    config: Mapping[str, JsonValue], *, pointer: str, default: ThinkingSpec
) -> ThinkingSpec:
    raw = _read_object(config, CONFIG_THINKING_KEY, allowed=THINKING_KEYS, pointer=pointer)
    if not raw:
        return default
    mode = _read_choice(raw, "mode", default=default.mode, allowed=THINKING_MODES)
    return ThinkingSpec(
        mode=mode,
        budget_tokens=_read_positive_int(raw, "budget_tokens", default=default.budget_tokens),
        display=_read_choice(raw, "display", default=default.display, allowed=_DISPLAY_MODES)
        if "display" in raw
        else default.display,
    )


def _read_caching(
    config: Mapping[str, JsonValue], *, pointer: str, default: CachingSpec
) -> CachingSpec:
    raw = _read_object(config, CONFIG_CACHING_KEY, allowed=CACHING_KEYS, pointer=pointer)
    if not raw:
        return default
    breakpoints = _read_object(
        raw, "breakpoints", allowed=_BREAKPOINT_KEYS, pointer=f"{pointer}/breakpoints"
    )
    return CachingSpec(
        enabled=_read_bool(raw, "enabled", default=default.enabled),
        ttl=_read_choice(raw, "ttl", default=default.ttl, allowed=CACHE_TTLS)
        if "ttl" in raw
        else default.ttl,
        system=_read_bool(breakpoints, "system", default=default.system),
        tools=_read_bool(breakpoints, "tools", default=default.tools),
        history=_read_bool(breakpoints, "history", default=default.history),
    )


class ModelEntry:
    """`models` 表里的一条：一个模型的窗口、上限与线格式偏好。"""

    __slots__ = (
        "_base_capabilities",
        "_caching",
        "_context_window",
        "_effort",
        "_max_output",
        "_supports_temperature",
        "_thinking",
    )

    def __init__(
        self,
        *,
        context_window_tokens: int,
        max_output_tokens: int,
        capabilities: frozenset[ModelCapability],
        supports_temperature: bool,
        thinking: ThinkingSpec,
        caching: CachingSpec,
        effort: str,
    ) -> None:
        self._context_window = context_window_tokens
        self._max_output = max_output_tokens
        self._base_capabilities = capabilities
        self._supports_temperature = supports_temperature
        self._thinking = thinking
        self._caching = caching
        self._effort = effort

    @property
    def context_window_tokens(self) -> int:
        return self._context_window

    @property
    def max_output_tokens(self) -> int:
        return self._max_output

    @property
    def base_capabilities(self) -> frozenset[ModelCapability]:
        """配置里显式列出的基线，不含派生项。"""
        return self._base_capabilities

    @property
    def capabilities(self) -> frozenset[ModelCapability]:
        """生效能力 = 基线 ∪ 由开关派生的两项（模块 docstring 第二条）。"""
        derived: set[ModelCapability] = set(self._base_capabilities)
        if self._thinking.enabled:
            derived.add(ModelCapability.REASONING)
        if self._caching.enabled:
            derived.add(ModelCapability.PROMPT_CACHING)
        return frozenset(derived)

    @property
    def supports_temperature(self) -> bool:
        return self._supports_temperature

    @property
    def thinking(self) -> ThinkingSpec:
        return self._thinking

    @property
    def caching(self) -> CachingSpec:
        return self._caching

    @property
    def effort(self) -> str:
        return self._effort


class AnthropicSettings:
    """一份已校验的配置。构造后不可变，每次请求直接读它。

    与内建 `OpenAISettings` 同一种做法：刻意不是 dataclass，让「校验只发生一次」在类型上
    就成立——没有一个能绕过 `resolve_settings()` 的公开构造路径。
    """

    __slots__ = (
        "_anthropic_version",
        "_auth",
        "_base_url",
        "_beta_headers",
        "_default",
        "_models",
        "_request_timeout_ms",
        "_stream_idle_timeout_ms",
    )

    def __init__(
        self,
        *,
        base_url: str,
        auth: str,
        anthropic_version: str,
        beta_headers: tuple[str, ...],
        default_entry: ModelEntry,
        models: Mapping[str, ModelEntry],
        request_timeout_ms: int,
        stream_idle_timeout_ms: int,
    ) -> None:
        self._base_url = base_url
        self._auth = auth
        self._anthropic_version = anthropic_version
        self._beta_headers = beta_headers
        self._default = default_entry
        self._models = dict(models)
        self._request_timeout_ms = request_timeout_ms
        self._stream_idle_timeout_ms = stream_idle_timeout_ms

    @property
    def base_url(self) -> str:
        """端点根，**原样使用不追加后缀**，请求路径固定 `/messages`。

        MiniMax 的 `https://api.minimax.io/anthropic`、Kimi Coding 的自有前缀都靠改这一行
        就能用——legacy 那三条 `backend="anthropic"` 的 `ProviderSpec` 的全部价值就在这里，
        因此删掉它们不丢能力。
        """
        return self._base_url

    @property
    def auth(self) -> str:
        return self._auth

    @property
    def requires_credential(self) -> bool:
        return self._auth != "none"

    @property
    def anthropic_version(self) -> str:
        return self._anthropic_version

    @property
    def beta_headers(self) -> tuple[str, ...]:
        """`anthropic-beta` 头的各项。

        它是**不去维护一张 beta 特性表的出口**：需要某个 beta 的人自己填一行配置，
        而不是等我们跟着 Anthropic 发一个新版本。
        """
        return self._beta_headers

    @property
    def request_timeout_ms(self) -> int:
        return self._request_timeout_ms

    @property
    def stream_idle_timeout_ms(self) -> int:
        return self._stream_idle_timeout_ms

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
            raise NucleaError(
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


def _check_budget(entry: ModelEntry, *, pointer: str) -> None:
    """`budget_tokens` 必须留得下正文的空间。

    legacy 在这里会偷偷把 `max_tokens` 抬到 `budget + 4096`。**我们直接拒**：抬完之后
    生效的输出上限就不是用户配的那个，而「显式优于魔法」。
    """
    thinking = entry.thinking
    if thinking.mode == THINKING_BUDGET and thinking.budget_tokens >= entry.max_output_tokens:
        raise _invalid(
            _BUDGET_TOO_LARGE,
            pointer=f"{pointer}/{CONFIG_THINKING_KEY}/budget_tokens",
            budget_tokens=thinking.budget_tokens,
            max_output_tokens=entry.max_output_tokens,
        )


def _read_entry(
    raw: Mapping[str, JsonValue], *, pointer: str, fallback: ModelEntry | None
) -> ModelEntry:
    """读一条模型设置。`fallback=None` 表示读的是顶层默认条目。"""
    base = fallback
    entry = ModelEntry(
        context_window_tokens=_read_positive_int(
            raw,
            _CONTEXT_WINDOW_ENTRY_KEY if base is not None else CONFIG_DEFAULT_CONTEXT_WINDOW_KEY,
            default=base.context_window_tokens if base else _DEFAULT_CONTEXT_WINDOW,
        ),
        max_output_tokens=_read_positive_int(
            raw,
            _MAX_OUTPUT_ENTRY_KEY if base is not None else CONFIG_DEFAULT_MAX_OUTPUT_KEY,
            default=base.max_output_tokens if base else _DEFAULT_MAX_OUTPUT,
        ),
        capabilities=_read_capabilities(
            raw, default=base.base_capabilities if base else _DEFAULT_CAPABILITIES
        ),
        supports_temperature=_read_bool(
            raw,
            CONFIG_SUPPORTS_TEMPERATURE_KEY,
            default=base.supports_temperature if base else True,
        ),
        thinking=_read_thinking(
            raw,
            pointer=f"{pointer}/{CONFIG_THINKING_KEY}",
            default=base.thinking if base else ThinkingSpec(mode=THINKING_OFF),
        ),
        caching=_read_caching(
            raw,
            pointer=f"{pointer}/{CONFIG_CACHING_KEY}",
            default=base.caching if base else CachingSpec(),
        ),
        effort=_read_choice(
            raw,
            CONFIG_EFFORT_KEY,
            default=base.effort if base else "",
            allowed=EFFORT_LEVELS,
        )
        if CONFIG_EFFORT_KEY in raw
        else (base.effort if base else ""),
    )
    _check_budget(entry, pointer=pointer)
    return entry


def _read_base_url(config: Mapping[str, JsonValue]) -> str:
    value = config.get(CONFIG_BASE_URL_KEY)
    if value is None:
        return DEFAULT_BASE_URL
    if not isinstance(value, str) or not value.strip():
        raise _invalid(
            _BASE_URL_NOT_STRING,
            key=CONFIG_BASE_URL_KEY,
            actual_type=type(value).__name__,
        )
    url = value.strip()
    if not url.startswith(("http://", "https://")):
        raise _invalid(_BASE_URL_SCHEME, key=CONFIG_BASE_URL_KEY)
    return url.rstrip("/")


def _read_version(config: Mapping[str, JsonValue]) -> str:
    value = config.get(CONFIG_ANTHROPIC_VERSION_KEY)
    if value is None:
        return ANTHROPIC_VERSION
    if not isinstance(value, str) or not value.strip():
        raise _invalid(
            _VERSION_NOT_STRING,
            key=CONFIG_ANTHROPIC_VERSION_KEY,
            actual_type=type(value).__name__,
        )
    return value.strip()


def _read_models(config: Mapping[str, JsonValue], default_entry: ModelEntry) -> dict[str, ModelEntry]:
    raw_models = config.get(CONFIG_MODELS_KEY)
    if raw_models is None:
        return {}
    if not isinstance(raw_models, Mapping):
        raise _invalid(
            _MODELS_NOT_OBJECT,
            key=CONFIG_MODELS_KEY,
            actual_type=type(raw_models).__name__,
        )
    models: dict[str, ModelEntry] = {}
    for model_id, raw in raw_models.items():
        pointer = f"/{CONFIG_MODELS_KEY}/{model_id}"
        if not isinstance(raw, Mapping):
            raise _invalid(
                _MODEL_ENTRY_NOT_OBJECT,
                key=pointer,
                actual_type=type(raw).__name__,
            )
        _unknown_fields(raw, MODEL_ENTRY_KEYS, pointer=pointer)
        models[model_id] = _read_entry(raw, pointer=pointer, fallback=default_entry)
    return models


def resolve_settings(ctx: PluginContext) -> AnthropicSettings:
    """把 `ctx.config` 校验成一份设置。

    **异常约定**：类型或取值不对抛 `CONFIG_INVALID`；出现未知键抛 `CONFIG_UNKNOWN_FIELD`
    （那是「你多写了一个键」而不是「你的值写错了」，补救动作不同）。校验在 `setup()` 时
    发生一次，不拖到第一次 turn。
    """
    config = ctx.config
    default_entry = _read_entry(config, pointer="", fallback=None)
    return AnthropicSettings(
        base_url=_read_base_url(config),
        auth=_read_choice(config, CONFIG_AUTH_KEY, default="x_api_key", allowed=AUTH_MODES),
        anthropic_version=_read_version(config),
        beta_headers=_read_str_list(config, CONFIG_BETA_HEADERS_KEY),
        default_entry=default_entry,
        models=_read_models(config, default_entry),
        request_timeout_ms=_read_positive_int(
            config, CONFIG_REQUEST_TIMEOUT_KEY, default=_DEFAULT_REQUEST_TIMEOUT_MS
        ),
        stream_idle_timeout_ms=_read_positive_int(
            config, CONFIG_STREAM_IDLE_TIMEOUT_KEY, default=_DEFAULT_STREAM_IDLE_TIMEOUT_MS
        ),
    )
