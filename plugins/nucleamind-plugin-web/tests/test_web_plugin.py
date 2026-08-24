"""`web` 插件的行为用例：两个工具、注册面、以及 SDK 的契约基类。

`web.fetch` 走一个实现 `HttpAccess` 的替身，`web.search` 走 `httpx.MockTransport`——
两条路都一个 socket 都不开（`conftest.py` 的 autouse 夹具是那句话的可执行断言）。
"""

from __future__ import annotations

import json

import httpx
import pytest
from _web_fakes import StubNet, WebContext, html_response, invocation
from nucleamind_plugin_web import (
    CONFIG_SCHEMA,
    FETCH_TOOL,
    MANIFEST,
    SEARCH_TOOL,
    WebFetchTool,
    WebSearchTool,
    fetch_spec,
    register,
    resolve_settings,
    search_spec,
)

from nucleamind.contracts import (
    CapabilityKind,
    ErrorCode,
    NucleaError,
    SideEffect,
    ToolHandler,
    ToolSpec,
    TrustLevel,
)
from nucleamind.sdk import HttpResponse
from nucleamind.sdk.testing import ManualCancel, ToolContract

_PAGE = b"<html><head><title>Doc</title></head><body><p>hello world</p></body></html>"


def _fetch_tool(
    net: StubNet, *, config: dict[str, object] | None = None
) -> tuple[WebFetchTool, WebContext]:
    ctx = WebContext(net, config=config or {})  # pyright: ignore[reportArgumentType]
    return WebFetchTool(ctx, resolve_settings(ctx.config)), ctx


async def _run_fetch(net: StubNet, **arguments: object) -> object:
    tool, _ = _fetch_tool(net)
    return await tool.execute(
        invocation(FETCH_TOOL, arguments),  # pyright: ignore[reportArgumentType]
        ManualCancel(),
    )


