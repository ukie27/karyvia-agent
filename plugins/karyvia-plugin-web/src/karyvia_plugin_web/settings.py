"""配置解析与一次性校验（`CFG-002`：插件只看得见自己那一块）。

职责：把 `plugins.web.config` 变成两个不可变设置对象，并在 `setup()` 里把能查的错一次查完。
不负责：读取凭据（`tools.py`，见下）、发请求、决定后端怎么拼包（`backends.py`）。

**形状校验在 `setup()` 做完，不拖到第一次 turn**（内建 `context_basic` 的先例）：一份写错
的配置应当在 `karyvia plugins list` 里看得见，而不是等到模型第一次调工具时才变成一条工具失败。

**但凭据刻意不在这里取。** `web.fetch` 与 `web.search` 是两条独立的工具能力，而
`plugins.web` 是一个提供方——在 `setup()` 里因为缺一个搜索凭据而抛错，会把
`web.fetch` 一起带走（`PLUGIN_LOAD_FAILED` 是提供方级的）。因此凭据在**第一次调用
`web.search` 时**才解析，缺失折成那一次调用的 `CONFIG_SECRET_MISSING`。代价如实记着：
配置里少一个 `api_key` 不会在启动时报出来，只会在第一次搜索时报。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from karyvia.contracts import ErrorCode, JsonValue, KaryviaError

__all__ = [
    "CREDENTIALLESS_PROVIDERS",
    "DEFAULT_MAX_RESULTS",
    "DEFAULT_PROVIDER",
    "DEFAULT_USER_AGENT",
    "PROVIDERS",
    "SECRET_NAME",
    "CustomBackend",
    "FetchSettings",
    "SearchSettings",
    "WebSettings",
    "resolve_settings",
]

#: 凭据名固定不可配置，使配置路径与 `ctx.secret()` 调用保持同源。
SECRET_NAME: Final = "api_key"

#: 支持的搜索后端。`custom` 是可配置的通用 JSON 后端——它的存在正是为了**不**再长出一张
#: 逐家写死的表（`D19` 拒过 `max_tokens_field` slug 表、`D32` 拒过四张版本 gating 表）。
PROVIDERS: Final[tuple[str, ...]] = ("duckduckgo", "tavily", "brave", "searxng", "custom")

#: 不需要凭据的后端。默认后端必须在这张表里——外部插件用不上 `runtime/bootstrap.py` 的
#: `keep` 声明过滤（那张 `_ENABLED_NAMES` 按内建 id 索引），因此 manifest 声明的两条能力
#: 恒被注册；默认开箱可用，才不会出现「声明了却不可用」（`D20` 明确拒过的那种）。
CREDENTIALLESS_PROVIDERS: Final[frozenset[str]] = frozenset({"duckduckgo", "searxng"})

#: 需要 `base_url` 的后端：自托管或完全自定义，没有可以内置的默认端点。
_BASE_URL_REQUIRED: Final[frozenset[str]] = frozenset({"searxng", "custom"})

DEFAULT_PROVIDER: Final = "duckduckgo"
DEFAULT_MAX_RESULTS: Final = 5

#: 不伪装成浏览器版本号：一个会随时间过期的 UA 只会在若干个月后开始被拒。
DEFAULT_USER_AGENT: Final = "Karyvia-web-plugin/0.1 (+https://github.com/)"

_DEFAULT_FETCH_TIMEOUT_MS: Final = 30_000
_DEFAULT_SEARCH_TIMEOUT_MS: Final = 15_000
_DEFAULT_MAX_BYTES: Final = 2_000_000
_DEFAULT_MAX_RESULT_CHARS: Final = 30_000

#: 契约的 `MAX_TOOL_RESULT_LENGTH` 是硬上限，配置超过它一律拒绝——放行只会让每次调用都在
#: 构造 `ToolResult` 时才炸，那时错误指向的是 kernel 而不是这行配置（`D20` 的先例）。
_MAX_RESULT_CHARS_CEILING: Final = 100_000

# 错误消息定义成模块常量而不是写在 `raise` 处：消息是稳定文案，动态部分一律进 `detail`
# （`builtins/model_openai/settings.py::_BASE_URL_SCHEME` 的先例，`ruff` 的 `TRY003`
# 也是这么要求的）。
_NOT_AN_OBJECT: Final = "这个配置项必须是对象。"
_NOT_A_STRING: Final = "这个配置项必须是字符串。"
_NOT_A_POSITIVE_INT: Final = "这个配置项必须是正整数。"
_UNKNOWN_PROVIDER: Final = "未知的搜索后端。"
_BASE_URL_REQUIRED_MESSAGE: Final = "这个后端没有可以内置的默认端点，必须配置 base_url。"
_HEADERS_NOT_AN_OBJECT: Final = "headers 必须是对象。"
_HEADER_VALUE_NOT_A_STRING: Final = "headers 的每个值都必须是字符串。"
_BAD_CUSTOM_METHOD: Final = "custom 后端只支持 GET 与 POST。"
_RESULT_CHARS_TOO_LARGE: Final = "max_result_chars 超过了契约的工具结果上限。"


@dataclass(frozen=True, slots=True)
class FetchSettings:
    """`web.fetch` 的设置。"""

    max_bytes: int = _DEFAULT_MAX_BYTES
    max_result_chars: int = _DEFAULT_MAX_RESULT_CHARS
    timeout_ms: int = _DEFAULT_FETCH_TIMEOUT_MS


@dataclass(frozen=True, slots=True)
class CustomBackend:
    """`provider="custom"` 时怎么拼请求、怎么读响应。

    字段路径用**点分串**（`data.results`）而不是 JSON Pointer：后者要处理 `~0`/`~1`
    转义，而这里的键是用户手写的搜索 API 字段名，点分串更接近他读文档时看到的样子。
    """

    method: str = "POST"
    query_field: str = "query"
    count_field: str = ""
    headers: Mapping[str, str] = field(default_factory=dict[str, str])
    results_path: str = "results"
    title_field: str = "title"
    url_field: str = "url"
    snippet_field: str = "content"


@dataclass(frozen=True, slots=True)
class SearchSettings:
    """`web.search` 的设置。"""

    provider: str = DEFAULT_PROVIDER
    base_url: str = ""
    max_results: int = DEFAULT_MAX_RESULTS
    timeout_ms: int = _DEFAULT_SEARCH_TIMEOUT_MS
    max_result_chars: int = _DEFAULT_MAX_RESULT_CHARS
    custom: CustomBackend = field(default_factory=CustomBackend)

    @property
    def needs_credential(self) -> bool:
        """本后端是否需要 `api_key`。`custom` 认作需要——它多半指向一个要鉴权的端点。"""
        return self.provider not in CREDENTIALLESS_PROVIDERS


@dataclass(frozen=True, slots=True)
class WebSettings:
    """本插件的全部设置。"""

    fetch: FetchSettings
    search: SearchSettings
    user_agent: str = DEFAULT_USER_AGENT


def resolve_settings(config: Mapping[str, JsonValue]) -> WebSettings:
    """解析 `plugins.web.config`。**异常约定**：任何问题一律 `CONFIG_INVALID` 并带键路径。

    manifest 的 `config_schema` 已经在阶段 A 校验过**形状**（类型、未知键、数值下界），
    这里做的是它表达不了的那些：取值受限的枚举给出「你可以写哪几个」、跨字段依赖
    （`searxng` / `custom` 必须给 `base_url`）、以及上界。
    """
    fetch = _fetch_settings(_section(config, "fetch"))
    search = _search_settings(_section(config, "search"))
    user_agent = _string(config, "user_agent", DEFAULT_USER_AGENT)
    return WebSettings(fetch=fetch, search=search, user_agent=user_agent)


def _section(config: Mapping[str, JsonValue], key: str) -> Mapping[str, JsonValue]:
    value = config.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise _invalid(_NOT_AN_OBJECT, key)
    return value


def _fetch_settings(block: Mapping[str, JsonValue]) -> FetchSettings:
    return FetchSettings(
        max_bytes=_positive_int(block, "max_bytes", _DEFAULT_MAX_BYTES, prefix="fetch"),
        max_result_chars=_result_chars(block, prefix="fetch"),
        timeout_ms=_positive_int(block, "timeout_ms", _DEFAULT_FETCH_TIMEOUT_MS, prefix="fetch"),
    )


def _search_settings(block: Mapping[str, JsonValue]) -> SearchSettings:
    provider = _string(block, "provider", DEFAULT_PROVIDER, prefix="search").strip().lower()
    if provider not in PROVIDERS:
        raise _invalid(
            _UNKNOWN_PROVIDER,
            "search.provider",
            provider=provider,
            choices=list(PROVIDERS),
        )
    base_url = _string(block, "base_url", "", prefix="search").strip()
    if provider in _BASE_URL_REQUIRED and not base_url:
        raise _invalid(
            _BASE_URL_REQUIRED_MESSAGE,
            "search.base_url",
            provider=provider,
        )
    return SearchSettings(
        provider=provider,
        base_url=base_url,
        max_results=_positive_int(block, "max_results", DEFAULT_MAX_RESULTS, prefix="search"),
        timeout_ms=_positive_int(block, "timeout_ms", _DEFAULT_SEARCH_TIMEOUT_MS, prefix="search"),
        max_result_chars=_result_chars(block, prefix="search"),
        custom=_custom_backend(_section(block, "custom")),
    )


def _custom_backend(block: Mapping[str, JsonValue]) -> CustomBackend:
    defaults = CustomBackend()
    method = _string(block, "method", defaults.method, prefix="search.custom").strip().upper()
    if method not in {"GET", "POST"}:
        raise _invalid(_BAD_CUSTOM_METHOD, "search.custom.method", method=method)
    headers_value = block.get("headers")
    headers: dict[str, str] = {}
    if headers_value is not None:
        if not isinstance(headers_value, Mapping):
            raise _invalid(_HEADERS_NOT_AN_OBJECT, "search.custom.headers")
        for name, value in headers_value.items():
            if not isinstance(value, str):
                raise _invalid(
                    _HEADER_VALUE_NOT_A_STRING, f"search.custom.headers.{name}"
                )
            headers[name] = value
    return CustomBackend(
        method=method,
        query_field=_string(block, "query_field", defaults.query_field, prefix="search.custom"),
        count_field=_string(block, "count_field", defaults.count_field, prefix="search.custom"),
        headers=headers,
        results_path=_string(
            block, "results_path", defaults.results_path, prefix="search.custom"
        ),
        title_field=_string(block, "title_field", defaults.title_field, prefix="search.custom"),
        url_field=_string(block, "url_field", defaults.url_field, prefix="search.custom"),
        snippet_field=_string(
            block, "snippet_field", defaults.snippet_field, prefix="search.custom"
        ),
    )


def _result_chars(block: Mapping[str, JsonValue], *, prefix: str) -> int:
    value = _positive_int(block, "max_result_chars", _DEFAULT_MAX_RESULT_CHARS, prefix=prefix)
    if value > _MAX_RESULT_CHARS_CEILING:
        raise _invalid(
            _RESULT_CHARS_TOO_LARGE,
            f"{prefix}.max_result_chars",
            value=value,
            ceiling=_MAX_RESULT_CHARS_CEILING,
        )
    return value


def _string(
    block: Mapping[str, JsonValue], key: str, default: str, *, prefix: str = ""
) -> str:
    value = block.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise _invalid(_NOT_A_STRING, _path(prefix, key))
    return value


def _positive_int(
    block: Mapping[str, JsonValue], key: str, default: int, *, prefix: str
) -> int:
    value = block.get(key)
    if value is None:
        return default
    # `True` 是 `int` 的实例，放行它会让 `"timeout_ms": true` 变成 1 毫秒。
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise _invalid(_NOT_A_POSITIVE_INT, _path(prefix, key))
    return value


def _path(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else key


def _invalid(message: str, key: str, **detail: object) -> KaryviaError:
    return KaryviaError(
        ErrorCode.CONFIG_INVALID, message, detail={"key": f"plugins.web.config.{key}", **detail}
    )
