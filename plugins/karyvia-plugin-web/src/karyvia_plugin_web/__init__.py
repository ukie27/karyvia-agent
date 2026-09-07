"""官方 Web 插件：提供 `web.fetch` 与 `web.search` 工具。

职责：执行有界网页抓取与搜索，并把响应规范化为 `ToolResult`。
不负责：决定何时调用工具、Kernel 参数校验或 Context 组装。

四个内建搜索后端覆盖不同协议形态，其余服务通过 `custom` 后端接入。凭据缺失会明确报错，
不会静默切换后端。模型决定 URL 的 `web.fetch` 使用带 SSRF 守卫的 `ctx.net`；运维决定端点
的 `web.search` 使用插件自己的客户端，以支持私有网络中的搜索服务。抓取内容始终作为
`UNTRUSTED` 数据进入模型；这是来源隔离，不是内容审查。读取达到 `max_bytes` 时立即停止并
通过 `HttpResponse.truncated` 标记截断。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from karyvia.contracts import CapabilityKind
from karyvia.sdk import (
    CapabilityDecl,
    KaryviaAPI,
    ManifestJsonSchema,
    PluginContext,
    PluginManifest,
)

from .backends import (
    BRAVE_ENDPOINT,
    DUCKDUCKGO_ENDPOINT,
    TAVILY_ENDPOINT,
    SearchHit,
    SearchRequest,
    build_request,
    check_status,
    format_hits,
    parse_response,
)
from .extract import (
    MediaKind,
    Page,
    decode_body,
    html_to_text,
    looks_binary,
    media_kind_of,
    truncate,
)
from .settings import (
    CREDENTIALLESS_PROVIDERS,
    DEFAULT_MAX_RESULTS,
    DEFAULT_PROVIDER,
    DEFAULT_USER_AGENT,
    PROVIDERS,
    SECRET_NAME,
    CustomBackend,
    FetchSettings,
    SearchSettings,
    WebSettings,
    resolve_settings,
)
from .tools import (
    FETCH_TOOL,
    SEARCH_TOOL,
    WebFetchTool,
    WebSearchTool,
    fetch_spec,
    search_spec,
)

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    import httpx

__all__ = [
    "BRAVE_ENDPOINT",
    "CONFIG_SCHEMA",
    "CREDENTIALLESS_PROVIDERS",
    "DEFAULT_MAX_RESULTS",
    "DEFAULT_PROVIDER",
    "DEFAULT_USER_AGENT",
    "DUCKDUCKGO_ENDPOINT",
    "FETCH_TOOL",
    "MANIFEST",
    "PROVIDERS",
    "SEARCH_TOOL",
    "SECRET_NAME",
    "TAVILY_ENDPOINT",
    "CustomBackend",
    "FetchSettings",
    "MediaKind",
    "Page",
    "SearchHit",
    "SearchRequest",
    "SearchSettings",
    "WebFetchTool",
    "WebSearchTool",
    "WebSettings",
    "build_request",
    "check_status",
    "decode_body",
    "fetch_spec",
    "format_hits",
    "html_to_text",
    "looks_binary",
    "media_kind_of",
    "parse_response",
    "register",
    "resolve_settings",
    "search_spec",
    "setup",
    "truncate",
]

#: `plugins.web.config` 的形状。加载前校验 用它校验（`kernel/plugins/loader.py`），
#: `settings.py` 再做它表达不了的那些（枚举可选值、跨字段依赖、上界）。
#: 标注成 `ManifestJsonSchema` 而不是 `contracts.JsonSchema`：契约那个类型进不了
#: pydantic 模型（会 `RecursionError`），细节见 `sdk/manifest.py::ManifestJsonValue`。
CONFIG_SCHEMA: Final[ManifestJsonSchema] = {
    "type": "object",
    "properties": {
        "user_agent": {"type": "string", "description": "两个工具共用的 User-Agent。"},
        "fetch": {
            "type": "object",
            "properties": {
                "max_bytes": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "响应体读取上限（字节），超出部分丢弃。",
                },
                "max_result_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "返回给模型的字符上限。",
                },
                "timeout_ms": {"type": "integer", "minimum": 1},
            },
            "additionalProperties": False,
        },
        "search": {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "enum": list(PROVIDERS),
                    "description": "搜索后端。searxng 与 custom 必须同时配 base_url。",
                },
                "base_url": {
                    "type": "string",
                    "description": "自托管或自定义端点。留空时内置后端用各自的官方地址。",
                },
                "max_results": {"type": "integer", "minimum": 1},
                "timeout_ms": {"type": "integer", "minimum": 1},
                "max_result_chars": {"type": "integer", "minimum": 1},
                "custom": {
                    "type": "object",
                    "description": "provider=custom 时怎么拼请求、怎么读响应。",
                    "properties": {
                        "method": {"type": "string", "enum": ["GET", "POST"]},
                        "query_field": {"type": "string"},
                        "count_field": {"type": "string"},
                        "headers": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                            "description": "值里的 {api_key} 会被替换成配置的凭据。",
                        },
                        "results_path": {
                            "type": "string",
                            "description": "结果数组的点分路径，如 data.results。",
                        },
                        "title_field": {"type": "string"},
                        "url_field": {"type": "string"},
                        "snippet_field": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
}

MANIFEST: Final = PluginManifest(
    id="web",
    version="0.1.0",
    sdk_range=">=5.0.0,<6.0.0",
    setup="karyvia_plugin_web:setup",
    capabilities=(
        CapabilityDecl(kind=CapabilityKind.TOOL, name=FETCH_TOOL),
        CapabilityDecl(kind=CapabilityKind.TOOL, name=SEARCH_TOOL),
    ),
    config_schema=CONFIG_SCHEMA,
)


def register(
    api: KaryviaAPI,
    ctx: PluginContext,
    *,
    transport: "httpx.AsyncBaseTransport | None" = None,
) -> WebSettings:
    """真正的注册体。`transport` 只有测试会传（`web.search` 的 httpx 传输层替身）。

    与 `setup()` 分开是为了让用例能在不构造整个装配根的情况下驱动它，同时保证
    生产路径与测试路径**注册的是同一组对象**。
    """
    settings = resolve_settings(ctx.config)
    api.register_tool(fetch_spec(), WebFetchTool(ctx, settings))
    api.register_tool(search_spec(), WebSearchTool(ctx, settings, transport=transport))
    return settings


def setup(api: KaryviaAPI) -> None:
    """注册入口。manifest 的 `setup` 字段指向它。

    **配置在这里一次校验完**（`resolve_settings` 会抛 `CONFIG_INVALID`）：一份写错的配置
    应当在 `karyvia plugins list` 里以 `PLUGIN_LOAD_FAILED` 看得见，而不是等到模型第一次调工具
    时才变成一条工具失败。**凭据不在这里取**，理由见 `settings.py` 的模块 docstring。

    **在返回前完成全部注册**：注册先进暂存批次，`setup` 正常返回才一次性并入 registry；
    中途抛异常则整批丢弃。
    """
    register(api, api.ctx)
