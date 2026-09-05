"""搜索后端的线格式：怎么拼请求、怎么读响应。全是纯函数。

职责：把「一次搜索」翻译成一个 `SearchRequest`，再把响应字节翻回一组 `SearchHit`。
不负责：发请求（`tools.py`）、决定用哪个后端（`settings.py`）。

**每个后端各写各的，允许重复**（`AGENTS.md` 原则 5）。为消除四段相似的 `parse_*` 而引入
一个「响应形状描述器」基类，只会让加第五个后端时先去读那个基类——而 `custom` 已经是那个
可配置形态了，两者并存反而说明了分界线在哪：**形状固定的写死，形状不定的走 `custom`**。

**错误映射按语义分类**（`MOD-003` 的同一种做法）：429 与 5xx 可重试、401/403 不可重试且
指向凭据、其余非 2xx 不可重试。`detail` 里**只放状态码与后端名，不放响应正文**——那段
自由文本可能把回显的 API key 带出来（`D19` 的先例）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Final, cast
from urllib.parse import parse_qs, urlsplit

from karyvia.contracts import ErrorCode, JsonValue, KaryviaError

from .settings import SearchSettings

__all__ = [
    "BRAVE_ENDPOINT",
    "DUCKDUCKGO_ENDPOINT",
    "TAVILY_ENDPOINT",
    "SearchHit",
    "SearchRequest",
    "build_request",
    "check_status",
    "format_hits",
    "parse_response",
]

DUCKDUCKGO_ENDPOINT: Final = "https://html.duckduckgo.com/html/"
TAVILY_ENDPOINT: Final = "https://api.tavily.com/search"
BRAVE_ENDPOINT: Final = "https://api.search.brave.com/res/v1/web/search"

#: 单条摘要的字符上限。一条几 KB 的摘要能把 5 条结果撑到超预算，而它的信息量并不随长度
#: 增长——模型要的是「值不值得 fetch」。
_SNIPPET_LIMIT: Final = 400


@dataclass(frozen=True, slots=True)
class SearchRequest:
    """一次搜索请求的完整描述。`tools.py` 照着它发，不做任何补充决定。"""

    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict[str, str])
    params: Mapping[str, str] = field(default_factory=dict[str, str])
    json_body: Mapping[str, JsonValue] | None = None
    form: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class SearchHit:
    """一条搜索结果。三个字段是全部后端的公共交集。"""

    title: str
    url: str
    snippet: str = ""


def build_request(
    settings: SearchSettings, query: str, count: int, api_key: str
) -> SearchRequest:
    """按后端拼出请求。`api_key` 为空串表示没有凭据（不需要凭据的后端不看它）。"""
    provider = settings.provider
    if provider == "duckduckgo":
        return SearchRequest(
            method="POST", url=DUCKDUCKGO_ENDPOINT, form={"q": query, "kl": "wt-wt"}
        )
    if provider == "tavily":
        return SearchRequest(
            method="POST",
            url=settings.base_url or TAVILY_ENDPOINT,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json_body={"query": query, "max_results": count},
        )
    if provider == "brave":
        return SearchRequest(
            method="GET",
            url=settings.base_url or BRAVE_ENDPOINT,
            headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
            params={"q": query, "count": str(count)},
        )
    if provider == "searxng":
        return SearchRequest(
            method="GET",
            url=f"{settings.base_url.rstrip('/')}/search",
            headers={"Accept": "application/json"},
            params={"q": query, "format": "json"},
        )
    return _custom_request(settings, query, count, api_key)


def parse_response(settings: SearchSettings, body: bytes) -> tuple[SearchHit, ...]:
    """把响应体翻成结果。

    **异常约定**：响应读不懂抛 `EXTERNAL_HTTP_REQUEST`（不可重试——同样的请求还会得到
    同样读不懂的东西）。空结果集**不是**错误，返回空元组由调用方渲染成「没有结果」。
    """
    provider = settings.provider
    if provider == "duckduckgo":
        return _parse_duckduckgo(body)
    payload = _json_object(body, provider)
    if provider == "tavily":
        return _hits_from(payload.get("results"), "title", "url", "content")
    if provider == "brave":
        web = payload.get("web")
        results = web.get("results") if isinstance(web, Mapping) else None
        return _hits_from(results, "title", "url", "description")
    if provider == "searxng":
        return _hits_from(payload.get("results"), "title", "url", "content")
    backend = settings.custom
    return _hits_from(
        _dig(payload, backend.results_path),
        backend.title_field,
        backend.url_field,
        backend.snippet_field,
    )


def check_status(provider: str, status: int) -> None:
    """非 2xx 折成 `KaryviaError`。**异常约定**：只抛，不返回。"""
    if 200 <= status < 300:
        return
    detail = {"provider": provider, "status": status}
    if status in {401, 403}:
        raise KaryviaError(
            ErrorCode.CONFIG_SECRET_MISSING,
            "搜索后端拒绝了凭据（未配置或已失效）。",
            detail=detail,
        )
    if status == 429 or status >= 500:
        raise KaryviaError(
            ErrorCode.EXTERNAL_HTTP_REQUEST,
            "搜索后端暂时不可用（限速或服务端故障）。",
            detail=detail,
            retryable=True,
        )
    raise KaryviaError(
        ErrorCode.EXTERNAL_HTTP_REQUEST, "搜索后端返回了错误。", detail=detail
    )


def format_hits(query: str, hits: Sequence[SearchHit]) -> str:
    """渲染给模型看的纯文本。

    **带上每条的 URL**：这个工具的产出主要是给 `web.fetch` 当输入的，没有 URL 的摘要
    等于让模型自己编一个地址。
    """
    if not hits:
        return f"没有找到与「{query}」相关的结果。"
    lines = [f"「{query}」的搜索结果（{len(hits)} 条）："]
    for index, hit in enumerate(hits, 1):
        lines.append(f"\n{index}. {hit.title or '(无标题)'}\n   {hit.url}")
        if hit.snippet:
            lines.append(f"   {hit.snippet}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------- custom


def _custom_request(
    settings: SearchSettings, query: str, count: int, api_key: str
) -> SearchRequest:
    backend = settings.custom
    headers = {
        # `{api_key}` 是 headers 值里唯一被替换的占位符：让用户既能写 `Bearer {api_key}`
        # 也能写 `{api_key}`，而不必为每种鉴权风格再加一个配置项。
        name: value.replace("{api_key}", api_key)
        for name, value in backend.headers.items()
    }
    if backend.method == "GET":
        params = {backend.query_field: query}
        if backend.count_field:
            params[backend.count_field] = str(count)
        return SearchRequest(
            method="GET", url=settings.base_url, headers=headers, params=params
        )
    body: dict[str, JsonValue] = {backend.query_field: query}
    if backend.count_field:
        body[backend.count_field] = count
    headers.setdefault("Content-Type", "application/json")
    return SearchRequest(
        method="POST", url=settings.base_url, headers=headers, json_body=body
    )


# ------------------------------------------------------------------------ duckduckgo


class _DuckDuckGoParser(HTMLParser):
    """抽 `html.duckduckgo.com/html/` 的结果块。

    **这是 HTML 抓取，不是 API**：站点改版即失效。选它作默认后端是因为它不需要凭据，
    而「装上插件就能搜」比「多一个必配项」更接近 `BAS-001`。代价写在 README 里。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hits: list[SearchHit] = []
        self._title = ""
        self._href = ""
        self._snippet = ""
        self._mode = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        classes = (dict(attrs).get("class") or "").split()
        if "result__a" in classes:
            self._flush()
            self._mode = "title"
            self._href = _unwrap_redirect(dict(attrs).get("href") or "")
        elif "result__snippet" in classes:
            self._mode = "snippet"

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._mode = ""

    def handle_data(self, data: str) -> None:
        if self._mode == "title":
            self._title += data
        elif self._mode == "snippet":
            self._snippet += data

    def close(self) -> None:
        super().close()
        self._flush()

    def _flush(self) -> None:
        if self._href:
            self.hits.append(
                SearchHit(
                    title=" ".join(self._title.split()),
                    url=self._href,
                    snippet=_clip(" ".join(self._snippet.split())),
                )
            )
        self._title = self._href = self._snippet = ""


