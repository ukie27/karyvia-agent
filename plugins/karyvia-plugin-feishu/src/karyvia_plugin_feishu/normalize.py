"""入站归一化：飞书事件 → 契约 `InboundMessage`（`MSG-004`、`MSG-002`，开发方案 `D34`）。

职责：门控顺序、去重、`conversation_id` 合成、附件引用、metadata 命名空间。
不负责：接触 SDK（`gateway.py` 已经把事件拍成了 `RawInbound`）、正文抽取（`content.py`）、
@ 判定细节（`mentions.py`）、下载任何东西。

**门控顺序本身是行为**，三条各有一个必须保留的理由：

    丢 bot 消息 → 字段完整性 → 群聊 @ 门控 → 去重 → 白名单 → 归一化
                                  ↑ 在去重之前     ↑ 在任何副作用之前

- **@ 门控在去重之前**：群里没 @ 我的消息不该占用去重表的 1000 个名额——一个热闹的群几
  分钟就能把表冲干净，此后 WS 的重投会真的变成第二次副作用（`EDG-201`）。
- **白名单在任何副作用之前**：反应 ack 在群里所有人都看得见，它就是副作用。
- **去重表上限 1000、FIFO 淘汰**：飞书的 WS 会重投。这是 `EDG-201` 在 Channel 侧的第一道
  防线，kernel 的 `DedupCache` 是第二道——**两道都要**，因为第一道决定要不要打反应。

**bot 消息一律丢弃**：飞书的 bot 互相收发要在开放平台另配权限，默认拿不到；放行它们只会
引入一类本插件无法判定的循环。

**`conversation_id` 是合成的**：飞书的话题没有独立 id，
`chat_id` 对整个群恒定，因此话题隔离要靠 `chat_id:root_id`。合成必须**可逆**——
`OutboundMessage` 只带 `conversation_id`，Channel 靠它拿回 `chat_id` 去寻址。
分隔符用 `:`：它过得了 `validate_identifier`（只拒空/超长/控制字符），而
`SessionKey.storage_id()` 会把它编码成 `%3A`，因此合成串可以直接当目录名且无碰撞。
"""

from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from karyvia.contracts import (
    AttachmentRef,
    AttachmentSource,
    InboundMessage,
    InstanceId,
    JsonValue,
    Sender,
)

from .content import MSG_TYPE_LABELS, extract_post, extract_share_card, parse_content
from .mentions import (
    GROUP_POLICY_MENTION,
    Mention,
    is_addressed_to_bot,
    resolve_mentions,
    strip_leading_bot_mention,
)

__all__ = [
    "DEDUP_CAPACITY",
    "EMPTY_CONTENT",
    "InboundGate",
    "RawInbound",
    "decode_conversation",
    "encode_conversation",
    "normalize",
]

#: Channel 侧去重表的容量。飞书的 WS 会重投，见模块 docstring。
DEDUP_CAPACITY: Final = 1000

#: 正文与附件都空时的地板值。契约要求「内容与 attachments 不能同时为空」。
EMPTY_CONTENT: Final = "[empty message]"

#: 话题隔离用的分隔符。见模块 docstring——它是可逆性与 `storage_id()` 编码共同决定的。
CONVERSATION_SEPARATOR: Final = ":"

#: 只有这两种消息带用户的 prompt。
_CHAT_TYPES: Final[frozenset[str]] = frozenset({"p2p", "group"})

#: 资源类消息的 media type。**飞书的事件里没有 MIME**，只有 `msg_type`，因此这是一张
#: 按类型猜的表。契约要求 `media_type` 是合法的 MIME 形状（`type/subtype`），
#: 通配的 `image/*` 过不了那道校验——猜一个具体的比留一个非法值诚实：下游真要用它时
#: 得自己按 `file_key` 换回来看，而那本来就是 `OPAQUE` 的语义。
_MEDIA_TYPES: Final[Mapping[str, str]] = {
    "image": "image/png",
    "audio": "audio/ogg",
    "media": "video/mp4",
    "file": "application/octet-stream",
    "sticker": "image/png",
}

_INVALID_ID: Final = re.compile(r"[^\w.-]")


