"""HTML 与文本的纯函数：媒体类型判定、解码、正文抽取、截断。

职责：把一次 HTTP 响应的字节变成「能给模型看的一段文本」。
不负责：发请求（`tools.py`）、决定上限取值（`settings.py`）、构造 `ToolResult`。

**本模块一个 IO 都不做**，因此每条规则都能在纯函数上逐字节钉住，不需要事件循环。
这是与 `builtins/model_openai/wire.py`、`plugins/…-anthropic/wire.py` 同一条切分线。

三条决定了本模块形状的规则：

- **二进制不解码**（`EDG-205` 的同一条判据）。含 NUL 字节即整份拒绝：给模型一段乱码不会
  让它停下来，只会让它据此继续猜。
- **编码只认 `Content-Type` 里的 charset**，不做 `<meta charset>` 嗅探、不做统计式猜测。
  猜错编码产出的是一整页看似正常的错字，比 `errors="replace"` 留下的 `�` 更难被发现。
  拿不到 charset 时按 UTF-8 + `errors="replace"` 解码并如实标注有损。
- **抽正文而不是渲染页面**。`script` / `style` 一类节点整段丢弃，块级标签变成换行，
  其余只留文字。这不是浏览器，表格布局与 JS 渲染出来的内容抓不到——如实写在 README 里。

`truncate()` 是 `builtins/tools_fs/content.py::truncate` 的**第二份实现**（`R4` 禁止插件
import `builtins/`）。算法逐字相同，由 `test_web_extract.py` 的一条对照用例钉住上限语义：
**标记算在上限内，返回值长度恒 ≤ limit**。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Final

__all__ = [
    "BINARY_SNIFF_BYTES",
    "REPLACEMENT_CHAR",
    "MediaKind",
    "Page",
    "charset_of",
    "decode_body",
    "html_to_text",
    "looks_binary",
    "media_kind_of",
    "truncate",
]

#: 判定二进制时嗅探的前缀长度。整份扫描对一个几十 MB 的响应没有意义——真正的二进制
#: 格式几乎都在头部就有 NUL。
BINARY_SNIFF_BYTES: Final = 8192

#: `errors="replace"` 产出的字符。测试断言它出现，而不是断言「解码没抛异常」。
REPLACEMENT_CHAR: Final = "�"

#: 截断标记。`{shown}` / `{total}` 是字符数，不是字节数——模型消费的是字符。
_TRUNCATION_MARKER: Final = "\n… [truncated: 已显示 {shown}/{total} 字符]"

#: 整段丢弃的节点。它们的文本对「这页在说什么」没有贡献，却能轻易占满整个预算。
_DROPPED_TAGS: Final[frozenset[str]] = frozenset(
    {"script", "style", "noscript", "template", "svg", "canvas", "head"}
)

#: 产生换行的块级标签。行内标签（`span` / `a` / `em` …）不在此列，否则一句话会被拆碎。
_BLOCK_TAGS: Final[frozenset[str]] = frozenset(
    {
        "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
        "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
        "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
        "table", "td", "th", "tr", "ul",
    }
)  # fmt: skip

_HTML_TYPES: Final[frozenset[str]] = frozenset({"text/html", "application/xhtml+xml"})

#: 按 `text/*` 一律当文本会把 `text/csv` 之类也放进来，那是刻意的：它们确实能读。
#: 但 `application/*` 只白名单几种——`application/octet-stream` 当文本读没有意义。
_TEXT_TYPES: Final[frozenset[str]] = frozenset(
    {"application/json", "application/xml", "application/javascript", "application/x-ndjson"}
)

_WHITESPACE_RUN: Final = re.compile(r"[ \t\f\v]+")
_BLANK_LINES: Final = re.compile(r"\n{3,}")


class MediaKind:
    """`Content-Type` 的三分类。刻意是常量而不是枚举——它只在本包内流转。"""

    HTML: Final = "html"
    TEXT: Final = "text"
    UNSUPPORTED: Final = "unsupported"


class Page:
    """一次抽取的产物：标题与正文。"""

    __slots__ = ("text", "title")

    def __init__(self, title: str, text: str) -> None:
        self.title = title
        self.text = text


def media_kind_of(content_type: str) -> str:
    """按 `Content-Type` 判 HTML / 文本 / 不支持。

    没有 `Content-Type` 时按**不支持**处理而不是猜成 HTML：一个不声明类型的端点更可能是
    在传二进制，而猜错的代价是把一段字节流塞进模型上下文。
    """
    essence = content_type.split(";", 1)[0].strip().lower()
    if not essence:
        return MediaKind.UNSUPPORTED
    if essence in _HTML_TYPES:
        return MediaKind.HTML
    if essence.startswith("text/") or essence in _TEXT_TYPES:
        return MediaKind.TEXT
    # `application/rss+xml`、`image/svg+xml` 之类：后缀说了算。
    if essence.endswith("+json") or essence.endswith("+xml"):
        return MediaKind.TEXT
    return MediaKind.UNSUPPORTED


def charset_of(content_type: str) -> str:
    """取 `Content-Type` 里的 charset，没有则返回空串。"""
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.strip().lower() == "charset":
            return value.strip().strip('"').lower()
    return ""


def looks_binary(data: bytes) -> bool:
    """含 NUL 字节即判为二进制。只看前 `BINARY_SNIFF_BYTES` 字节。"""
    return b"\x00" in data[:BINARY_SNIFF_BYTES]


def decode_body(data: bytes, content_type: str) -> tuple[str, bool]:
    """解码响应体，返回 `(文本, 是否有损)`。

    调用方应当**先**用 `looks_binary()` 挡掉二进制：本函数不做那个判断，它只负责
    「已经确定要当文本读了，怎么读」（与 `tools_fs/content.py::decode_text` 同一条分工）。

    声明的 charset 认不出来时**退回 UTF-8 而不是抛错**：一个写错 charset 的站点仍然值得
    读，而退回的结果是可见的（`�`）。
    """
    encoding = charset_of(content_type) or "utf-8"
    try:
        return _normalize_newlines(data.decode(encoding)), False
    except LookupError:
        # 声明了一个 Python 不认识的 charset。**先按 UTF-8 严格试一次**：绝大多数这类站点
        # 实际上就是 UTF-8，直接标成有损会让一份完好的正文带上「可能有乱码」的假警报。
        pass
    except UnicodeDecodeError:
        return _normalize_newlines(data.decode("utf-8", errors="replace")), True
    try:
        return _normalize_newlines(data.decode("utf-8")), False
    except UnicodeDecodeError:
        return _normalize_newlines(data.decode("utf-8", errors="replace")), True


def html_to_text(markup: str) -> Page:
    """抽出标题与正文。

    **不是浏览器**：JS 渲染出来的内容、`<table>` 的视觉布局、CSS 隐藏的节点都拿不到或
    分不清。这条如实写在这里与 README 里，不要改成「已提取正文」这种说法。
    """
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    return Page(title=_collapse(parser.title), text=_tidy(parser.chunks))


def truncate(text: str, limit: int) -> tuple[str, bool]:
    """收进 `limit` 字符内，返回 `(文本, 是否截断)`。

    **标记算在上限里**：返回值的长度恒 ≤ `limit`。先截到上限再拼一行标记会让结果比上限
    长，而上限的下游是契约的 `MAX_TOOL_RESULT_LENGTH`——那里超一个字符就构造失败。
    """
    total = len(text)
    if total <= limit:
        return text, False
    # 先按最坏情况（`shown` 等于上限，位数最多）算出能留多少字符，再用真实的 `shown`
    # 渲染标记。后者只会更短，因此 `keep + len(marker) <= limit` 恒成立。
    keep = limit - len(_TRUNCATION_MARKER.format(shown=limit, total=total))
    if keep <= 0:
        return "", True
    return text[:keep] + _TRUNCATION_MARKER.format(shown=keep, total=total), True


# ---------------------------------------------------------------------------- 内部实现


class _TextExtractor(HTMLParser):
    """把 HTML 摊成一串文本片段。

    `convert_charrefs=True`（默认）意味着实体已经在 `handle_data` 里解好了，因此本类
    不实现 `handle_entityref` / `handle_charref`——实现了反而会漏掉一半的实体。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.title = ""
        self._drop_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in _DROPPED_TAGS:
            self._drop_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag in _BLOCK_TAGS:
            self._break()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in _BLOCK_TAGS:
            self._break()

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROPPED_TAGS:
            # 不配对的结束标签在真实网页里很常见，钳到 0 而不是让计数变负——否则一个
            # 多余的 `</head>` 会让此后所有正文被当成「被丢弃的节点」。
            self._drop_depth = max(0, self._drop_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK_TAGS:
            self._break()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._drop_depth == 0:
            self.chunks.append(data)

    def _break(self) -> None:
        """插一个换行，但**相邻的块边界只算一次**。

        `</li><li>` 会连着产出两个换行，照单全收就会让每两条列表项之间空一行。
        块级元素之间统一用单个换行分隔——段落感由缩进与标点承担，不由空行承担。
        两个标签之间的缩进空白同样要先丢掉，否则它会把两个换行隔开、让上面那条判断失效。
        """
        while self.chunks and self.chunks[-1] != "\n" and not self.chunks[-1].strip():
            self.chunks.pop()
        if self.chunks and self.chunks[-1] == "\n":
            return
        self.chunks.append("\n")


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _collapse(text: str) -> str:
    return _WHITESPACE_RUN.sub(" ", text.replace("\n", " ")).strip()


def _tidy(chunks: list[str]) -> str:
    joined = _normalize_newlines("".join(chunks))
    lines = [_WHITESPACE_RUN.sub(" ", line).strip() for line in joined.split("\n")]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()
