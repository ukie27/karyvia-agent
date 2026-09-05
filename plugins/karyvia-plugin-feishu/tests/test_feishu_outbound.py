"""出站渲染的验收（`MSG-003`、`EDG-304`，开发方案 `D34`）。

| 验收项 | 测试 |
| --- | --- |
| 三选一格式级联**逐条** + 两条边界值 | `TestFormatCascade` |
| markdown → post 体（含硬编码的 `zh_cn`） | `TestPostBody` |
| 中断 / 失败标记 | `TestTerminalMarkers` |
| 卡片元素与**一表一卡** | `TestCards` |

纯函数，不需要事件循环也不需要假平台。
"""

from __future__ import annotations

import json

from karyvia_plugin_feishu import (
    POST_MAX_LEN,
    TERMINAL_MARKERS,
    TEXT_MAX_LEN,
    build_elements,
    detect_format,
    markdown_to_post,
    split_by_table_limit,
)
from karyvia_plugin_feishu.cards import MAX_TABLES_PER_CARD, strip_markdown_marks
from karyvia_plugin_feishu.outbound import compose_body, fallback_chunks

from karyvia.contracts import AttachmentRef, AttachmentSource, StreamState

_TABLE = "|列一|列二|\n|---|---|\n|1|2|\n"


class TestFormatCascade:
    """**七条判据，顺序不可换**（`outbound.py` 的模块 docstring 说明了为什么）。"""

    def test_1_a_code_block_forces_a_card(self) -> None:
        assert detect_format("```py\nprint(1)\n```") == "interactive"

    def test_1_a_table_forces_a_card(self) -> None:
        assert detect_format(_TABLE) == "interactive"

    def test_1_a_heading_forces_a_card(self) -> None:
        assert detect_format("# 标题\n正文") == "interactive"

    def test_2_long_content_becomes_a_card(self) -> None:
        assert detect_format("字" * (POST_MAX_LEN + 1)) == "interactive"

    def test_3_bold_italic_and_strike_force_a_card(self) -> None:
        """post 渲染不了它们——发过去用户会看到一堆星号。"""
        for text in ("**粗**", "__粗__", "*斜*", "~~删~~"):
            assert detect_format(text) == "interactive"

    def test_4_lists_force_a_card(self) -> None:
        assert detect_format("- 一\n- 二") == "interactive"
        assert detect_format("1. 一\n2. 二") == "interactive"

    def test_5_a_link_becomes_post(self) -> None:
        """post 支持 `<a>` 标签，而 text 里的链接不可点。"""
        assert detect_format("看这个 [文档](https://example.test)") == "post"

    def test_6_short_plain_text_stays_text(self) -> None:
        assert detect_format("你好") == "text"

    def test_7_medium_plain_text_becomes_post(self) -> None:
        assert detect_format("字" * (TEXT_MAX_LEN + 1)) == "post"

    def test_the_text_boundary_is_inclusive(self) -> None:
        assert detect_format("字" * TEXT_MAX_LEN) == "text"
        assert detect_format("字" * (TEXT_MAX_LEN + 1)) == "post"

    def test_the_post_boundary_is_inclusive(self) -> None:
        assert detect_format("字" * POST_MAX_LEN) == "post"
        assert detect_format("字" * (POST_MAX_LEN + 1)) == "interactive"

    def test_length_is_checked_before_formatting(self) -> None:
        """顺序的可观察后果：一段超长的纯文本走卡片而不是 post。"""
        assert detect_format("字" * 2500) == "interactive"

    def test_a_link_in_a_short_message_still_wins_over_text(self) -> None:
        """顺序的另一个可观察后果：链接判据在长度判据之前。"""
        assert detect_format("[a](https://x.test)") == "post"


class TestPostBody:
    def test_the_locale_key_is_hardcoded(self) -> None:
        """`zh_cn` 是 post 消息体的**必需结构**，不是 i18n 选择——做成配置会让消息在某些
        租户上发不出去。"""
        body = json.loads(markdown_to_post("你好"))
        assert set(body) == {"zh_cn"}
        assert body["zh_cn"]["content"] == [[{"tag": "text", "text": "你好"}]]

    def test_links_become_anchor_tags(self) -> None:
        body = json.loads(markdown_to_post("见 [文档](https://example.test) 谢谢"))
        elements = body["zh_cn"]["content"][0]
        assert elements[0] == {"tag": "text", "text": "见 "}
        assert elements[1] == {"tag": "a", "text": "文档", "href": "https://example.test"}
        assert elements[2] == {"tag": "text", "text": " 谢谢"}

    def test_each_line_is_a_paragraph(self) -> None:
        body = json.loads(markdown_to_post("一\n二"))
        assert len(body["zh_cn"]["content"]) == 2

    def test_a_blank_line_keeps_its_paragraph(self) -> None:
        """飞书的 post 靠段落分行，去掉空行整段会挤成一坨。"""
        body = json.loads(markdown_to_post("一\n\n二"))
        assert body["zh_cn"]["content"][1] == [{"tag": "text", "text": ""}]