def encode_conversation(chat_id: str, topic_root: str | None) -> str:
    """`(chat_id, 话题根)` → `conversation_id`。`topic_root` 为空即整群一个会话。"""
    return f"{chat_id}{CONVERSATION_SEPARATOR}{topic_root}" if topic_root else chat_id


def decode_conversation(conversation_id: str) -> tuple[str, str | None]:
    """`encode_conversation` 的逆运算。**出站寻址靠它拿回 `chat_id`。**

    `chat_id` 本身不含 `:`（飞书的 id 形如 `oc_xxx`），因此 `partition` 的还原无歧义。
    """
    chat_id, separator, topic = conversation_id.partition(CONVERSATION_SEPARATOR)
    return (chat_id, topic) if separator else (conversation_id, None)


@dataclass(frozen=True, slots=True)
class RawInbound:
    """一条飞书消息的归一化输入。**`gateway.py` 是它唯一的构造者。**

    它存在的理由是让本模块与它的用例**不需要装 `lark-oapi`**。
    """

    message_id: str
    chat_id: str
    chat_type: str
    msg_type: str
    sender_id: str
    sender_type: str
    #: `message.content` 的原始 JSON 字符串。
    content: str
    create_time: str = ""
    root_id: str | None = None
    parent_id: str | None = None
    thread_id: str | None = None
    mentions: tuple[Mention, ...] = ()

    @property
    def is_group(self) -> bool:
        return self.chat_type == "group"

    @property
    def complete(self) -> bool:
        """五个字段缺一不可——缺任何一个都没法安全地构造 `InboundMessage`。"""
        return bool(
            self.message_id
            and self.chat_id
            and self.sender_id
            and self.msg_type
            and self.chat_type in _CHAT_TYPES
        )


@dataclass(slots=True)
class InboundGate:
    """归一化需要的全部配置 + 去重表。由 `settings.py` 构造。

    **它有状态**（去重表），因此每条 Channel 持有**一个**实例并复用——每次新建会让去重
    表恒为空，`EDG-201` 的第一道防线随之失效。
    """

    instance_id: InstanceId
    channel_id: str
    bot_open_id: str = ""
    allow_from: frozenset[str] = frozenset()
    allow_chats: frozenset[str] = frozenset()
    operators: frozenset[str] = frozenset()
    group_policy: str = GROUP_POLICY_MENTION
    topic_isolation: bool = True
    rejections: list[str] = field(default_factory=list, compare=False)
    _seen: OrderedDict[str, None] = field(default_factory=OrderedDict, compare=False)

    def remember(self, message_id: str) -> bool:
        """记下这条 message_id。已经见过返回 `False`。有界 FIFO。"""
        if message_id in self._seen:
            return False
        self._seen[message_id] = None
        while len(self._seen) > DEDUP_CAPACITY:
            self._seen.popitem(last=False)
        return True

    def seen_count(self) -> int:
        """去重表当前条数。诊断与用例用。"""
        return len(self._seen)


