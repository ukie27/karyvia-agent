"""`feishu` 插件测试的假平台（供同目录的测试文件 import）。

职责：手写的飞书事件形状替身、四个 Protocol 的记录式实现、可注入的时钟，
以及 `RawInbound` / `OutboundMessage` / `FeishuSettings` 的构造助手。
不负责：任何断言——每条判定都写在对应的 `test_*.py` 里。

**它们不是 `lark-oapi` 的对象，只是长得像**：`gateway.event_to_raw()` 用 `getattr` 解构，
因此一个带对的属性的普通对象就够。这条正是「只有两个模块接触 SDK」换来的东西——
不装 `lark-oapi` 也能把归一化与流式逐条钉住。

**不放在 `conftest.py`**：那个文件的职责是零网络闸门那条 autouse 夹具（`D32`/`D33` 的先例）。
测试目录不是包，pytest 的 prepend 导入模式会把它加进 `sys.path`，因此 `import _feishu_fakes`
成立。

**文件名带插件前缀是必须的，不是洁癖**：正因为测试目录不是包，pytest 按**模块名**去重，
而 `testpaths` 一次收集整个 `plugins/`。两个插件各有一个 `_fakes.py` 时，先被导入的那个
会顶掉后一个，另一棵测试树整体 `ImportError`——单独跑各自的目录看不出来，跑全量才炸。
**下一个 Channel 插件照这个名字切。**
"""

from __future__ import annotations

import json
from typing import Any

from karyvia_plugin_feishu import FeishuSettings, InboundGate, Mention, RawInbound
from karyvia_plugin_feishu.outbound import OutboundBody
from karyvia_plugin_feishu.tool_hints import DEFAULT_PREFIX

from karyvia.contracts import (
    InstanceId,
    OutboundMessage,
    SecretStr,
    SessionKey,
    StreamState,
    TurnId,
)

CHANNEL_ID = "feishu"
CHAT_ID = "oc_chat_1"
ROOT_ID = "om_root_1"
MESSAGE_ID = "om_msg_1"
BOT_OPEN_ID = "ou_bot"
USER_OPEN_ID = "ou_user"
APP_ID = SecretStr("cli_app_id_0123456789")
APP_SECRET = SecretStr("app-secret-0123456789")


# ------------------------------------------------------------------ 事件形状替身


class FakeMentionId:
    def __init__(self, open_id: str = BOT_OPEN_ID, user_id: str = "") -> None:
        self.open_id = open_id
        self.user_id = user_id


class FakeMention:
    def __init__(
        self, key: str = "@_user_1", *, name: str = "机器人", open_id: str = BOT_OPEN_ID,
        user_id: str = "",
    ) -> None:
        self.key = key
        self.name = name
        self.id = FakeMentionId(open_id, user_id)


class FakeSenderId:
    def __init__(self, open_id: str = USER_OPEN_ID) -> None:
        self.open_id = open_id


class FakeSender:
    def __init__(self, open_id: str = USER_OPEN_ID, sender_type: str = "user") -> None:
        self.sender_id = FakeSenderId(open_id)
        self.sender_type = sender_type


class FakeEventMessage:
    def __init__(
        self,
        *,
        message_id: str = MESSAGE_ID,
        chat_id: str = CHAT_ID,
        chat_type: str = "p2p",
        message_type: str = "text",
        content: str | None = None,
        create_time: str = "1700000000000",
        root_id: str = "",
        parent_id: str = "",
        thread_id: str = "",
        mentions: tuple[FakeMention, ...] = (),
    ) -> None:
        self.message_id = message_id
        self.chat_id = chat_id
        self.chat_type = chat_type
        self.message_type = message_type
        self.content = content if content is not None else json.dumps({"text": "在吗"})
        self.create_time = create_time
        self.root_id = root_id
        self.parent_id = parent_id
        self.thread_id = thread_id
        self.mentions = mentions


class FakeEventBody:
    def __init__(self, message: FakeEventMessage, sender: FakeSender) -> None:
        self.message = message
        self.sender = sender