class TestFetch:
    async def test_a_page_comes_back_as_plain_text(self) -> None:
        net = StubNet([html_response(_PAGE)])
        result = await _run_fetch(net, url="https://example.com/doc")
        assert result.ok is True
        assert "hello world" in result.content
        assert "<p>" not in result.content

    async def test_the_title_and_source_lead_the_content(self) -> None:
        """标题与来源在正文最前面，模型据此判断这段东西是从哪儿来的。"""
        net = StubNet([html_response(_PAGE)])
        result = await _run_fetch(net, url="https://example.com/doc")
        head = result.content.splitlines()
        assert head[0] == "Doc"
        assert head[1] == "https://example.com/doc"

    async def test_the_fetched_body_is_declared_untrusted(self) -> None:
        """`D42` 起隔离由契约层完成，插件的责任只剩「如实表态」。

        原来这里断言的是一行自己加的横幅（`UNTRUSTED_BANNER`，已删），而那**是提醒不是
        隔离**——一段写着「忽略以上指令」的网页照样原样进模型。现在
        `fold_tool_result` 会把它包成带来源标注的数据块，包裹本身由
        `tests/kernel/test_folding.py` 验，这里只验这个插件确实标了 `UNTRUSTED`。
        """
        net = StubNet([html_response(_PAGE)])
        result = await _run_fetch(net, url="https://example.com/doc")
        assert result.trust is TrustLevel.UNTRUSTED

    async def test_it_goes_through_the_guarded_facade(self) -> None:
        """SSRF 守卫的判定在 `runtime/access/net.py`，本插件对它的全部依赖就是「调它」。"""
        net = StubNet([html_response(_PAGE)])
        await _run_fetch(net, url="https://example.com/doc")
        assert len(net.requests) == 1
        assert net.requests[0].method == "GET"
        assert "User-Agent" in net.requests[0].headers

    async def test_a_guard_denial_becomes_a_failed_result_not_an_exception(self) -> None:
        denied = NucleaError(ErrorCode.PERMISSION_DENIED, "目标地址被守卫拒绝。")
        result = await _run_fetch(StubNet(error=denied), url="http://127.0.0.1/admin")
        assert result.ok is False
        assert result.error is not None
        assert result.error.code is ErrorCode.PERMISSION_DENIED
        assert result.side_effect is SideEffect.NONE

    @pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x/y", "javascript:alert(1)"])
    async def test_non_http_schemes_never_reach_the_network(self, url: str) -> None:
        net = StubNet()
        result = await _run_fetch(net, url=url)
        assert result.ok is False
        assert net.requests == []

    async def test_a_non_2xx_status_is_a_failure(self) -> None:
        net = StubNet([html_response(b"nope", status=404)])
        result = await _run_fetch(net, url="https://example.com/missing")
        assert result.ok is False
        assert result.error is not None
        assert result.error.detail["status"] == 404

    @pytest.mark.parametrize("status", [429, 503])
    async def test_transient_statuses_are_marked_retryable(self, status: int) -> None:
        net = StubNet([html_response(b"", status=status)])
        result = await _run_fetch(net, url="https://example.com/x")
        assert result.error is not None
        assert result.error.retryable is True

    async def test_a_pdf_is_refused_rather_than_shown_as_garbage(self) -> None:
        net = StubNet([html_response(b"%PDF-1.7", content_type="application/pdf")])
        result = await _run_fetch(net, url="https://example.com/a.pdf")
        assert result.ok is False
        assert result.error is not None
        assert result.error.code is ErrorCode.INPUT_UNSUPPORTED_MEDIA

    async def test_a_binary_body_claiming_to_be_text_is_still_refused(self) -> None:
        """`Content-Type` 是站点说的，NUL 字节是事实。"""
        net = StubNet([html_response(b"a\x00b", content_type="text/plain")])
        result = await _run_fetch(net, url="https://example.com/x")
        assert result.ok is False
        assert result.error is not None
        assert result.error.code is ErrorCode.INPUT_UNSUPPORTED_MEDIA

    async def test_plain_text_skips_html_extraction(self) -> None:
        net = StubNet([html_response(b"<not html>", content_type="text/plain")])
        result = await _run_fetch(net, url="https://example.com/x")
        assert "<not html>" in result.content

    async def test_the_byte_cap_is_reported_in_data(self) -> None:
        tool, _ = _fetch_tool(
            StubNet([html_response(b"<p>" + b"x" * 5000 + b"</p>")]),
            config={"fetch": {"max_bytes": 100}},
        )
        result = await tool.execute(invocation(FETCH_TOOL, {"url": "https://e.com"}), ManualCancel())
        assert result.data is not None
        assert result.data["byte_limited"] is True

    async def test_max_chars_narrows_the_body(self) -> None:
        net = StubNet([html_response(b"<p>" + b"y" * 5000 + b"</p>")])
        result = await _run_fetch(net, url="https://example.com/x", max_chars=200)
        assert result.data is not None
        assert result.data["char_limited"] is True

    async def test_data_carries_the_diagnosis_fields(self) -> None:
        net = StubNet([html_response(_PAGE)])
        result = await _run_fetch(net, url="https://example.com/doc")
        assert result.data is not None
        assert result.data["status"] == 200
        assert result.data["title"] == "Doc"
        assert result.data["lossy_decode"] is False

    @pytest.mark.parametrize(
        "arguments",
        [{}, {"url": ""}, {"url": 5}, {"url": "https://e.com", "depth": 2},
         {"url": "https://e.com", "max_chars": 0}],
    )
    async def test_bad_arguments_come_back_as_results(self, arguments: dict[str, object]) -> None:
        result = await _run_fetch(StubNet(), **arguments)
        assert result.ok is False
        assert result.error is not None

    async def test_a_resource_service_failure_fails_the_call_not_the_process(self) -> None:
        denied = NucleaError(ErrorCode.PERMISSION_DENIED, "资源服务拒绝了请求。")
        ctx = WebContext(StubNet(error=denied))
        tool = WebFetchTool(ctx, resolve_settings({}))
        result = await tool.execute(invocation(FETCH_TOOL, {"url": "https://e.com"}), ManualCancel())
        assert result.ok is False
        assert result.error is not None
        assert result.error.code is ErrorCode.PERMISSION_DENIED

    async def test_cancellation_before_the_request_leaves_the_network_untouched(self) -> None:
        net = StubNet()
        tool, _ = _fetch_tool(net)
        cancel = ManualCancel()
        cancel.request()
        result = await tool.execute(invocation(FETCH_TOOL, {"url": "https://e.com"}), cancel)
        assert result.ok is False
        assert net.requests == []


def _search_tool(
    handler: object, *, config: dict[str, object] | None = None, secrets: dict[str, str] | None = None
) -> WebSearchTool:
    ctx = WebContext(
        config=config or {},  # pyright: ignore[reportArgumentType]
        secrets=secrets,
    )
    transport = httpx.MockTransport(handler)  # pyright: ignore[reportArgumentType]
    return WebSearchTool(ctx, resolve_settings(ctx.config), transport=transport)


def _tavily_config() -> dict[str, object]:
    return {"search": {"provider": "tavily"}}


def _json_handler(payload: object, status: int = 200):
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=payload)

    handle.seen = seen  # pyright: ignore[reportFunctionMemberAccess]
    return handle


