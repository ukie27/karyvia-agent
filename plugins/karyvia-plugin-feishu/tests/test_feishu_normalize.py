"""入站归一化的验收。

| 验收项 | 测试 |
| --- | --- |
| 门控顺序与五道门 | `TestGating` |
| `conversation_id` 合成与还原 | `TestConversationId` |
| 平台消息 → `InboundMessage` | `TestNormalization` |
| 附件 → `OPAQUE` 引用 | `TestAttachments` |
| 事件 → `RawInbound`（`gateway.event_to_raw`） | `TestEventConversion` |

**这一整个文件不需要装 `lark-oapi`**：`normalize()` 只认识 `RawInbound`，而
`event_to_raw()` 用 `getattr` 解构，一个长得像的普通对象就够（`_feishu_fakes.py`）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from _feishu_fakes import (
    BOT_OPEN_ID,
    CHAT_ID,
    MESSAGE_ID,
    USER_OPEN_ID,
    FakeEvent,
    FakeEventMessage,
    FakeMention,
    FakeSender,
    gate,
    mention,
    raw,
    text_content,
)
from karyvia_plugin_feishu import (
    decode_conversation,
    encode_conversation,
    event_to_raw,
    normalize,
)
from karyvia_plugin_feishu.normalize import DEDUP_CAPACITY, EMPTY_CONTENT

from karyvia.contracts import AttachmentSource, SessionKey


class TestGating:
    def test_bot_messages_are_dropped(self) -> None:
        """飞书的 bot 互发要另配权限，放行它们只会引入无法可靠判定的循环。"""
        probe = gate()
        assert normalize(raw(sender_type="bot"), probe) is None
        assert probe.rejections == ["bot_sender"]

    def test_an_incomplete_event_is_dropped(self) -> None:
        for missing in ({"message_id": ""}, {"chat_id": ""}, {"sender_id": ""}, {"msg_type": ""}):
            assert normalize(raw(**missing), gate()) is None
        assert normalize(raw(chat_type="unknown"), gate()) is None

    def test_a_group_message_without_an_address_is_dropped(self) -> None:
        assert normalize(raw(chat_type="group"), gate()) is None

    def test_the_group_gate_runs_before_dedup(self) -> None:
        """**顺序是行为**：群里没 @ 我的消息不该占用去重表的名额——一个热闹的群几分钟就能
        把表冲干净，此后 WS 的重投会真的变成第二次副作用。"""
        probe = gate()
        assert normalize(raw(chat_type="group"), probe) is None
        assert probe.seen_count() == 0

    def test_a_duplicate_is_dropped(self) -> None:
        """飞书的 WS 会重投。这是  在 Channel 侧的第一道防线。"""
        probe = gate()
        assert normalize(raw(), probe) is not None
        assert normalize(raw(), probe) is None
        assert probe.rejections == ["duplicate"]

    def test_the_dedup_table_is_bounded(self) -> None:
        probe = gate()
        for index in range(DEDUP_CAPACITY + 50):
            probe.remember(f"om_{index}")
        assert probe.seen_count() == DEDUP_CAPACITY

    def test_the_allowlist_runs_after_dedup_but_before_any_side_effect(self) -> None:
        """反应 ack 在群里所有人都看得见，它就是副作用——因此白名单必须在它之前。

        这条断言的可观察形态：被白名单拒掉的消息**仍然进过去重表**（说明顺序是
        「去重 → 白名单」），而 `normalize` 返回 `None` 意味着调用方根本没机会打反应。
        """
        probe = gate(allow_from=frozenset({"ou_other"}))
        assert normalize(raw(), probe) is None
        assert probe.rejections == ["sender_not_allowed"]
        assert probe.seen_count() == 1

    def test_an_empty_allow_from_allows_everyone(self) -> None:
        """与 legacy 一致。改它等于静默改变谁能用这个 bot。"""
        assert normalize(raw(), gate(allow_from=frozenset())) is not None

    def test_allow_chats_is_a_whitelist(self) -> None:
        assert normalize(raw(), gate(allow_chats=frozenset({CHAT_ID}))) is not None
        assert normalize(raw(), gate(allow_chats=frozenset({"oc_other"}))) is None


class TestConversationId:
    def test_a_private_chat_is_its_chat_id(self) -> None:
        message = normalize(raw(), gate())
        assert message is not None
        assert message.conversation_id == CHAT_ID

    def test_a_group_topic_gets_its_own_conversation(self) -> None:
        """飞书的话题没有独立 id，因此必须合成。"""
        message = normalize(
            raw(chat_type="group", root_id="om_root", mentions=(mention(),)), gate()
        )
        assert message is not None
        assert message.conversation_id == f"{CHAT_ID}:om_root"

    def test_a_group_without_a_root_uses_the_message_id(self) -> None:
        """一条新起的群消息就是它自己那个话题的根。"""
        message = normalize(raw(chat_type="group", mentions=(mention(),)), gate())
        assert message is not None
        assert message.conversation_id == f"{CHAT_ID}:{MESSAGE_ID}"

    def test_topic_isolation_off_shares_one_conversation(self) -> None:
        message = normalize(
            raw(chat_type="group", root_id="om_root", mentions=(mention(),)),
            gate(topic_isolation=False),
        )
        assert message is not None
        assert message.conversation_id == CHAT_ID

    def test_the_synthesis_round_trips(self) -> None:
        """**出站寻址靠还原拿回 `chat_id`**，因此合成必须可逆。"""
        assert decode_conversation(encode_conversation(CHAT_ID, "om_r")) == (CHAT_ID, "om_r")
        assert decode_conversation(encode_conversation(CHAT_ID, None)) == (CHAT_ID, None)

    def test_the_synthesis_survives_the_session_key_encoding(self) -> None:
        """`:` 会被 `storage_id()` 编码成 `%3A`，因此合成串可以直接当目录名且无碰撞。"""
        key = SessionKey(channel_id="feishu", conversation_id=encode_conversation(CHAT_ID, "om_r"))
        assert "%3A" in key.storage_id()
        assert SessionKey.from_storage_id(key.storage_id()) == key


class TestNormalization:
    def test_a_text_message_round_trips(self) -> None:
        message = normalize(raw(content=text_content("你好")), gate())
        assert message is not None
        assert message.content == "你好"
        assert message.sender.user_id == USER_OPEN_ID
        assert message.timestamp.tzinfo is not None

    def test_a_leading_bot_mention_is_stripped_before_the_body_is_used(self) -> None:
        """`@bot /help` 必须变成 `/help`，否则 dispatcher 认不出它是命令。"""
        message = normalize(
            raw(
                chat_type="group",
                content=text_content("@_user_1 /help"),
                mentions=(mention(),),
            ),
            gate(),
        )
        assert message is not None
        assert message.content == "/help"

    def test_platform_fields_live_under_their_own_namespace(self) -> None:
        """平台私有字段只进 `metadata["feishu"]`，Kernel 不解读它。"""
        message = normalize(raw(parent_id="om_parent"), gate())
        assert message is not None
        assert set(message.metadata) == {"feishu"}
        assert message.metadata["feishu"]["chat_id"] == CHAT_ID
        assert message.metadata["feishu"]["parent_id"] == "om_parent"

    def test_is_operator_only_for_the_named_operators(self) -> None:
        assert normalize(raw(), gate()).sender.is_operator is False  # type: ignore[union-attr]
        named = gate(operators=frozenset({USER_OPEN_ID}))
        assert normalize(raw(), named).sender.is_operator is True  # type: ignore[union-attr]

    def test_a_reply_carries_its_parent(self) -> None:
        message = normalize(raw(parent_id="om_parent"), gate())
        assert message is not None
        assert message.reply_to == "om_parent"

    def test_an_empty_message_gets_a_floor_value(self) -> None:
        """契约要求内容与附件不能同时为空。"""
        message = normalize(raw(content=text_content("   ")), gate())
        assert message is not None
        assert message.content == EMPTY_CONTENT

    def test_a_malformed_content_payload_degrades_instead_of_raising(self) -> None:
        """一条看不懂的消息体应当变成「没有正文」，而不是让整条 Channel 报错。"""
        message = normalize(raw(content="not json"), gate())
        assert message is not None
        assert message.content == EMPTY_CONTENT

    def test_a_bad_timestamp_falls_back_to_now(self) -> None:
        message = normalize(raw(create_time="nonsense"), gate())
        assert message is not None
        assert message.timestamp <= datetime.now(UTC)


class TestAttachments:
    def test_a_resource_becomes_an_opaque_ref(self) -> None:
        """**飞书的资源没有公开 URL**——只能用 `message_id + file_key` 经 SDK 换取，
        而那正是 `AttachmentSource.OPAQUE` 的定义。因此本插件不下载、不落盘。"""
        message = normalize(
            raw(msg_type="image", content=json.dumps({"image_key": "img_1"})), gate()
        )
        assert message is not None
        ref = message.attachments[0]
        assert ref.source is AttachmentSource.OPAQUE
        assert ref.locator == f"{MESSAGE_ID}:img_1"
        # 飞书的事件不带 MIME，因此这是按 `msg_type` 猜的一个**合法形状**——
        # 契约拒绝通配的 `image/*`，而下游本来就得按 `file_key` 换回来才知道真类型。
        assert ref.media_type == "image/png"
        assert message.content == "[image]"

    def test_post_images_become_refs_too(self) -> None:
        payload = json.dumps(
            {"zh_cn": {"content": [[{"tag": "text", "text": "看图"}, {"tag": "img", "image_key": "img_9"}]]}}
        )
        message = normalize(raw(msg_type="post", content=payload), gate())
        assert message is not None
        assert message.content == "看图"
        assert message.attachments[0].locator == f"{MESSAGE_ID}:img_9"

    def test_a_resource_without_a_key_still_gets_a_label(self) -> None:
        message = normalize(raw(msg_type="audio", content="{}"), gate())
        assert message is not None
        assert message.content == "[audio]"
        assert message.attachments == ()


class TestEventConversion:
    def test_a_plain_event_round_trips(self) -> None:
        converted = event_to_raw(FakeEvent())
        assert converted.message_id == MESSAGE_ID
        assert converted.chat_id == CHAT_ID
        assert converted.sender_id == USER_OPEN_ID
        assert converted.sender_type == "user"
        assert converted.msg_type == "text"

    def test_mentions_are_flattened(self) -> None:
        converted = event_to_raw(
            FakeEvent(FakeEventMessage(mentions=(FakeMention(open_id=BOT_OPEN_ID),)))
        )
        assert converted.mentions[0].open_id == BOT_OPEN_ID
        assert converted.mentions[0].key == "@_user_1"

    def test_empty_optional_ids_become_none(self) -> None:
        """空串与 `None` 在门控里的含义不同，拍平时必须归一。"""
        converted = event_to_raw(FakeEvent())
        assert converted.root_id is None
        assert converted.parent_id is None
        assert converted.thread_id is None

    def test_a_bot_sender_is_preserved_for_the_gate_to_reject(self) -> None:
        converted = event_to_raw(FakeEvent(sender=FakeSender(sender_type="bot")))
        assert converted.sender_type == "bot"
