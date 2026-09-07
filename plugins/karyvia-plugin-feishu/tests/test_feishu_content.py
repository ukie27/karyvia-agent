"""正文抽取与指示器的验收。

| 验收项 | 测试 |
| --- | --- |
| post 的三种载荷形状 | `TestPost` |
| 交互卡片的递归抽取与 11 种 tag | `TestInteractive` |
| 分享卡片 / 系统消息 / 未知类型 | `TestShareCards` |
| 反应当指示器用 | `TestIndicators` |
"""

from __future__ import annotations

import json

from _feishu_fakes import CHAT_ID, MESSAGE_ID, FakeReactions
from karyvia_plugin_feishu import (
    Indicators,
    extract_interactive,
    extract_post,
    extract_share_card,
)
from karyvia_plugin_feishu.content import parse_content
from karyvia_plugin_feishu.indicators import REACTION_TABLE_CAPACITY


class TestPost:
    def test_the_direct_shape(self) -> None:
        text, images = extract_post({"title": "标题", "content": [[{"tag": "text", "text": "正文"}]]})
        assert "标题" in text and "正文" in text
        assert images == []

    def test_the_localized_shape(self) -> None:
        text, _ = extract_post({"zh_cn": {"content": [[{"tag": "text", "text": "你好"}]]}})
        assert text == "你好"

    def test_the_wrapped_shape(self) -> None:
        text, _ = extract_post({"post": {"zh_cn": {"content": [[{"tag": "text", "text": "你好"}]]}}})
        assert text == "你好"

    def test_an_unknown_locale_still_works(self) -> None:
        """不认识的语言键回落到「任意一个字典子节点」——否则那条消息会变成空正文。"""
        text, _ = extract_post({"de_de": {"content": [[{"tag": "text", "text": "hallo"}]]}})
        assert text == "hallo"

    def test_image_keys_are_collected(self) -> None:
        _, images = extract_post(
            {"zh_cn": {"content": [[{"tag": "img", "image_key": "img_1"}]]}}
        )
        assert images == ["img_1"]

    def test_at_and_code_blocks_are_rendered(self) -> None:
        text, _ = extract_post(
            {
                "zh_cn": {
                    "content": [
                        [{"tag": "at", "user_name": "小明"}],
                        [{"tag": "code_block", "language": "py", "text": "print(1)"}],
                    ]
                }
            }
        )
        assert "@小明" in text
        assert "```py" in text and "print(1)" in text


class TestInteractive:
    def test_markdown_and_text_elements(self) -> None:
        parts = extract_interactive({"elements": [{"tag": "markdown", "content": "一段"}]})
        assert parts == ["一段"]

    def test_a_nested_element_list(self) -> None:
        """两种形状都认：嵌套的 `[[el, …], …]` 与扁平的 `[el, …]`。"""
        parts = extract_interactive({"elements": [[{"tag": "text", "text": "嵌套"}]]})
        assert parts == ["嵌套"]

    def test_a_div_with_fields(self) -> None:
        parts = extract_interactive(
            {
                "elements": [
                    {
                        "tag": "div",
                        "text": {"content": "主体"},
                        "fields": [{"text": {"content": "字段"}}],
                    }
                ]
            }
        )
        assert parts == ["主体", "字段"]

    def test_links_and_buttons_expose_their_url(self) -> None:
        parts = extract_interactive(
            {
                "elements": [
                    {"tag": "a", "href": "https://x.test", "text": "点我"},
                    {"tag": "button", "text": {"content": "确定"}, "multi_url": {"url": "https://y.test"}},
                ]
            }
        )
        assert "link: https://x.test" in parts
        assert "link: https://y.test" in parts

    def test_a_table_becomes_rows(self) -> None:
        parts = extract_interactive(
            {
                "elements": [
                    {
                        "tag": "table",
                        "columns": [{"name": "c0", "display_name": "名字"}],
                        "rows": [{"c0": "甲"}],
                    }
                ]
            }
        )
        assert parts == ["名字", "甲"]

    def test_a_column_set_is_flattened(self) -> None:
        parts = extract_interactive(
            {
                "elements": [
                    {"tag": "column_set", "columns": [{"elements": [{"tag": "markdown", "content": "列"}]}]}
                ]
            }
        )
        assert parts == ["列"]

    def test_an_unknown_tag_recurses_into_its_children(self) -> None:
        """飞书一直在加新元素，而它们几乎都把子元素放在 `elements` 里。"""
        parts = extract_interactive(
            {"elements": [{"tag": "brand_new_thing", "elements": [{"tag": "markdown", "content": "里面"}]}]}
        )
        assert parts == ["里面"]

    def test_user_dsl_wins_and_short_circuits(self) -> None:
        """渲染过的卡片会把原始定义放在 `user_dsl`，它比渲染结果信息更全；两份合起来会让
        模型看到重复的正文。"""
        payload = {
            "user_dsl": json.dumps({"elements": [{"tag": "markdown", "content": "原始"}]}),
            "elements": [{"tag": "markdown", "content": "渲染"}],
        }
        assert extract_interactive(payload) == ["原始"]

    def test_a_schema_2_body_is_read(self) -> None:
        parts = extract_interactive({"body": {"elements": [{"tag": "markdown", "content": "新版"}]}})
        assert parts == ["新版"]

    def test_a_json_string_payload_is_parsed(self) -> None:
        parts = extract_interactive(json.dumps({"elements": [{"tag": "markdown", "content": "串"}]}))
        assert parts == ["串"]

    def test_a_top_level_title_comes_before_the_elements(self) -> None:
        parts = extract_interactive(
            {"title": "顶部", "elements": [{"tag": "markdown", "content": "正文"}]}
        )
        assert parts == ["title: 顶部", "正文"]

    def test_a_header_title_comes_after_the_elements(self) -> None:
        """**两个标题的位置不同。** 用一个合并列表再按下标切会在「只有 header、没有顶层
        title」时把它挪到最前面——那是一处静默的顺序错误（写这段时真的踩到过，
        因此这条用例存在）。
        """
        parts = extract_interactive(
            {
                "header": {"title": {"content": "抬头"}},
                "elements": [{"tag": "markdown", "content": "正文"}],
            }
        )
        assert parts == ["正文", "title: 抬头"]

    def test_both_titles_keep_their_own_sides(self) -> None:
        parts = extract_interactive(
            {
                "title": "顶部",
                "header": {"title": {"content": "抬头"}},
                "elements": [{"tag": "markdown", "content": "正文"}],
            }
        )
        assert parts == ["title: 顶部", "正文", "title: 抬头"]

    def test_an_unparseable_string_becomes_itself(self) -> None:
        assert extract_interactive("就是一段话") == ["就是一段话"]


