"""@ 门控与 mention 占位符的验收（开发方案 `D34`）。

| 验收项 | 测试 |
| --- | --- |
| 四条命中路径 + 兜底启发式 | `TestAddressing` |
| 前导 @ 剥离（**必须在命令路由之前**） | `TestStripLeading` |
| 占位符 → 名字 | `TestResolve` |

纯正则，不需要事件循环也不需要假平台。
"""

from __future__ import annotations

from _feishu_fakes import BOT_OPEN_ID, mention
from karyvia_plugin_feishu import (
    is_addressed_to_bot,
    resolve_mentions,
    strip_leading_bot_mention,
)
from karyvia_plugin_feishu.mentions import AT_ALL, mentions_from


class TestAddressing:
    def test_open_policy_answers_everything(self) -> None:
        assert is_addressed_to_bot(
            content="随便说说", mentions=(), bot_open_id=BOT_OPEN_ID, group_policy="open"
        )

    def test_mention_policy_needs_an_address(self) -> None:
        assert not is_addressed_to_bot(
            content="随便说说", mentions=(), bot_open_id=BOT_OPEN_ID, group_policy="mention"
        )

    def test_at_all_counts_and_is_checked_before_open_id(self) -> None:
        """`@所有人` **不出现在 mentions 列表里**，先比 open_id 会让它永远漏掉。"""
        assert is_addressed_to_bot(
            content=f"{AT_ALL} 大家看一下",
            mentions=(),
            bot_open_id=BOT_OPEN_ID,
            group_policy="mention",
        )

    def test_an_open_id_match_counts(self) -> None:
        assert is_addressed_to_bot(
            content="@_user_1 在吗",
            mentions=(mention(),),
            bot_open_id=BOT_OPEN_ID,
            group_policy="mention",
        )

    def test_someone_elses_mention_does_not_count(self) -> None:
        assert not is_addressed_to_bot(
            content="@_user_1 在吗",
            mentions=(mention(open_id="ou_someone", user_id="u_1"),),
            bot_open_id=BOT_OPEN_ID,
            group_policy="mention",
        )

    def test_without_a_bot_identity_a_userless_ou_mention_counts(self) -> None:
        """飞书拿不到 bot 身份可能是永久的权限缺失，兜底避免群聊功能静默失效。"""
        assert is_addressed_to_bot(
            content="@_user_1 在吗",
            mentions=(mention(open_id="ou_unknown", user_id=""),),
            bot_open_id="",
            group_policy="mention",
        )

    def test_the_fallback_ignores_mentions_that_carry_a_user_id(self) -> None:
        """真人的 mention 通常带 `user_id`——那是把它与机器人分开的唯一线索。"""
        assert not is_addressed_to_bot(
            content="@_user_1 在吗",
            mentions=(mention(open_id="ou_person", user_id="u_9"),),
            bot_open_id="",
            group_policy="mention",
        )


class TestStripLeading:
    def test_a_leading_bot_mention_is_removed(self) -> None:
        """**必须在命令路由之前**：`@bot /help` 不剥就会被 dispatcher 当成普通文本。"""
        assert strip_leading_bot_mention(
            "@_user_1 /help", (mention(),), bot_open_id=BOT_OPEN_ID
        ) == "/help"

    def test_a_mention_in_the_middle_is_left_alone(self) -> None:
        text = "帮我问问 @_user_1"
        assert strip_leading_bot_mention(text, (mention(),), bot_open_id=BOT_OPEN_ID) == text

    def test_user_1_does_not_eat_the_prefix_of_user_10(self) -> None:
        """负向断言 `(?![A-Za-z0-9_])` 存在的全部理由。没有它这里会留下一个孤零零的 `0`。"""
        result = strip_leading_bot_mention(
            "@_user_10 在吗", (mention(key="@_user_1"),), bot_open_id=BOT_OPEN_ID
        )
        assert result == "@_user_10 在吗"

    def test_someone_elses_leading_mention_is_kept(self) -> None:
        text = "@_user_2 你看看"
        assert (
            strip_leading_bot_mention(
                text, (mention(key="@_user_2", open_id="ou_other"),), bot_open_id=BOT_OPEN_ID
            )
            == text
        )

    def test_without_a_bot_identity_the_first_leading_mention_is_stripped(self) -> None:
        assert strip_leading_bot_mention(
            "@_user_1 /help", (mention(open_id="ou_unknown"),), bot_open_id=""
        ) == "/help"


class TestResolve:
    def test_placeholders_become_names(self) -> None:
        """模型看到的是正文；留着 `@_user_2` 它只能猜。"""
        text = resolve_mentions("你好 @_user_2", (mention(key="@_user_2", name="小明"),))
        assert text == "你好 @小明"

    def test_a_nameless_mention_gets_a_placeholder_label(self) -> None:
        text = resolve_mentions("你好 @_user_2", (mention(key="@_user_2", name=""),))
        assert text == "你好 @某人"

    def test_user_1_does_not_eat_user_10(self) -> None:
        text = resolve_mentions("@_user_10", (mention(key="@_user_1", name="甲"),))
        assert text == "@_user_10"


class TestMentionsFrom:
    def test_malformed_entries_are_skipped_not_fatal(self) -> None:
        """一条看不懂的 mention 不该让整条消息被丢掉（`MSG-004`）。"""
        parsed = mentions_from(["nope", {"no_key": 1}, {"key": "@_user_1", "name": "甲"}])
        assert len(parsed) == 1
        assert parsed[0].key == "@_user_1"

    def test_ids_are_flattened(self) -> None:
        parsed = mentions_from(
            [{"key": "@_user_1", "id": {"open_id": "ou_x", "user_id": "u_1"}}]
        )
        assert parsed[0].open_id == "ou_x"
        assert parsed[0].user_id == "u_1"