class FakeEvent:
    """`P2ImMessageReceiveV1` 的形状替身。`event_to_raw()` 全靠 `getattr`。"""

    def __init__(
        self, message: FakeEventMessage | None = None, sender: FakeSender | None = None
    ) -> None:
        self.event = FakeEventBody(message or FakeEventMessage(), sender or FakeSender())


# ------------------------------------------------------------------ Protocol 替身


class FakeCards:
    """`stream.Cards`。`calls` 里是每次调用的 `(op, card_id, sequence)` 三元组。

    **记录 sequence 是这个替身存在的全部理由**：跳号或复用会当场被看见，而断言「调了
    几次」看不出来。`fail_on` 按 `(op, sequence)` 注入失败。
    """

    def __init__(
        self, *, card_id: str | None = "card-1", fail_on: set[tuple[str, int]] | None = None
    ) -> None:
        self.calls: list[tuple[str, str, int]] = []
        self.contents: list[str] = []
        self._card_id = card_id
        self._fail_on = fail_on or set()

    async def create(self) -> str | None:
        self.calls.append(("create", self._card_id or "", 0))
        return self._card_id

    async def update(self, card_id: str, content: str, sequence: int) -> bool:
        self.calls.append(("content", card_id, sequence))
        if ("content", sequence) in self._fail_on:
            return False
        self.contents.append(content)
        return True

    async def set_streaming(self, card_id: str, enabled: bool, sequence: int) -> bool:
        self.calls.append(("settings", card_id, sequence))
        return ("settings", sequence) not in self._fail_on

    def sequences(self) -> list[int]:
        return [sequence for op, _, sequence in self.calls if op != "create"]


class FakeMessenger:
    """`stream.Messenger`。`sent` 与 `replied` 分开记，回复语义因此可断言。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[str, OutboundBody]] = []
        self.replied: list[tuple[str, OutboundBody, bool]] = []
        self.fail = fail

    async def send(self, chat_id: str, body: OutboundBody) -> str | None:
        if self.fail:
            return None
        self.sent.append((chat_id, body))
        return f"om_sent_{len(self.sent)}"

    async def reply(self, message_id: str, body: OutboundBody, *, in_thread: bool) -> str | None:
        if self.fail:
            return None
        self.replied.append((message_id, body, in_thread))
        return f"om_reply_{len(self.replied)}"


class FakeReactions:
    """`indicators.Reactions`。可注入失败，用来验「指示器的失败不影响 turn」。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.added: list[tuple[str, str]] = []
        self.removed: list[tuple[str, str]] = []
        self.fail = fail

    async def add_reaction(self, message_id: str, emoji: str) -> str | None:
        if self.fail:
            raise RuntimeError("reaction failed")
        self.added.append((message_id, emoji))
        return f"rid_{len(self.added)}"

    async def remove_reaction(self, message_id: str, reaction_id: str) -> None:
        if self.fail:
            raise RuntimeError("reaction failed")
        self.removed.append((message_id, reaction_id))


class FakeClient(FakeCards, FakeMessenger, FakeReactions):
    """把四个 Protocol 合成一个对象——`FeishuChannel` 拿到的就是这样一个 client。"""

    # boundary: 透传给三个基类的构造参数，形状由它们各自决定
    def __init__(self, *, bot_open_id: str = BOT_OPEN_ID, **kwargs: Any) -> None:
        FakeCards.__init__(self, **kwargs)
        FakeMessenger.__init__(self)
        FakeReactions.__init__(self)
        self._bot_open_id = bot_open_id

    async def bot_open_id(self) -> str:
        return self._bot_open_id

    async def message_text(self, message_id: str) -> str:
        return ""


class FakeGateway:
    """`FeishuGateway` 的替身：不连任何东西，但生命周期方法都在。"""

    def __init__(self) -> None:
        self.connected = 0
        self.closed = 0
        self.http = object()

    async def connect(self) -> None:
        self.connected += 1

    async def close(self) -> None:
        self.closed += 1


