"""CardKit 流式 edit-in-place 的状态机。

职责：把一串 `STARTED` / `DELTA` / 终态出站消息变成「建一张流式卡片、按节流反复更新它、
最后关掉流式模式」的平台动作序列；全部失败路径的回落。
不负责：卡片元素构建（`cards.py`）、格式判定（`outbound.py`）、真的调 SDK（`Cards` 与
`Messenger` 由调用方注入）。

**sequence 的唯一规则**——本模块最容易写错的一条：

> 一张卡一个计数器，**内容更新（`card_element.content`）与设置变更（`card.settings`）
> 共用它**；每次调用**之前** `+1`，**无论上一次成功还是失败**。

失败也 bump 的理由：失败可能发生在平台已经消费掉那个 sequence **之后**（超时、连接断在
响应回来之前），复用同一个数会让后续全部更新被判为「非严格递增」而**永久**失败。
飞书只要求严格递增，不要求连续，跳号是安全的。

**streaming_mode 必须显式关掉**，否则会话列表会**永久**显示「生成中」。全部五个出口：
终态更新成功 / 终态更新失败 / DELTA 首次更新失败 / DELTA 节流更新失败 /
**`Channel.stop()`**（最后这条 legacy 没有，是本插件新增——实例被 Ctrl-C 时留着的卡片
否则会一直卡在那里）。

**注入时钟**：节流是本模块唯一的时间语义，用真实时间测它意味着每个用例都要等 0.5 秒。

**缓冲表按 conversation 分片，不加锁**： 的泵按 conversation 扇出、lane 内串行，
而 `conversation_id ↔ SessionKey` 是双射（`normalize.encode_conversation` 是可逆纯函数），
因此各条 lane 只碰自己那个键。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

from karyvia.contracts import (
    ErrorCode,
    JsonValue,
    KaryviaError,
    OutboundMessage,
    StreamState,
)

from .cards import build_elements, card_payload, split_by_table_limit
from .outbound import FORMAT_INTERACTIVE, OutboundBody, compose_body, detect_format, plan_simple

__all__ = [
    "DEFAULT_EDIT_INTERVAL_MS",
    "STREAM_ELEMENT_ID",
    "Cards",
    "Messenger",
    "StreamRelay",
]

#: 两次更新之间的最小间隔。legacy 的 `_STREAM_EDIT_INTERVAL = 0.5`，只换了单位。
DEFAULT_EDIT_INTERVAL_MS: Final = 500

#: 流式卡片里那个唯一的 markdown 元素的 id。更新时按它定位。
STREAM_ELEMENT_ID: Final = "streaming_md"

#: 一条都没发出去时的用户可见文案。模块级常量：`ruff` 的 `TRY003` 不允许在 `raise`
#: 处写多词消息。
_SEND_FAILED: Final = "飞书拒收了这条消息。"


class Cards(Protocol):
    """CardKit 的四个调用。`client.py` 提供实现。

    **全部返回 `bool` / `str | None` 而不是抛异常**：流式的每一步都有回落路径，
    用异常表达会让 `handle()` 变成一棵 try/except 树。
    """

    async def create(self) -> str | None:
        """建一张流式卡片，返回 `card_id`；失败返回 `None`。"""
        ...

    async def update(self, card_id: str, content: str, sequence: int) -> bool: ...

    async def set_streaming(self, card_id: str, enabled: bool, sequence: int) -> bool: ...


class Messenger(Protocol):
    """发消息的两条路。`client.py` 提供实现。"""

    async def send(self, chat_id: str, body: OutboundBody) -> str | None:
        """发一条消息，返回 `message_id`；失败返回 `None`。"""
        ...

    async def reply(
        self, message_id: str, body: OutboundBody, *, in_thread: bool
    ) -> str | None: ...


@dataclass(slots=True)
class _Card:
    """一个 conversation 的流式缓冲。"""

    turn_id: str
    text: str = ""
    card_id: str | None = None
    sequence: int = 0
    last_edit_ms: int = 0
    #: 建卡或更新彻底失败过。终态因此直接走普通卡片，不再试流式。
    degraded: bool = False


@dataclass(slots=True)
class StreamRelay:
    """把出站流翻成平台动作。每 conversation 一份缓冲。"""

    cards: Cards
    messenger: Messenger
    now_ms: Callable[[], int]
    #: `conversation_id` → `chat_id`（出站寻址）与可选的回复目标。**只吃 conversation_id
    #: 而不是整条 `OutboundMessage`**：`note()` 送进来的进度提示没有对应的出站消息，
    #: 而寻址本来就只需要这一个键。
    resolve_target: Callable[[str], tuple[str, str | None, bool]]
    edit_interval_ms: int = DEFAULT_EDIT_INTERVAL_MS
    streaming: bool = True
    _buffers: dict[str, _Card] = field(default_factory=dict)

    def active(self) -> int:
        """当前有缓冲的 conversation 数。诊断用，也是「没有泄漏」的可断言量。"""
        return len(self._buffers)

    async def handle(self, message: OutboundMessage) -> None:
        """处理一条出站消息。

        ** 起它可以抛 `EXTERNAL_CHANNEL`**：`_send_plain` 一条都没发出去时抛，
        由 `channel.py` 原样带给出站路由点。其余失败（卡片更新、关流）仍然内部降级——
        那些路径上还有别的出路，而「一条都没发出去」没有。
        """
        if message.stream_state is StreamState.STARTED:
            # 只登记，不发任何东西——空卡片在会话列表里是噪声。
            self._buffers[message.conversation_id] = _Card(turn_id=str(message.turn_id))
            return
        if message.stream_state is StreamState.DELTA:
            await self._absorb(message)
            return
        await self._finish(message)

    def note(self, conversation_id: str, turn_id: str, text: str) -> bool:
        """把一块进度提示并进流式缓冲。**同步**，返回是否真的写进去了。

        同步是刻意的：事件订阅者的签名是 `Callable[[RuntimeEvent], None]`，而提示与模型
        增量必须按**到达顺序**落进同一段文本。把这次追加做成异步等于把顺序交给事件循环的
        ready 队列——`kernel/routing/fanout.py` 已经为拒绝依赖那个顺序付过一次钱。真正要
        `await` 的只有那次网络更新，它在 `flush()` 里。

        **`turn_id` 对不上就丢弃**：一条迟到的提示落进下一轮的卡片，比不显示它更糟。
        """
        if not text:
            return False
        buffer = self._buffers.get(conversation_id)
        if buffer is None or buffer.turn_id != turn_id:
            return False
        # 前后各留一个空行，逐字沿用 legacy `runtime.py:2465`：不留白会让 markdown 把
        # 提示和正文渲染成同一段。
        buffer.text += f"\n\n{text}\n\n"
        return True

    async def flush(self, conversation_id: str) -> None:
        """把 `note()` 写进去的内容推到卡片上。**约定不抛**由调用方（`channel.py`）负责。"""
        buffer = self._buffers.get(conversation_id)
        if buffer is not None:
            await self._pump(conversation_id, buffer)

    async def shutdown(self) -> None:
        """把所有还开着的流式卡片关掉。**`Channel.stop()` 调用一次。**

        legacy 没有这一条：实例被 Ctrl-C 时留着的卡片会在飞书的会话列表里**永久**显示
        「生成中」，而那是一个用户无法自行清除的状态。
        """
        for buffer in list(self._buffers.values()):
            if buffer.card_id is not None:
                buffer.sequence += 1
                await self.cards.set_streaming(buffer.card_id, False, buffer.sequence)
        self._buffers.clear()

    # ------------------------------------------------------------------ 内部

    def _buffer_for(self, message: OutboundMessage) -> _Card:
        """取（或换）缓冲。`turn_id` 变了就换——旧卡片原样留在飞书里，它自己的终态已经
        带过  标记了，在这里再补一条等于把同一件事说两遍。"""
        buffer = self._buffers.get(message.conversation_id)
        if buffer is None or buffer.turn_id != str(message.turn_id):
            buffer = _Card(turn_id=str(message.turn_id))
            self._buffers[message.conversation_id] = buffer
        return buffer

    async def _update_with_reopen(self, buffer: _Card, content: str) -> bool:
        """更新内容；失败就重开 streaming 再试一次。

        存在理由：**飞书会在超时之后自己把卡片的 streaming_mode 关掉**，此后内容更新一律
        失败。不 reopen 的话一次网络抖动就会让这张卡永久停止更新。
        """
        card_id = buffer.card_id
        if card_id is None:
            return False
        buffer.sequence += 1
        if await self.cards.update(card_id, content, buffer.sequence):
            return True
        buffer.sequence += 1
        if not await self.cards.set_streaming(card_id, True, buffer.sequence):
            return False
        buffer.sequence += 1
        return await self.cards.update(card_id, content, buffer.sequence)

    async def _close_streaming(self, buffer: _Card) -> bool:
        if buffer.card_id is None:
            return True
        buffer.sequence += 1
        return await self.cards.set_streaming(buffer.card_id, False, buffer.sequence)

    async def _absorb(self, message: OutboundMessage) -> None:
        buffer = self._buffer_for(message)
        buffer.text += message.content
        await self._pump(message.conversation_id, buffer)

    async def _pump(self, conversation_id: str, buffer: _Card) -> None:
        """把 `buffer.text` 的当前内容推到卡片上（受节流约束）。

        `_absorb`（模型增量）与 `flush`（工具提示）共用它——两者的差别只在**谁往
        `buffer.text` 里写**，写完之后要做的事一模一样。
        """
        if not self.streaming or buffer.degraded:
            # 关掉流式（或已经降级）就只累积，终态时一次发完（降级）。
            return
        if buffer.card_id is None:
            if not buffer.text.strip():
                # 首片全是空白：现在建卡会在会话里挂一张空卡片。
                return
            await self._open(conversation_id, buffer)
            return
        now = self.now_ms()
        if now - buffer.last_edit_ms < self.edit_interval_ms:
            # 被节流跳过的文本**不丢**：它留在 buffer.text 里，下次更新或收束带上。
            return
        if await self._update_with_reopen(buffer, buffer.text):
            buffer.last_edit_ms = now
            return
        await self._close_streaming(buffer)
        buffer.card_id = None
        buffer.degraded = True

    async def _open(self, conversation_id: str, buffer: _Card) -> None:
        """建卡 + 把它作为一条 `interactive` 消息发出去 + 第一次更新。"""
        card_id = await self.cards.create()
        if card_id is None:
            buffer.degraded = True
            return
        chat_id, reply_to, in_thread = self.resolve_target(conversation_id)
        body = OutboundBody("interactive", _card_reference(card_id))
        sent = (
            await self.messenger.reply(reply_to, body, in_thread=in_thread)
            if reply_to
            else await self.messenger.send(chat_id, body)
        )
        if sent is None:
            # 卡建出来了但没发出去：它不在任何会话里，关不关都没人看得见。
            buffer.degraded = True
            return
        buffer.card_id = card_id
        if await self._update_with_reopen(buffer, buffer.text):
            buffer.last_edit_ms = self.now_ms()
            return
        await self._close_streaming(buffer)
        buffer.degraded = True

    async def _finish(self, message: OutboundMessage) -> None:
        buffer = self._buffers.pop(message.conversation_id, None)
        # 终态消息带的是完整正文；只有它为空时才回退到累积的 delta（`CANCELLED` /
        # `FAILED` 允许空正文，`FINAL` 在契约构造时就不许为空）。
        body = compose_body(
            message.content or (buffer.text if buffer else ""),
            message.attachments,
            message.stream_state,
        )
        if buffer is not None and buffer.card_id is not None:
            updated = await self._update_with_reopen(buffer, body)
            closed = await self._close_streaming(buffer)
            if not closed:
                # 关不掉就再试一次——不关会让会话列表永久显示「生成中」。
                await self._close_streaming(buffer)
            if updated:
                return
        await self._send_plain(message, body)

    async def _send_plain(self, message: OutboundMessage, body: str) -> None:
        """不走流式的那条路：按格式级联发 text / post，或拆成一到多张卡片。

        **一条都没发出去就抛 `EXTERNAL_CHANNEL`**。这是最后一条出路——走到这里
        说明流式卡片那条路没成功（或压根没开），因此它失败就等于这条答案一个字都没送到。
        飞书的失败信号是 `None` 返回值而不是异常（`client.py` 的四个方法都是这样），
        因此判据是返回值：不看它，一次失败的投递在事件流里连一条记录都留不下。

        **部分成功不算失败**：一段长正文被拆成多条，前两条到了第三条没到，用户看到的是
        半截答案而不是什么都没有——那不该被记成「答案发不出去」。
        """
        if not body:
            return
        chat_id, reply_to, in_thread = self.resolve_target(message.conversation_id)
        sent = 0
        planned = _plan_bodies(body)
        for index, item in enumerate(planned):
            target = reply_to if (reply_to and (index == 0 or in_thread)) else None
            if target:
                sent += bool(await self.messenger.reply(target, item, in_thread=in_thread))
                continue
            sent += bool(await self.messenger.send(chat_id, item))
        if planned and not sent:
            raise KaryviaError(
                ErrorCode.EXTERNAL_CHANNEL,
                _SEND_FAILED,
                detail={"conversation": message.conversation_id, "parts": len(planned)},
                retryable=True,
            )


def _card_reference(card_id: str) -> str:
    """把一个 `card_id` 包成 `interactive` 消息体。"""
    return json.dumps({"type": "card", "data": {"card_id": card_id}}, ensure_ascii=False)


def _plan_bodies(body: str) -> list[OutboundBody]:
    """一段正文 → 一到多条待发消息。

    卡片格式要先拆元素、再按「一表一卡」分组，因此可能是多条；text / post 恒为一条。
    """
    if detect_format(body) != FORMAT_INTERACTIVE:
        return [plan_simple(body)]
    groups = split_by_table_limit(build_elements(body))
    return [
        OutboundBody(
            FORMAT_INTERACTIVE, json.dumps(card_payload(group), ensure_ascii=False)
        )
        for group in groups
        if group
    ]


def elements_of(body: str) -> Sequence[dict[str, JsonValue]]:
    """诊断与用例用：这段正文会被拆成哪些卡片元素。"""
    return build_elements(body)