def _parse_duckduckgo(body: bytes) -> tuple[SearchHit, ...]:
    parser = _DuckDuckGoParser()
    parser.feed(body.decode("utf-8", errors="replace"))
    parser.close()
    return tuple(parser.hits)


def _unwrap_redirect(href: str) -> str:
    """把 `//duckduckgo.com/l/?uddg=<encoded>` 还原成真实地址。

    还原不了就**原样返回**：一个跳转链接仍然能用，而丢掉它等于丢掉一条结果。
    """
    if "/l/" not in href:
        return f"https:{href}" if href.startswith("//") else href
    target = parse_qs(urlsplit(href).query).get("uddg")
    return target[0] if target else href


# ------------------------------------------------------------------------------ 共用


def _json_object(body: bytes, provider: str) -> Mapping[str, JsonValue]:
    try:
        payload: object = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError as error:
        raise KaryviaError(
            ErrorCode.EXTERNAL_HTTP_REQUEST,
            "搜索后端返回的不是 JSON。",
            detail={"provider": provider},
        ) from error
    if not isinstance(payload, Mapping):
        raise KaryviaError(
            ErrorCode.EXTERNAL_HTTP_REQUEST,
            "搜索后端返回的 JSON 顶层不是对象。",
            detail={"provider": provider},
        )
    # boundary: `json.loads` 交回的是 `object`，上面两条 isinstance 已经把它收窄到映射；
    # 值侧的形状由 `_hits_from` / `_dig` 逐个判定，不在这里一次性断言。
    items = cast("Mapping[object, object]", payload).items()
    return {str(key): _as_json(value) for key, value in items}


def _as_json(value: object) -> JsonValue:
    """把 `json.loads` 的产物标成 `JsonValue`。

    运行时不做深校验——`json.loads` 的输出**按定义**就是 `JsonValue`，再遍历一遍只是为了
    让类型检查器满意，代价是一次与数据量成正比的无用拷贝。
    """
    return value  # pyright: ignore[reportReturnType]


def _dig(payload: Mapping[str, JsonValue], path: str) -> JsonValue | None:
    """按点分路径取值。任一段取不到即返回 `None`。"""
    current: JsonValue | None = payload
    for segment in path.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(segment)
    return current


def _hits_from(
    results: JsonValue | None, title_key: str, url_key: str, snippet_key: str
) -> tuple[SearchHit, ...]:
    """把一个结果数组翻成 `SearchHit`。

    **没有 URL 的条目丢弃**：这个工具的产出是给 `web.fetch` 当输入的，一条没有地址的
    结果对下一步毫无用处，留着只会占预算。
    """
    if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
        return ()
    hits: list[SearchHit] = []
    for item in results:
        if not isinstance(item, Mapping):
            continue
        url = item.get(url_key)
        if not isinstance(url, str) or not url:
            continue
        hits.append(
            SearchHit(
                title=_text(item.get(title_key)),
                url=url,
                snippet=_clip(_text(item.get(snippet_key))),
            )
        )
    return tuple(hits)


def _text(value: JsonValue | None) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _clip(text: str) -> str:
    return text if len(text) <= _SNIPPET_LIMIT else text[:_SNIPPET_LIMIT] + "…"