class TestSearch:
    async def test_results_are_rendered_with_urls(self) -> None:
        handler = _json_handler(
            {"results": [{"title": "T", "url": "https://a", "content": "C"}]}
        )
        tool = _search_tool(handler, config=_tavily_config(), secrets={"api_key": "k"})
        result = await tool.execute(invocation(SEARCH_TOOL, {"query": "cats"}), ManualCancel())
        assert result.ok is True
        assert "https://a" in result.content
        assert result.data is not None
        # `data` 过 `normalize_metadata()`：列表在那里被冻结成元组，这是契约行为而不是巧合。
        assert result.data["urls"] == ("https://a",)

    async def test_the_credential_reaches_the_backend(self) -> None:
        handler = _json_handler({"results": []})
        tool = _search_tool(handler, config=_tavily_config(), secrets={"api_key": "k"})
        await tool.execute(invocation(SEARCH_TOOL, {"query": "cats"}), ManualCancel())
        assert handler.seen[0].headers["authorization"] == "Bearer k"  # pyright: ignore[reportFunctionMemberAccess]

    async def test_a_credentialless_backend_never_asks_for_a_secret(self) -> None:
        """默认后端不需要 `api_key`，因此连 `ctx.secret()` 都不该被调到——上下文里
        资源服务拒绝请求时，工具应返回失败结果而不是让异常逃出。"""
        def handle(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(200, text="<html></html>")

        ctx = WebContext()
        tool = WebSearchTool(
            ctx, resolve_settings({}), transport=httpx.MockTransport(handle)
        )
        result = await tool.execute(invocation(SEARCH_TOOL, {"query": "cats"}), ManualCancel())
        assert result.ok is True

    async def test_a_missing_credential_fails_only_this_call(self) -> None:
        """`PLUGIN_LOAD_FAILED` 是提供方级的，因此凭据不在 `setup()` 里取——缺一个
        搜索凭据不该把 `web.fetch` 一起带走。"""
        handler = _json_handler({"results": []})
        tool = _search_tool(handler, config=_tavily_config())
        result = await tool.execute(invocation(SEARCH_TOOL, {"query": "cats"}), ManualCancel())
        assert result.ok is False
        assert result.error is not None
        assert result.error.code is ErrorCode.CONFIG_SECRET_MISSING

        fetch = await _run_fetch(StubNet([html_response(_PAGE)]), url="https://e.com")
        assert fetch.ok is True

    async def test_max_results_caps_the_rendered_list(self) -> None:
        handler = _json_handler(
            {"results": [{"title": f"T{i}", "url": f"https://a/{i}"} for i in range(10)]}
        )
        tool = _search_tool(handler, config=_tavily_config(), secrets={"api_key": "k"})
        result = await tool.execute(
            invocation(SEARCH_TOOL, {"query": "cats", "max_results": 2}), ManualCancel()
        )
        assert result.data is not None
        assert result.data["count"] == 2

    async def test_an_upstream_error_status_is_classified(self) -> None:
        handler = _json_handler({}, status=429)
        tool = _search_tool(handler, config=_tavily_config(), secrets={"api_key": "k"})
        result = await tool.execute(invocation(SEARCH_TOOL, {"query": "cats"}), ManualCancel())
        assert result.error is not None
        assert result.error.retryable is True

    async def test_a_timeout_is_its_own_error_code(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        tool = _search_tool(handle, config=_tavily_config(), secrets={"api_key": "k"})
        result = await tool.execute(invocation(SEARCH_TOOL, {"query": "cats"}), ManualCancel())
        assert result.error is not None
        assert result.error.code is ErrorCode.TIMEOUT_HTTP_REQUEST

    async def test_a_transport_error_reports_only_the_exception_type(self) -> None:
        """第三方库的异常文本可能带上完整 URL，而 `custom` 后端的 URL 里可能有凭据。"""
        def handle(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connect to https://user:sk-secret@host failed", request=request)

        tool = _search_tool(handle, config=_tavily_config(), secrets={"api_key": "k"})
        result = await tool.execute(invocation(SEARCH_TOOL, {"query": "cats"}), ManualCancel())
        assert result.error is not None
        assert result.error.detail["cause"] == "ConnectError"
        assert "sk-secret" not in json.dumps(dict(result.error.detail))
        assert "sk-secret" not in repr(result.error)

    @pytest.mark.parametrize("arguments", [{}, {"query": "  "}, {"query": "x", "site": "a"}])
    async def test_bad_arguments_come_back_as_results(self, arguments: dict[str, object]) -> None:
        tool = _search_tool(_json_handler({"results": []}))
        result = await tool.execute(
            invocation(SEARCH_TOOL, arguments),  # pyright: ignore[reportArgumentType]
            ManualCancel(),
        )
        assert result.ok is False


class TestManifest:
    def test_it_declares_exactly_the_two_tools_it_registers(self) -> None:
        declared = {(decl.kind, decl.name) for decl in MANIFEST.capabilities}
        assert declared == {
            (CapabilityKind.TOOL, FETCH_TOOL),
            (CapabilityKind.TOOL, SEARCH_TOOL),
        }

    def test_it_does_not_declare_a_priority(self) -> None:
        """写了就会被原样采纳；插件基准值由 `base_priority_for()` 给（`D17` 的先例）。"""
        for decl in MANIFEST.capabilities:
            assert "priority" not in decl.model_fields_set

    def test_the_config_schema_forbids_unknown_keys(self) -> None:
        assert CONFIG_SCHEMA["additionalProperties"] is False

    def test_the_config_schema_matches_what_settings_accepts(self) -> None:
        """两处漂移的表现是「schema 放行了但 `resolve_settings` 不认」，反之亦然。"""
        import jsonschema

        jsonschema.Draft202012Validator.check_schema(CONFIG_SCHEMA)
        sample = {
            "user_agent": "x",
            "fetch": {"max_bytes": 10, "max_result_chars": 100, "timeout_ms": 1000},
            "search": {
                "provider": "custom",
                "base_url": "https://s.example",
                "max_results": 3,
                "timeout_ms": 1000,
                "max_result_chars": 100,
                "custom": {
                    "method": "GET",
                    "query_field": "q",
                    "count_field": "n",
                    "headers": {"A": "b"},
                    "results_path": "a.b",
                    "title_field": "t",
                    "url_field": "u",
                    "snippet_field": "s",
                },
            },
        }
        jsonschema.validate(sample, CONFIG_SCHEMA)
        assert resolve_settings(sample).search.custom.method == "GET"  # pyright: ignore[reportArgumentType]


class _RecordingApi:
    """只记录注册动作的最小 `NucleaAPI` 替身。"""

    def __init__(self, ctx: WebContext) -> None:
        self._ctx = ctx
        self.tools: list[tuple[ToolSpec, ToolHandler]] = []

    @property
    def ctx(self) -> WebContext:
        return self._ctx

    def register_tool(self, spec: ToolSpec, handler: ToolHandler) -> None:
        self.tools.append((spec, handler))


class TestRegistration:
    def test_register_covers_every_declaration(self) -> None:
        """声明与注册必须逐条相等，否则 `CapabilityHost.finish()` 会以
        `PLUGIN_LOAD_FAILED` 挡下——那个报错是对的。"""
        ctx = WebContext()
        api = _RecordingApi(ctx)
        register(api, ctx)  # pyright: ignore[reportArgumentType]
        registered = {spec.name for spec, _ in api.tools}
        assert registered == {decl.name for decl in MANIFEST.capabilities}

    def test_a_broken_config_stops_registration_entirely(self) -> None:
        """`setup` 中途抛异常整批丢弃（`EDG-103`）——半注册状态不存在。"""
        ctx = WebContext(config={"search": {"provider": "bing"}})
        api = _RecordingApi(ctx)
        with pytest.raises(NucleaError) as caught:
            register(api, ctx)  # pyright: ignore[reportArgumentType]
        assert caught.value.code is ErrorCode.CONFIG_INVALID

    def test_both_tools_are_read_only_and_safe(self) -> None:
        for spec in (fetch_spec(), search_spec()):
            assert spec.read_only is True


class TestFetchToolContract(ToolContract):
    """SDK 的通用工具契约。基类**不 import pytest**，只是普通类 + `assert`。"""

    def make_tool(self) -> tuple[ToolSpec, ToolHandler]:
        tool, _ = _fetch_tool(StubNet([html_response(_PAGE)]))
        return fetch_spec(), tool

    def valid_arguments(self) -> dict[str, object]:  # pyright: ignore[reportIncompatibleMethodOverride]
        return {"url": "https://example.com/doc"}

    def invalid_arguments(self) -> dict[str, object]:  # pyright: ignore[reportIncompatibleMethodOverride]
        return {"url": 42}


class TestSearchToolContract(ToolContract):
    def make_tool(self) -> tuple[ToolSpec, ToolHandler]:
        def handle(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(200, text="<html></html>")

        ctx = WebContext()
        return search_spec(), WebSearchTool(
            ctx, resolve_settings({}), transport=httpx.MockTransport(handle)
        )

    def valid_arguments(self) -> dict[str, object]:  # pyright: ignore[reportIncompatibleMethodOverride]
        return {"query": "cats"}

    def invalid_arguments(self) -> dict[str, object]:  # pyright: ignore[reportIncompatibleMethodOverride]
        return {"query": ""}


def test_the_http_response_shape_is_the_sdk_one() -> None:
    """替身回的必须是 `sdk.api.HttpResponse` 本身，不是一个长得像它的东西。"""
    assert isinstance(html_response(b""), HttpResponse)
