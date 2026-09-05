"""`backends.py` 的纯函数用例：请求怎么拼、响应怎么读、错误怎么分类。

每个后端各写各的（`AGENTS.md` 原则 5），因此用例也逐个后端写——参数化成一张表会让
「哪个后端的哪条规则错了」变得难读。
"""

from __future__ import annotations

import json

import pytest
from karyvia_plugin_web.backends import (
    BRAVE_ENDPOINT,
    DUCKDUCKGO_ENDPOINT,
    TAVILY_ENDPOINT,
    SearchHit,
    build_request,
    check_status,
    format_hits,
    parse_response,
)
from karyvia_plugin_web.settings import CustomBackend, SearchSettings

from karyvia.contracts import ErrorCode, KaryviaError


def _settings(provider: str, **kwargs: object) -> SearchSettings:
    return SearchSettings(provider=provider, **kwargs)  # pyright: ignore[reportArgumentType]


class TestBuildRequest:
    def test_duckduckgo_posts_a_form_and_needs_no_key(self) -> None:
        request = build_request(_settings("duckduckgo"), "cats", 5, "")
        assert (request.method, request.url) == ("POST", DUCKDUCKGO_ENDPOINT)
        assert request.form == {"q": "cats", "kl": "wt-wt"}

    def test_tavily_sends_a_bearer_token(self) -> None:
        request = build_request(_settings("tavily"), "cats", 3, "k")
        assert request.url == TAVILY_ENDPOINT
        assert request.headers["Authorization"] == "Bearer k"
        assert request.json_body == {"query": "cats", "max_results": 3}

    def test_brave_sends_a_subscription_header(self) -> None:
        request = build_request(_settings("brave"), "cats", 7, "k")
        assert request.url == BRAVE_ENDPOINT
        assert request.headers["X-Subscription-Token"] == "k"
        assert request.params == {"q": "cats", "count": "7"}

    def test_searxng_asks_for_json_on_the_configured_host(self) -> None:
        request = build_request(
            _settings("searxng", base_url="https://s.example/"), "cats", 5, ""
        )
        assert request.url == "https://s.example/search"
        assert request.params["format"] == "json"

    def test_base_url_overrides_a_builtin_endpoint(self) -> None:
        """代理与自建网关的落点。内置后端的官方地址只是默认值。"""
        request = build_request(
            _settings("tavily", base_url="https://gw.example/search"), "cats", 5, "k"
        )
        assert request.url == "https://gw.example/search"

    def test_custom_substitutes_the_api_key_placeholder(self) -> None:
        """让 `Bearer {api_key}` 与裸 `{api_key}` 两种鉴权风格都不必再加配置项。"""
        settings = _settings(
            "custom",
            base_url="https://s.example/api",
            custom=CustomBackend(headers={"X-Key": "{api_key}", "A": "b"}),
        )
        request = build_request(settings, "cats", 5, "secret-value")
        assert request.headers["X-Key"] == "secret-value"
        assert request.headers["A"] == "b"

    def test_custom_get_puts_the_query_in_params(self) -> None:
        settings = _settings(
            "custom",
            base_url="https://s.example/api",
            custom=CustomBackend(method="GET", query_field="q", count_field="n"),
        )
        request = build_request(settings, "cats", 4, "")
        assert (request.method, request.params) == ("GET", {"q": "cats", "n": "4"})

    def test_custom_post_omits_the_count_field_when_unset(self) -> None:
        settings = _settings(
            "custom", base_url="https://s.example/api", custom=CustomBackend(count_field="")
        )
        request = build_request(settings, "cats", 4, "")
        assert request.json_body == {"query": "cats"}