class TestTerminalMarkers:
    def test_the_markers_cover_exactly_the_two_incomplete_states(self) -> None:
        assert set(TERMINAL_MARKERS) == {StreamState.CANCELLED, StreamState.FAILED}

    def test_the_markers_are_the_same_words_the_terminal_uses(self) -> None:
        """与 `builtins/cli_entry/console.py::TERMINAL_MARKERS` 逐字相同。
        `R4` 够不着彼此，因此各写一份并用这条对照用例防止漂移。"""
        assert TERMINAL_MARKERS[StreamState.CANCELLED] == "[已中断：以上是中断前已产生的内容]"
        assert TERMINAL_MARKERS[StreamState.FAILED] == "[本轮失败]"

    def test_the_marker_shares_the_body_with_the_partial_answer(self) -> None:
        """`EDG-304`：分开发会让半截答案孤零零留在上面看起来像完整回答。"""
        body = compose_body("半句", (), StreamState.CANCELLED)
        assert "半句" in body
        assert body.endswith("[已中断：以上是中断前已产生的内容]")

    def test_a_complete_answer_carries_no_marker(self) -> None:
        assert compose_body("答案", (), StreamState.FINAL) == "答案"

    def test_an_empty_cancellation_is_just_the_marker(self) -> None:
        assert compose_body("", (), StreamState.CANCELLED) == TERMINAL_MARKERS[StreamState.CANCELLED]

    def test_an_unsendable_attachment_says_so(self) -> None:
        """`FileAccess` 没有 `read_bytes`，本轮传不出去。假装发过比说不出去更糟。"""
        ref = AttachmentRef(
            source=AttachmentSource.WORKSPACE,
            locator="notes/a.md",
            media_type="text/markdown",
            filename="a.md",
        )
        assert "无法上传" in compose_body("给你", (ref,), StreamState.FINAL)


class TestCards:
    def test_a_plain_paragraph_is_one_markdown_element(self) -> None:
        elements = build_elements("就一段话")
        assert elements == [{"tag": "markdown", "content": "就一段话"}]

    def test_a_heading_becomes_a_bold_div(self) -> None:
        """飞书的卡片没有原生标题元素。"""
        elements = build_elements("# 标题\n正文")
        assert elements[0]["tag"] == "div"
        assert elements[0]["text"]["content"] == "**标题**"

    def test_a_markdown_table_becomes_a_table_element(self) -> None:
        elements = build_elements(_TABLE)
        table = next(item for item in elements if item["tag"] == "table")
        assert [column["display_name"] for column in table["columns"]] == ["列一", "列二"]
        assert table["rows"] == [{"c0": "1", "c1": "2"}]
        # `page_size` 要大于等于行数，否则飞书会分页、后面几行用户看不见。
        assert table["page_size"] >= len(table["rows"])

    def test_two_tables_become_two_cards(self) -> None:
        """飞书对含多个表格的卡片直接报错（11310）。拆开之后每个表格都送达。"""
        groups = split_by_table_limit(build_elements(f"{_TABLE}\n中间\n\n{_TABLE}"))
        assert len(groups) == 2
        for group in groups:
            assert sum(1 for item in group if item["tag"] == "table") <= MAX_TABLES_PER_CARD

    def test_a_pipe_inside_a_code_block_is_not_a_table(self) -> None:
        """代码块要先保护起来——表格与标题的正则都是行首锚定的多行匹配。"""
        source = "```\n|a|b|\n|-|-|\n|1|2|\n```"
        elements = build_elements(source)
        assert all(item["tag"] != "table" for item in elements)
        assert "```" in elements[0]["content"]

    def test_a_hash_inside_a_code_block_is_not_a_heading(self) -> None:
        elements = build_elements("```\n# 注释\n```")
        assert all(item["tag"] != "div" for item in elements)

    def test_table_cells_have_their_markdown_marks_stripped(self) -> None:
        """飞书的 table 元素不渲染 markdown，留着 `**` 用户会看到一堆星号。"""
        elements = build_elements("|**粗**|b|\n|---|---|\n|~~删~~|2|\n")
        table = next(item for item in elements if item["tag"] == "table")
        assert table["columns"][0]["display_name"] == "粗"
        assert table["rows"][0]["c0"] == "删"

    def test_strip_markdown_marks_handles_all_four_forms(self) -> None:
        assert strip_markdown_marks("**a** __b__ *c* ~~d~~") == "a b c d"

    def test_an_unparseable_table_degrades_to_markdown(self) -> None:
        """降级成「渲染得不好看」而不是「发不出去」。"""
        elements = build_elements("|只有表头|\n|---|\n")
        assert all(item["tag"] in {"markdown", "table"} for item in elements)

    def test_empty_input_still_yields_one_group(self) -> None:
        assert split_by_table_limit([]) == [[]]


class TestFallbackChunks:
    def test_short_text_is_one_chunk(self) -> None:
        assert fallback_chunks("短") == ["短"]

    def test_an_empty_string_yields_nothing(self) -> None:
        assert fallback_chunks("") == []

    def test_a_newline_is_preferred_as_the_cut(self) -> None:
        text = "a" * 30 + "\n" + "b" * 30
        assert fallback_chunks(text, 40) == ["a" * 30, "b" * 30]

    def test_a_cut_in_the_first_half_falls_back_to_a_hard_split(self) -> None:
        """为了「好看」把一块切成很小的一段只会多发几条消息。"""
        text = "a" * 5 + "\n" + "b" * 60
        chunks = fallback_chunks(text, 40)
        assert len(chunks[0]) == 40