class FakeClock:
    """可注入的单调时钟，毫秒。**节流因此是确定的**，用例不必真的等 0.5 秒。"""

    def __init__(self, start: int = 0) -> None:
        self.now = start

    def __call__(self) -> int:
        return self.now

    def advance(self, ms: int) -> None:
        self.now += ms


class FakeWsClient:
    """`lark.ws.Client` 的替身：四个私有属性 + 一个调用顺序表。

    `gateway.close()` 的五步顺序靠它逐条断言——不装 SDK 也能验。
    """

    def __init__(self, *, fail_connect: bool = False) -> None:
        self.order: list[str] = []
        self._auto_reconnect = True
        self.fail_connect = fail_connect

    async def _connect(self) -> None:
        self.order.append("connect")
        if self.fail_connect:
            raise RuntimeError("connect failed")

    async def _disconnect(self) -> None:
        self.order.append("disconnect")

    async def _ping_loop(self) -> None:
        self.order.append("ping")
        import asyncio

        await asyncio.Event().wait()


# ------------------------------------------------------------------ 构造助手


# boundary: 下面四个是关键字转发器，类型检查落在被构造的 dataclass 上
def gate(**kwargs: Any) -> InboundGate:
    """一个默认允许所有人的门控。用例按需收紧。"""
    defaults: dict[str, Any] = {  # boundary: 关键字转发
        "instance_id": InstanceId("test"),
        "channel_id": CHANNEL_ID,
        "bot_open_id": BOT_OPEN_ID,
    }
    defaults.update(kwargs)
    return InboundGate(**defaults)


def raw(**kwargs: Any) -> RawInbound:  # boundary: 同上
    """一条默认可通过的私聊文本消息。"""
    defaults: dict[str, Any] = {  # boundary: 关键字转发
        "message_id": MESSAGE_ID,
        "chat_id": CHAT_ID,
        "chat_type": "p2p",
        "msg_type": "text",
        "sender_id": USER_OPEN_ID,
        "sender_type": "user",
        "content": json.dumps({"text": "在吗"}),
        "create_time": "1700000000000",
    }
    defaults.update(kwargs)
    return RawInbound(**defaults)


def mention(**kwargs: Any) -> Mention:  # boundary: 同上
    # boundary: 构造用的字段包，形状即 `Mention` 的字段
    defaults: dict[str, Any] = {"key": "@_user_1", "name": "机器人", "open_id": BOT_OPEN_ID}
    defaults.update(kwargs)
    return Mention(**defaults)


def outbound(
    content: str = "答案",
    *,
    state: StreamState = StreamState.FINAL,
    conversation: str = CHAT_ID,
    turn: str = "turn-1",
    **kwargs: Any,  # boundary: 同上
) -> OutboundMessage:
    key = SessionKey(channel_id=CHANNEL_ID, conversation_id=conversation)
    return OutboundMessage(
        session_key=key,
        channel_id=key.channel_id,
        conversation_id=key.conversation_id,
        turn_id=TurnId(turn),
        content=content,
        stream_state=state,
        **kwargs,
    )


def settings(**kwargs: Any) -> FeishuSettings:  # boundary: 同上
    defaults: dict[str, Any] = {  # boundary: 关键字转发
        "instance_id": InstanceId("test"),
        "channel_id": CHANNEL_ID,
        "domain": "feishu",
        "allow_from": frozenset(),
        "allow_chats": frozenset(),
        "operators": frozenset(),
        "group_policy": "mention",
        "topic_isolation": True,
        "reply_to_message": False,
        "streaming": True,
        "stream_edit_interval_ms": 500,
        "react_emoji": "THUMBSUP",
        "done_emoji": "",
        "tool_hint_prefix": DEFAULT_PREFIX,
    }
    defaults.update(kwargs)
    return FeishuSettings(**defaults)


def text_content(text: str) -> str:
    """飞书的 `message.content` 是 JSON 字符串。"""
    return json.dumps({"text": text}, ensure_ascii=False)