class TestParseResponse:
    def test_tavily(self) -> None:
        body = json.dumps(
            {"results": [{"title": "T", "url": "https://a", "content": "C"}]}
        ).encode()
        assert parse_response(_settings("tavily"), body) == (
            SearchHit(title="T", url="https://a", snippet="C"),
        )

    def test_brave_reads_the_nested_web_results(self) -> None:
        body = json.dumps(
            {"web": {"results": [{"title": "T", "url": "https://a", "description": "D"}]}}
        ).encode()
        assert parse_response(_settings("brave"), body)[0].snippet == "D"

    def test_custom_digs_a_dotted_path(self) -> None:
        settings = _settings(
            "custom",
            base_url="u",
            custom=CustomBackend(
                results_path="data.items",
                title_field="name",
                url_field="link",
                snippet_field="desc",
            ),
        )
        body = json.dumps(
            {"data": {"items": [{"name": "N", "link": "https://a", "desc": "D"}]}}
        ).encode()
        assert parse_response(settings, body) == (
            SearchHit(title="N", url="https://a", snippet="D"),
        )

    def test_a_missing_path_yields_no_results_rather_than_an_error(self) -> None:
        """路径写错与「这次真的没搜到」在数据上不可区分，都折成空结果由调用方渲染。"""
        settings = _settings("custom", base_url="u", custom=CustomBackend(results_path="nope"))
        assert parse_response(settings, b'{"results": []}') == ()

    def test_entries_without_a_url_are_dropped(self) -> None:
        """这个工具的产出是给 `web.fetch` 当输入的，没有地址的结果只会占预算。"""
        body = json.dumps(
            {"results": [{"title": "T"}, {"title": "U", "url": "https://a"}]}
        ).encode()
        assert [hit.url for hit in parse_response(_settings("tavily"), body)] == ["https://a"]

    def test_long_snippets_are_clipped(self) -> None:
        body = json.dumps(
            {"results": [{"title": "T", "url": "https://a", "content": "x" * 5000}]}
        ).encode()
        assert len(parse_response(_settings("tavily"), body)[0].snippet) < 500

    def test_non_json_is_an_error_not_an_empty_result(self) -> None:
        with pytest.raises(KaryviaError) as caught:
            parse_response(_settings("tavily"), b"<html>rate limited</html>")
        assert caught.value.code is ErrorCode.EXTERNAL_HTTP_REQUEST

    def test_a_json_array_at_the_top_level_is_an_error(self) -> None:
        with pytest.raises(KaryviaError):
            parse_response(_settings("tavily"), b"[]")


class TestDuckDuckGoScraping:
    _PAGE = b"""
    <div class="result results_links">
      <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=x">
        First <b>result</b>
      </a>
      <a class="result__snippet">Snippet one.</a>
    </div>
    <div class="result">
      <a class="result__a" href="https://plain.example/b">Second</a>
      <a class="result__snippet">Snippet two.</a>
    </div>
    """

    def test_titles_urls_and_snippets(self) -> None:
        hits = parse_response(_settings("duckduckgo"), self._PAGE)
        assert [hit.title for hit in hits] == ["First result", "Second"]
        assert [hit.snippet for hit in hits] == ["Snippet one.", "Snippet two."]

    def test_the_redirect_wrapper_is_unwrapped(self) -> None:
        hits = parse_response(_settings("duckduckgo"), self._PAGE)
        assert hits[0].url == "https://example.com/a"

    def test_a_plain_href_is_left_alone(self) -> None:
        hits = parse_response(_settings("duckduckgo"), self._PAGE)
        assert hits[1].url == "https://plain.example/b"

    def test_an_unrecognised_page_yields_nothing_rather_than_crashing(self) -> None:
        """站点改版即失效——这是「默认后端不要凭据」的价格，写在 README 里。
        但失效的形态必须是「没有结果」，不能是一次异常。"""
        assert parse_response(_settings("duckduckgo"), b"<html><body>nope</body></html>") == ()


class TestCheckStatus:
    def test_2xx_passes(self) -> None:
        check_status("tavily", 204)

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_failures_point_at_the_credential(self, status: int) -> None:
        with pytest.raises(KaryviaError) as caught:
            check_status("tavily", status)
        assert caught.value.code is ErrorCode.CONFIG_SECRET_MISSING

    @pytest.mark.parametrize("status", [429, 500, 503])
    def test_transient_failures_are_retryable(self, status: int) -> None:
        with pytest.raises(KaryviaError) as caught:
            check_status("tavily", status)
        assert caught.value.retryable is True

    def test_other_4xx_is_not_retryable(self) -> None:
        with pytest.raises(KaryviaError) as caught:
            check_status("tavily", 400)
        assert caught.value.retryable is False

    def test_the_response_body_never_reaches_the_detail(self) -> None:
        """自由文本可能把回显的 API key 带出来（`D19` 的先例）。"""
        with pytest.raises(KaryviaError) as caught:
            check_status("tavily", 400)
        assert set(caught.value.detail) == {"provider", "status"}


class TestFormatHits:
    def test_empty_results_say_so(self) -> None:
        assert "没有找到" in format_hits("cats", ())

    def test_every_line_carries_its_url(self) -> None:
        """没有 URL 的摘要等于让模型自己编一个地址。"""
        text = format_hits("cats", (SearchHit("T", "https://a", "S"),))
        assert "https://a" in text and "T" in text and "S" in text

    def test_a_missing_title_is_labelled_not_blank(self) -> None:
        assert "(无标题)" in format_hits("cats", (SearchHit("", "https://a"),))
