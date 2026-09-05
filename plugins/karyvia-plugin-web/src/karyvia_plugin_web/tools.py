"""两个工具的执行体。本包唯一碰 IO 的模块。

职责：`web.fetch`（抓一个网页）与 `web.search`（搜一次）的 `ToolHandler` 实现。
不负责：解析 HTML（`extract.py`）、拼搜索请求（`backends.py`）、读配置（`settings.py`）。

**两个工具走两条不同的出网路径，这是本插件最要紧的一个设计决定**，判据是
**谁决定了那个 URL**：

- `web.fetch` 的 URL **整个来自模型**，因此走 `ctx.net`——`runtime/access/net.py` 的
  SSRF 守卫（解析后逐地址判定 + 手动跟随重定向）正是为这种输入存在的（`EDG-406`）。
  本插件**不写第四份守卫**（另三份在 `runtime/access/paths.py` 一线之外的那几处）。
- `web.search` 的端点**来自运维配置**，模型只控制 query。自托管 SearXNG 常在私有网段，
  而 `ctx.net` 会按设计拒掉私有地址；因此这一条直接用 httpx，与内建 `model_openai`
  要连本地 vLLM / Ollama 是同一条先例。

**`execute()` 约定不抛**，两个类共用 `_Tool` 的那一个出口（`builtins/tools_fs/base.py`
的同一种做法）。逸出的异常会被 Kernel 记成 `side_effect=UNKNOWN`——而这两个工具都是只读的，
凭空多出一次「可能改了什么」的记录是谎报。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from karyvia.contracts import (
    CancelSignal,
    ErrorCode,
    JsonValue,
    KaryviaError,
    RiskLevel,
    SideEffect,
    ToolInvocation,
    ToolResult,
    ToolSpec,
)
from karyvia.sdk import PluginContext

from .backends import build_request, check_status, format_hits, parse_response
from .extract import MediaKind, decode_body, html_to_text, looks_binary, media_kind_of, truncate
from .settings import SECRET_NAME, WebSettings

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    import httpx

__all__ = [
    "FETCH_TOOL",
    "SEARCH_TOOL",
    "WebFetchTool",
    "WebSearchTool",
    "fetch_spec",
    "search_spec",
]

FETCH_TOOL: Final = "web.fetch"
SEARCH_TOOL: Final = "web.search"

def fetch_spec() -> ToolSpec:
    """`web.fetch` 的声明。"""
    return ToolSpec(
        name=FETCH_TOOL,
        description=(
            "抓取一个 http(s) 网页并返回纯文本正文。只读；"
            "私有网段与云元数据地址会被安全守卫拒绝。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要抓取的 http(s) 地址。"},
                "max_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "正文的字符上限，不给则用配置里的值。",
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        read_only=True,
        risk=RiskLevel.SAFE,
    )


def search_spec() -> ToolSpec:
    """`web.search` 的声明。"""
    return ToolSpec(
        name=SEARCH_TOOL,
        description="用配置好的搜索后端搜一次，返回标题、地址与摘要。只读。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索词。"},
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "最多返回几条，不给则用配置里的值。",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        read_only=True,
        risk=RiskLevel.SAFE,
    )


class _Tool:
    """两个工具的公共外壳：计时、入口取消检查与失败折叠。

    子类实现 `run()`，可以自由抛 `KaryviaError`——它会被折成 `ok=False` 的结果。
    子类**不该**自己捕获它：那样「失败是一等结果」这件事就要在两个文件里各实现一遍。
    """

    __slots__ = ("_limit",)

    def __init__(self, limit: int) -> None:
        self._limit = limit

    async def execute(self, invocation: ToolInvocation, cancel: CancelSignal) -> ToolResult:
        """**约定不抛**。**取消语义**：入口检查一次；两个工具各只有一次外部往返，
        往返本身由 `timeout_ms` 收口，中途没有可插检查点的位置。"""
        started = time.perf_counter()
        try:
            cancel.raise_if_requested()
            content, data = await self.run(invocation)
        except KaryviaError as error:
            text, cut = truncate(error.user_message, self._limit)
            return ToolResult(
                call_id=invocation.call.call_id,
                ok=False,
                content=text,
                truncated=cut,
                # 两个工具都是只读的：失败与否，外部世界都没变。
                side_effect=SideEffect.NONE,
                error=error,
                duration_ms=_elapsed_ms(started),
            )
        text, cut = truncate(content, self._limit)
        return ToolResult(
            call_id=invocation.call.call_id,
            ok=True,
            content=text,
            truncated=cut,
            side_effect=SideEffect.NONE,
            data=data,
            duration_ms=_elapsed_ms(started),
        )

    async def run(
        self, invocation: ToolInvocation
    ) -> tuple[str, Mapping[str, JsonValue] | None]:
        raise NotImplementedError


class WebFetchTool(_Tool):
    """抓一个网页，返回纯文本正文。**经 `ctx.net`**，见模块 docstring。"""

    __slots__ = ("_ctx", "_settings")

    def __init__(self, ctx: PluginContext, settings: WebSettings) -> None:
        super().__init__(settings.fetch.max_result_chars)
        self._ctx = ctx
        self._settings = settings

    async def run(
        self, invocation: ToolInvocation
    ) -> tuple[str, Mapping[str, JsonValue] | None]:
        arguments = invocation.call.arguments
        _reject_unknown(arguments, {"url", "max_chars"})
        url = _require_str(arguments, "url")
        if not url.lower().startswith(("http://", "https://")):
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                "只支持 http 与 https 地址。",
                # 原始串照放：它是模型自己给的，不含宿主机信息。
                detail={"url": url[:200]},
            )
        limit = _optional_int(arguments, "max_chars", self._settings.fetch.max_result_chars)

        settings = self._settings.fetch
        response = await self._ctx.net.request(
            "GET",
            url,
            headers={
                "User-Agent": self._settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
            },
            timeout_ms=settings.timeout_ms,
            # `D42` 之前这里没有上界，`max_bytes` 只在下面切一刀——对着一个几百 MB 的
            # URL 会先把它整个读进内存。上界现在落在**读取**上，到量即断开。
            max_bytes=settings.max_bytes,
        )
        if not 200 <= response.status < 300:
            raise KaryviaError(
                ErrorCode.EXTERNAL_HTTP_REQUEST,
                "目标站点返回了错误状态。",
                detail={"status": response.status},
                retryable=response.status == 429 or response.status >= 500,
            )

        content_type = _header(response.headers, "content-type")
        kind = media_kind_of(content_type)
        if kind == MediaKind.UNSUPPORTED:
            raise KaryviaError(
                ErrorCode.INPUT_UNSUPPORTED_MEDIA,
                "这个地址返回的不是文本或网页，无法读成正文。",
                detail={"content_type": content_type.split(";", 1)[0].strip()},
            )
        # 门面已经按 `max_bytes` 停在上界处并标了 `truncated`，这里不再切第二刀。
        body = response.body
        oversized = response.truncated
        if looks_binary(body):
            raise KaryviaError(
                ErrorCode.INPUT_UNSUPPORTED_MEDIA,
                "响应体是二进制内容，无法读成正文。",
                detail={"content_type": content_type.split(";", 1)[0].strip()},
            )

        text, lossy = decode_body(body, content_type)
        title = ""
        if kind == MediaKind.HTML:
            page = html_to_text(text)
            title, text = page.title, page.text
        body_text, cut = truncate(text, max(1, limit))
        header = "\n".join(part for part in (title, url) if part)
        data: dict[str, JsonValue] = {
            "url": url,
            "status": response.status,
            "content_type": content_type,
            "lossy_decode": lossy,
            # 三种截断分别可查：字节层的上限、字符层的上限，以及两者都没触发。
            "byte_limited": oversized,
            "char_limited": cut,
        }
        if title:
            data["title"] = title
        return f"{header}\n\n{body_text}", data


class WebSearchTool(_Tool):
    """搜一次。**直接用 httpx**（端点由运维配置），见模块 docstring。"""

    __slots__ = ("_ctx", "_settings", "_transport")

    def __init__(
        self,
        ctx: PluginContext,
        settings: WebSettings,
        *,
        transport: "httpx.AsyncBaseTransport | None" = None,
    ) -> None:
        super().__init__(settings.search.max_result_chars)
        self._ctx = ctx
        self._settings = settings
        # 可注入的传输层：测试全部走 `httpx.MockTransport`，一个 socket 都不开
        # （`plugins/…-anthropic` 的同一种做法）。
        self._transport = transport

    async def run(
        self, invocation: ToolInvocation
    ) -> tuple[str, Mapping[str, JsonValue] | None]:
        import httpx  # noqa: PLC0415 - 惰性：没人搜的实例不该为它付导入开销

        arguments = invocation.call.arguments
        _reject_unknown(arguments, {"query", "max_results"})
        query = _require_str(arguments, "query")
        settings = self._settings.search
        count = _optional_int(arguments, "max_results", settings.max_results)

        request = build_request(settings, query, count, self._credential())
        # 每次调用开一个 client 而不是长驻一个：本插件没有关闭钩子（manifest 没有 teardown
        # 字段），一个活到进程结束的连接池只能靠 GC 收，而搜索本来就是低频操作。
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=settings.timeout_ms / 1000,
            follow_redirects=True,
        ) as client:
            try:
                response = await client.request(
                    request.method,
                    request.url,
                    headers={"User-Agent": self._settings.user_agent, **request.headers},
                    params=dict(request.params) or None,
                    json=dict(request.json_body) if request.json_body is not None else None,
                    data=dict(request.form) if request.form is not None else None,
                )
            except httpx.TimeoutException as error:
                raise KaryviaError(
                    ErrorCode.TIMEOUT_HTTP_REQUEST,
                    "搜索请求超时。",
                    detail={"provider": settings.provider},
                ) from error
            except httpx.HTTPError as error:
                raise KaryviaError(
                    ErrorCode.EXTERNAL_HTTP_REQUEST,
                    "搜索请求失败。",
                    # 只放异常**类型名**，不放消息——第三方库的异常文本可能带上完整 URL，
                    # 而 `custom` 后端的 URL 里可能有 query string 形态的凭据。
                    detail={"provider": settings.provider, "cause": type(error).__name__},
                    retryable=True,
                ) from error

        check_status(settings.provider, response.status_code)
        hits = parse_response(settings, response.content)[:count]
        data: dict[str, JsonValue] = {
            "provider": settings.provider,
            "query": query,
            "count": len(hits),
            "urls": [hit.url for hit in hits],
        }
        return format_hits(query, hits), data

    def _credential(self) -> str:
        """取凭据。不需要凭据的后端返回空串。

        **每次调用都取一遍**：`ctx.secret()` 只是查一次已经在内存里的配置 + 环境变量，
        而缓存住它意味着用户改了变量要重启实例。缺失时抛出的
        `CONFIG_SECRET_MISSING` 由 `_Tool.execute` 折成这一次调用的失败——**不牵连
        `web.fetch`**，理由见 `settings.py` 的模块 docstring。
        """
        if not self._settings.search.needs_credential:
            return ""
        return self._ctx.secret(SECRET_NAME).reveal()


# ------------------------------------------------------------------------------ 参数


def _reject_unknown(arguments: Mapping[str, JsonValue], allowed: set[str]) -> None:
    """表外参数是错误而不是可忽略的多余字段。

    Kernel 的 `ToolInvoker` 已按 schema 校验过一遍（`additionalProperties: false`），
    这里再挡一次是因为 `ToolHandler` 是公开契约：`sdk.testing.ToolContract` 直接调它。
    """
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            "出现了未知参数。",
            detail={"unknown": unknown, "allowed": sorted(allowed)},
        )


def _require_str(arguments: Mapping[str, JsonValue], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            "缺少必填参数或类型不对（应为非空字符串）。",
            detail={"argument": key, "actual_type": type(value).__name__},
        )
    return value.strip()


def _optional_int(arguments: Mapping[str, JsonValue], key: str, default: int) -> int:
    value = arguments.get(key)
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            "参数类型不对或超出范围（应为正整数）。",
            detail={"argument": key, "actual_type": type(value).__name__},
        )
    return value


def _header(headers: Mapping[str, str], name: str) -> str:
    """大小写无关地取一个响应头。

    `HttpResponse.headers` 是一个普通 `Mapping`——httpx 转 dict 时给的是小写键，但那是
    httpx 的实现细节而不是 `sdk.api.HttpResponse` 的承诺。
    """
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return ""


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))
