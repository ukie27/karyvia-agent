"""`extract.py` 的纯函数用例：媒体类型、解码、正文抽取、截断。

一个 IO 都不做，因此每条规则都能逐字节钉住。
"""

from __future__ import annotations

import pytest
from karyvia_plugin_web.extract import (
    REPLACEMENT_CHAR,
    MediaKind,
    charset_of,
    decode_body,
    html_to_text,
    looks_binary,
    media_kind_of,
    truncate,
)


class TestMediaKind:
    @pytest.mark.parametrize(
        ("content_type", "expected"),
        [
            ("text/html", MediaKind.HTML),
            ("text/html; charset=utf-8", MediaKind.HTML),
            ("TEXT/HTML", MediaKind.HTML),
            ("application/xhtml+xml", MediaKind.HTML),
            ("text/plain", MediaKind.TEXT),
            ("text/markdown; charset=utf-8", MediaKind.TEXT),
            ("application/json", MediaKind.TEXT),
            ("application/rss+xml", MediaKind.TEXT),
            ("application/pdf", MediaKind.UNSUPPORTED),
            ("image/png", MediaKind.UNSUPPORTED),
            ("application/octet-stream", MediaKind.UNSUPPORTED),
        ],
    )
    def test_classification(self, content_type: str, expected: str) -> None:
        assert media_kind_of(content_type) == expected

    def test_a_missing_content_type_is_unsupported_not_guessed_as_html(self) -> None:
        """不声明类型的端点更可能在传二进制，猜错的代价是把字节流塞进模型上下文。"""
        assert media_kind_of("") == MediaKind.UNSUPPORTED


class TestDecode:
    @pytest.mark.parametrize(
        ("content_type", "expected"),
        [
            ("text/html; charset=utf-8", "utf-8"),
            ('text/html; charset="GBK"', "gbk"),
            ("text/html;charset=iso-8859-1;foo=bar", "iso-8859-1"),
            ("text/html", ""),
        ],
    )
    def test_charset_of(self, content_type: str, expected: str) -> None:
        assert charset_of(content_type) == expected

    def test_declared_charset_is_honoured(self) -> None:
        body = "中文".encode("gbk")
        text, lossy = decode_body(body, "text/html; charset=gbk")
        assert (text, lossy) == ("中文", False)

    def test_an_unknown_charset_falls_back_instead_of_failing(self) -> None:
        """写错 charset 的站点仍然值得读，而退回的结果是可见的。"""
        text, lossy = decode_body("hi".encode(), "text/html; charset=x-nonesuch")
        assert (text, lossy) == ("hi", False)

    def test_bad_utf8_is_lossy_not_rejected(self) -> None:
        text, lossy = decode_body(b"a\xffb", "text/plain; charset=utf-8")
        assert lossy is True
        assert REPLACEMENT_CHAR in text

    def test_newlines_are_normalised(self) -> None:
        text, _ = decode_body(b"a\r\nb\rc", "text/plain")
        assert text == "a\nb\nc"

    def test_looks_binary(self) -> None:
        assert looks_binary(b"\x89PNG\x00\x1a") is True
        assert looks_binary(b"<html>hi</html>") is False


class TestHtmlToText:
    def test_title_and_body(self) -> None:
        page = html_to_text("<html><head><title> Hello  World </title></head><body><p>a</p></body></html>")
        assert page.title == "Hello World"
        assert page.text == "a"

    def test_script_and_style_are_dropped_whole(self) -> None:
        page = html_to_text(
            "<body><p>keep</p><script>var evil = 1;</script><style>p{color:red}</style></body>"
        )
        assert "evil" not in page.text
        assert "color:red" not in page.text
        assert "keep" in page.text

    def test_inline_tags_do_not_split_a_sentence(self) -> None:
        """行内标签不产生换行，否则一句话会被拆碎。"""
        page = html_to_text("<p>an <em>important</em> word</p>")
        assert page.text == "an important word"

    def test_block_tags_become_line_breaks(self) -> None:
        page = html_to_text("<ul><li>one</li><li>two</li></ul>")
        assert page.text.splitlines() == ["one", "two"]

    def test_entities_are_decoded(self) -> None:
        page = html_to_text("<p>a &amp; b &lt;c&gt;</p>")
        assert page.text == "a & b <c>"

    def test_an_unbalanced_closing_tag_does_not_swallow_the_rest(self) -> None:
        """真实网页里不配对的结束标签很常见；计数钳到 0 而不是变负。"""
        page = html_to_text("</head><body><p>visible</p></body>")
        assert "visible" in page.text

    def test_blank_lines_collapse(self) -> None:
        page = html_to_text("<div></div><div></div><div></div><p>x</p>")
        assert "\n\n\n" not in page.text


class TestTruncate:
    def test_short_text_is_untouched(self) -> None:
        assert truncate("abc", 10) == ("abc", False)

    def test_the_marker_counts_against_the_limit(self) -> None:
        """返回值长度恒 ≤ limit——下游是契约的 `MAX_TOOL_RESULT_LENGTH`，超一个字符就构造失败。"""
        text, cut = truncate("x" * 500, 120)
        assert cut is True
        assert len(text) <= 120

    def test_the_marker_reports_the_number_it_actually_kept(self) -> None:
        text, _ = truncate("x" * 500, 120)
        shown = text.count("x")
        assert f"已显示 {shown}/500 字符" in text

    def test_a_limit_too_small_for_the_marker_yields_empty(self) -> None:
        """配置写得不合理时宁可空，也不要一个说不清自己被截断的结果。"""
        assert truncate("x" * 50, 3) == ("", True)