def _timestamp(create_time: str) -> datetime:
    """飞书的 `create_time` 是**毫秒字符串**。解析失败回落到当前时间。

    契约要求 `timestamp` 带时区；一个解析不出来的时间戳不该让整条消息被丢掉。
    """
    try:
        return datetime.fromtimestamp(int(create_time) / 1000, UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


def _attachment(message_id: str, msg_type: str, file_key: str) -> AttachmentRef:
    """资源 → `OPAQUE` 引用。

    **飞书的资源没有公开 URL**——只能用 `message_id + file_key` 经 SDK 换取，而这正是
    `AttachmentSource.OPAQUE` 的定义（契约原文：「平台侧不透明标识，需 Channel 用自己的
    凭据换取」）。因此本插件在归一化阶段**不下载、不落盘**。
    """
    return AttachmentRef(
        source=AttachmentSource.OPAQUE,
        locator=f"{message_id}:{file_key}",
        media_type=_MEDIA_TYPES.get(msg_type, "application/octet-stream"),
    )


def _body(raw: RawInbound) -> tuple[str, tuple[AttachmentRef, ...]]:
    """按 `msg_type` 抽正文与附件引用。"""
    payload = parse_content(raw.content)
    if raw.msg_type == "text":
        text = payload.get("text")
        return (text if isinstance(text, str) else ""), ()
    if raw.msg_type == "post":
        text, image_keys = extract_post(payload)
        refs = tuple(_attachment(raw.message_id, "image", key) for key in image_keys)
        return text, refs
    if raw.msg_type in _MEDIA_TYPES:
        key = payload.get("image_key") or payload.get("file_key")
        label = MSG_TYPE_LABELS.get(raw.msg_type, f"[{raw.msg_type}]")
        if isinstance(key, str) and key:
            return label, (_attachment(raw.message_id, raw.msg_type, key),)
        return label, ()
    return extract_share_card(payload, raw.msg_type), ()


def _metadata(raw: RawInbound, *, topic_isolated: bool) -> Mapping[str, JsonValue]:
    """平台私有字段只进 `metadata["feishu"]`（`MSG-002`）。值必须可 JSON 化。"""
    payload: dict[str, JsonValue] = {
        "chat_id": raw.chat_id,
        "chat_type": raw.chat_type,
        "msg_type": raw.msg_type,
        "topic_isolated": topic_isolated,
    }
    for key, value in (
        ("root_id", raw.root_id),
        ("parent_id", raw.parent_id),
        ("thread_id", raw.thread_id),
    ):
        if value:
            payload[key] = value
    return {"feishu": payload}


def normalize(raw: RawInbound, gate: InboundGate) -> InboundMessage | None:
    """一条飞书消息 → `InboundMessage`，或 `None` 表示「不处理」。

    **返回 `None` 不是错误**：绝大多数被丢掉的消息是别人在群里正常聊天。原因记进
    `gate.rejections` 供诊断，**不发事件**——一个热闹的群每秒能产生几十条。
    """
    if raw.sender_type == "bot":
        gate.rejections.append("bot_sender")
        return None
    if not raw.complete:
        gate.rejections.append("incomplete")
        return None

    payload = parse_content(raw.content)
    text = payload.get("text")
    raw_text = text if isinstance(text, str) else ""
    if raw.is_group and not is_addressed_to_bot(
        content=raw_text,
        mentions=raw.mentions,
        bot_open_id=gate.bot_open_id,
        group_policy=gate.group_policy,
    ):
        gate.rejections.append("not_addressed")
        return None

    if not gate.remember(raw.message_id):
        gate.rejections.append("duplicate")
        return None

    if gate.allow_from and raw.sender_id not in gate.allow_from:
        gate.rejections.append("sender_not_allowed")
        return None
    if gate.allow_chats and raw.chat_id not in gate.allow_chats:
        gate.rejections.append("chat_not_allowed")
        return None

    body, attachments = _body(raw)
    if raw.msg_type == "text":
        body = strip_leading_bot_mention(body, raw.mentions, bot_open_id=gate.bot_open_id)
        body = resolve_mentions(body, raw.mentions)
    body = body.strip()
    if not body and not attachments:
        body = EMPTY_CONTENT

    topic_isolated = raw.is_group and gate.topic_isolation
    conversation_id = encode_conversation(
        raw.chat_id, (raw.root_id or raw.message_id) if topic_isolated else None
    )
    return InboundMessage(
        message_id=_INVALID_ID.sub("_", raw.message_id),
        instance_id=gate.instance_id,
        channel_id=gate.channel_id,
        conversation_id=conversation_id,
        sender=Sender(
            user_id=raw.sender_id,
            # `is_operator` 由 Channel 在边界决定（契约原话）：`operator_only` 命令
            # （`/config` 这类）的可达性全靠这一行。
            is_operator=raw.sender_id in gate.operators,
        ),
        content=body,
        timestamp=_timestamp(raw.create_time),
        attachments=attachments,
        reply_to=raw.parent_id,
        metadata=_metadata(raw, topic_isolated=topic_isolated),
    )


def mention_names(mentions: Sequence[Mention]) -> tuple[str, ...]:
    """诊断用：这条消息 @ 了谁。"""
    return tuple(mention.name or mention.open_id for mention in mentions)