class TestShareCards:
    def test_each_share_type_gets_a_summary(self) -> None:
        assert "shared chat" in extract_share_card({"chat_id": "oc_x"}, "share_chat")
        assert "shared user" in extract_share_card({"user_id": "ou_x"}, "share_user")
        assert "calendar" in extract_share_card({"event_key": "e"}, "share_calendar_event")
        assert extract_share_card({}, "system") == "[system message]"
        assert extract_share_card({}, "merge_forward") == "[merged forward messages]"

    def test_an_unknown_type_gets_a_label(self) -> None:
        assert extract_share_card({}, "brand_new") == "[brand_new]"

    def test_parse_content_degrades_instead_of_raising(self) -> None:
        """一条看不懂的消息体应当变成「没有正文」，而不是让整条 Channel 报错。"""
        assert parse_content("not json") == {}
        assert parse_content("[1, 2]") == {}


class TestIndicators:
    async def test_a_reaction_lands_on_receipt(self) -> None:
        reactions = FakeReactions()
        panel = Indicators(reactions=reactions)  # type: ignore[arg-type]
        await panel.start(CHAT_ID, MESSAGE_ID)
        assert reactions.added == [(MESSAGE_ID, "THUMBSUP")]
        assert panel.active() == 1

    async def test_stop_removes_it_by_reaction_id(self) -> None:
        """移除要的是 `reaction_id` 而不是 emoji 名，因此 `add` 的返回值必须记下来。"""
        reactions = FakeReactions()
        panel = Indicators(reactions=reactions)  # type: ignore[arg-type]
        await panel.start(CHAT_ID, MESSAGE_ID)
        await panel.stop(CHAT_ID)
        assert reactions.removed == [(MESSAGE_ID, "rid_1")]
        assert panel.active() == 0

    async def test_a_done_emoji_is_added_at_the_end(self) -> None:
        reactions = FakeReactions()
        panel = Indicators(reactions=reactions, done_emoji="DONE")  # type: ignore[arg-type]
        await panel.start(CHAT_ID, MESSAGE_ID)
        await panel.stop(CHAT_ID)
        assert reactions.added[-1] == (MESSAGE_ID, "DONE")

    async def test_starting_twice_cleans_up_the_previous_round(self) -> None:
        """否则一条接一条时会在会话里堆出一串没人清理的反应。"""
        reactions = FakeReactions()
        panel = Indicators(reactions=reactions)  # type: ignore[arg-type]
        await panel.start(CHAT_ID, "om_1")
        await panel.start(CHAT_ID, "om_2")
        assert reactions.removed == [("om_1", "rid_1")]
        assert panel.active() == 1

    async def test_an_empty_emoji_disables_the_indicator(self) -> None:
        reactions = FakeReactions()
        panel = Indicators(reactions=reactions, react_emoji="")  # type: ignore[arg-type]
        await panel.start(CHAT_ID, MESSAGE_ID)
        assert reactions.added == []

    async def test_a_failing_platform_never_bubbles_up(self) -> None:
        """反应可能因为消息被删、权限不足而失败——那是装饰不是内容。"""
        reactions = FakeReactions(fail=True)
        panel = Indicators(reactions=reactions)  # type: ignore[arg-type]
        await panel.start(CHAT_ID, MESSAGE_ID)
        await panel.stop(CHAT_ID)
        assert panel.active() == 0

    async def test_the_reaction_table_is_bounded(self) -> None:
        reactions = FakeReactions()
        panel = Indicators(reactions=reactions)  # type: ignore[arg-type]
        for index in range(REACTION_TABLE_CAPACITY + 20):
            await panel.start(f"oc_{index}", f"om_{index}")
        assert len(panel._ids) <= REACTION_TABLE_CAPACITY  # noqa: SLF001 - 断言的就是它有界

    async def test_shutdown_clears_everything(self) -> None:
        reactions = FakeReactions()
        panel = Indicators(reactions=reactions)  # type: ignore[arg-type]
        await panel.start("oc_a", "om_a")
        await panel.start("oc_b", "om_b")
        await panel.shutdown()
        assert panel.active() == 0

    async def test_stopping_an_unknown_conversation_is_a_no_op(self) -> None:
        reactions = FakeReactions()
        panel = Indicators(reactions=reactions)  # type: ignore[arg-type]
        await panel.stop("never-started")
        assert reactions.removed == []
