"""`contracts.Channel` 的飞书实现。

职责：`channel_id` / `start` / `stop` / `receive` / `deliver` 五个成员，以及把 gateway、
归一化、流式与指示器组装到一起；维护「每 conversation 最后一条入站消息」表。
不负责：归一化（`normalize.py`）、渲染（`outbound.py` / `cards.py`）、流式时机
（`stream.py`）、指示器生命周期（`indicators.py`）、接触 SDK（`gateway.py` / `client.py`）。

**`_last_inbound` 表存在的理由**：新层仅有的两个 `OutboundMessage` 构造点
（`kernel/turn/orchestration.py::emit_outbound` 与 `runtime/instance.py::_rejection`）
**都不设 `reply_to`**。而飞书的话题**必须调 Reply API 才留得住**——不回复就会另起一条
消息、掉出话题。因此 Channel 自己记住每个 conversation 最后一条入站消息的 id。
表有界（`OrderedDict` 上限 500）：一个跑几个月的实例不该把每条消息都留着。

**`reply_in_thread` 的不变量**（最容易回归的一条）：它只在配置显式开了
`reply_to_message` 时才为真。否则「回复到一个已有话题」会让飞书**新建**一个话题——
用户会看到自己的对话被切成一串互不相干的小话题。

**`deliver` 照约定抛 `EXTERNAL_CHANNEL`**：出站路由点
（`runtime/instance.py::outbound_router`）捕获它、发一条 `channel.delivery_failed`，
turn 照样走到自己的终态。在此之前这里把投递故障整个吞掉，于是「答案发不出
去」在事件流里一个字都没有——用户看到的现象是 bot 不说话，而日志一片正常。
**只有正文的投递会抛；指示器与工具提示仍然静默**（`_quietly`）：一个没清掉的「正在输入」
是外观问题，而一条没发出去的答案不是。

**工具提示走事件订阅而不是出站流**：`OutboundMessage` 只有 `content` 与 `metadata`，
不带工具调用信息，因此提示的唯一来源是 `tool.call_started`（`ctx.events`）。订阅者签名是
同步的 `Callable[[RuntimeEvent], None]` 且**连续 5 次抛异常会被 Kernel 自动退订**，所以
回调只做两件事：判定与 `put_nowait`。真正的网络更新在 `_hint_pump` 那条后台任务里。
队列**有界且满了就丢**——工具提示是锦上添花，为它把入站事件流堵住是本末倒置。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Any, Final

from karyvia.contracts import (
    ErrorCode,
    InboundMessage,
    KaryviaError,
    OutboundMessage,
    RuntimeEvent,
    SecretStr,
    StreamState,
)

from .client import FeishuClient
from .gateway import FeishuGateway, event_to_raw
from .indicators import Indicators
from .normalize import decode_conversation, normalize
from .settings import FeishuSettings
from .stream import StreamRelay
from .tool_hints import render

__all__ = ["FeishuChannel"]

#: 这些状态表示「这条 turn 说完了」，指示器该清掉。**`DELTA` 不在其中**——模型还在说话时
#: 拆掉指示器会让它闪一下又回来（legacy `runtime.py:479` 那条坑的新家在这里）。
_TERMINAL: Final[frozenset[StreamState]] = frozenset(
    {StreamState.FINAL, StreamState.CANCELLED, StreamState.FAILED}
)

#: 推理增量的元数据标记。`show_reasoning` 关掉时整条丢弃。
_REASONING: Final = "reasoning"

#: 投递失败的用户可见文案。定义成模块级常量：`ruff` 的 `TRY003` 不允许在 `raise` 处
#: 写多词消息。
_DELIVERY_FAILED: Final = "出站消息投递失败。"

#: `conversation_id → 最后一条入站 message_id` 表的容量。
_INBOUND_TABLE_CAPACITY: Final = 500

#: 工具提示队列的上界。满了就丢——见模块 docstring 的最后一段。取 256 是因为一条 turn
#: 里几百次工具调用已经属于异常，再大只会让丢弃发生得更晚而不是更少。
_HINT_QUEUE_CAPACITY: Final = 256


class FeishuChannel:
    """一条飞书 Channel。`setup()` 构造它，装配根负责 `start()` / `stop()`。"""

    __slots__ = (
        "_client",
        "_gate",
        "_gateway",
        "_hint_pump",
        "_hints",
        "_inbox",
        "_indicators",
        "_last_inbound",
        "_relay",
        "_settings",
        "_show_reasoning",
        "_started",
    )

    def __init__(
        self,
        settings: FeishuSettings,
        *,
        app_id: SecretStr,
        app_secret: SecretStr,
        gateway: FeishuGateway | None = None,
        client: Any = None,  # boundary: 测试注入的假 client，生产由 gateway 建
        show_reasoning: bool = False,
    ) -> None:
        self._settings = settings
        self._show_reasoning = show_reasoning
        self._started = False
        self._inbox: asyncio.Queue[InboundMessage | None] = asyncio.Queue()
        # **门控只建一次并复用**：它持有去重表，每次新建会让的第一道防线失效。
        self._gate = settings.gate()
        self._last_inbound: OrderedDict[str, str] = OrderedDict()
        #: `(conversation_id, turn_id, 工具名)`。回调只往里放，`_hint_pump` 只从里取。
        self._hints: asyncio.Queue[tuple[str, str, str]] = asyncio.Queue(
            maxsize=_HINT_QUEUE_CAPACITY
        )
        self._hint_pump: asyncio.Task[None] | None = None
        self._gateway = gateway or FeishuGateway(
            app_id=app_id,
            app_secret=app_secret,
            domain=settings.domain,
            on_event=self._on_event,
        )
        self._client = client
        self._relay = StreamRelay(
            cards=client,
            messenger=client,
            now_ms=lambda: int(asyncio.get_running_loop().time() * 1000),
            resolve_target=self._target_for,
            edit_interval_ms=settings.stream_edit_interval_ms,
            streaming=settings.streaming,
        )
        self._indicators = Indicators(
            reactions=client,
            react_emoji=settings.react_emoji,
            done_emoji=settings.done_emoji,
        )

    # ------------------------------------------------------------------ 契约成员

    @property
    def channel_id(self) -> str:
        """稳定标识，构成 `SessionKey.channel_id`，也是出站路由键。"""
        return self._settings.channel_id

    async def start(self) -> None:
        """连上 WS 并取一次 bot 自己的 open_id。

        **异常约定**：连接失败抛 `EXTERNAL_CHANNEL`（见 `gateway.py`）。
        **取 open_id 失败只是降级**：群聊 @ 门控有兜底启发式（`mentions.py`），
        为它让整条 Channel 起不来是过度反应。
        """
        if self._started:
            return
        self._started = True
        if self._settings.tool_hint_prefix:
            self._hint_pump = asyncio.create_task(self._drain_hints(), name="feishu:tool-hints")
        if self._client is None:
            await self._gateway.connect()
            self._client = FeishuClient(raw=self._gateway.http)
            self._relay.cards = self._client
            self._relay.messenger = self._client
            self._indicators.reactions = self._client
        self._gate.bot_open_id = await self._client.bot_open_id()

    async def stop(self) -> None:
        """断开并结束入站流。**幂等且不抛**。

        **先关流式卡片再断连**：关卡片要走 HTTP，断连之后那条路就没了，留着的卡片会在
        飞书的会话列表里永久显示「生成中」。
        """
        if not self._started:
            self._inbox.put_nowait(None)
            return
        self._started = False
        await self._stop_pump()
        await self._quietly(self._relay.shutdown())
        await self._quietly(self._indicators.shutdown())
        await self._quietly(self._gateway.close())
        self._inbox.put_nowait(None)

    async def receive(self) -> AsyncIterator[InboundMessage]:
        """入站流。`stop()` 之后结束。**拉模型**（契约原文）。"""
        while True:
            message = await self._inbox.get()
            if message is None:
                return
            yield message

    async def deliver(self, message: OutboundMessage) -> None:
        """投递一条出站消息。

        **正文失败抛 `EXTERNAL_CHANNEL`**（见模块 docstring）；指示器的清理仍然
        静默——它在正文之后，而一次失败的清理不该盖掉「正文已经送到了」。
        """
        if not self._show_reasoning and message.metadata.get(_REASONING):
            return
        await self._relayed(message)
        if message.stream_state in _TERMINAL:
            # 只在终态清指示器。判断留在这里而不是 `Indicators` 里——那个模块不该猜
            # 什么算「说完了」。
            await self._quietly(self._indicators.stop(message.conversation_id))

    # ------------------------------------------------------------------ 工具提示

    def on_tool_call(self, event: RuntimeEvent) -> None:
        """`tool.call_started` 的订阅者。**同步且不抛**（`sdk.api.EventSubscriber`当前约定）。

        `setup()` 把它挂到 `ctx.events` 上。这里只做判定与入队：Kernel 在**发布事件的
        同一个栈**里调它，任何 `await` 都会把 turn 的执行卡在这条回调上。

        **按 `channel_id` 过滤**：事件总线是实例级的，一条飞书 Channel 会看到 CLI、
        HTTP API 与其它 Channel 的全部工具调用。`correlation.session_key` 带着寻址三件套
        （`contracts/ids.py`），拿它比对即可。
        """
        correlation = event.correlation
        if correlation is None or correlation.session_key.channel_id != self.channel_id:
            return
        tool = event.payload.get("tool")
        if not isinstance(tool, str) or not tool:
            return
        entry = (correlation.session_key.conversation_id, str(correlation.turn_id), tool)
        with contextlib.suppress(asyncio.QueueFull):
            # 满了就丢：工具提示是锦上添花，为它把事件发布堵住是本末倒置。
            self._hints.put_nowait(entry)

    async def _drain_hints(self) -> None:
        """把队列里的提示批量渲染进流式卡片。`start()` 派生，`stop()` 取消。

        **成批取而不是一条一条取**是折叠 `× N` 的前提：一次并行发起的五个 `fs.read` 会在
        同一个 tick 里进队列，攒在一起才折得成一行（`tool_hints.render` 只折叠相邻同名）。
        """
        while True:
            first = await self._hints.get()
            batch = [first]
            while True:
                try:
                    batch.append(self._hints.get_nowait())
                except asyncio.QueueEmpty:
                    break
            for conversation_id, turn_id, names in _group(batch):
                text = render(self._settings.tool_hint_prefix, names)
                if self._relay.note(conversation_id, turn_id, text):
                    await self._quietly(self._relay.flush(conversation_id))

    async def _stop_pump(self) -> None:
        """取消提示泵并等它收尸。**幂等且不抛**。"""
        pump, self._hint_pump = self._hint_pump, None
        if pump is None:
            return
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump

    # ------------------------------------------------------------------ 内部

    def _target_for(self, conversation_id: str) -> tuple[str, str | None, bool]:
        """出站寻址：`(chat_id, 回复目标, 是否进话题)`。

        `chat_id` 从 `conversation_id` 还原（`decode_conversation` 是 `encode` 的逆运算）
        ——这就是那个合成必须可逆的原因。
        """
        chat_id, topic = decode_conversation(conversation_id)
        reply_to = self._last_inbound.get(conversation_id)
        if reply_to is None:
            return chat_id, None, False
        # **只有配置显式开了 `reply_to_message` 才允许新建话题**，见模块 docstring。
        # 已经在话题里（`topic` 非空）时回复不会新建，因此那一侧不受这条约束。
        in_thread = self._settings.reply_to_message and topic is None
        return chat_id, reply_to, in_thread

    def _remember_inbound(self, conversation_id: str, message_id: str) -> None:
        self._last_inbound[conversation_id] = message_id
        self._last_inbound.move_to_end(conversation_id)
        while len(self._last_inbound) > _INBOUND_TABLE_CAPACITY:
            self._last_inbound.popitem(last=False)

    async def _on_event(self, event: Any) -> None:  # boundary: lark 的事件对象
        """gateway 的回调。**一条消息炸掉不该让 bot 下线**。"""
        try:
            raw = event_to_raw(event)
            message = normalize(raw, self._gate)
        except Exception:  # noqa: BLE001 - 畸形消息丢弃并继续
            return
        if message is None:
            return
        self._remember_inbound(message.conversation_id, raw.message_id)
        # 指示器在**进队列之前**打上：用户发完消息看到的第一个反馈不该等到 turn 真的
        # 开始跑（那可能要排队）。
        await self._quietly(self._indicators.start(message.conversation_id, raw.message_id))
        self._inbox.put_nowait(message)

    async def _relayed(self, message: OutboundMessage) -> None:
        """把正文交给 relay，失败折成 `EXTERNAL_CHANNEL`。

        **只放异常类型名不放消息**：lark-oapi 的异常文本可能带着完整请求 URL 与令牌。
        `retryable=True`——投递失败几乎总是网络或限流，重发一条聊天消息的代价是可能重复，
        不是不可逆。
        """
        try:
            await self._relay.handle(message)
        except KaryviaError:
            raise
        except Exception as exc:
            raise KaryviaError(
                ErrorCode.EXTERNAL_CHANNEL,
                _DELIVERY_FAILED,
                detail={"conversation": message.conversation_id, "cause": type(exc).__name__},
                retryable=True,
            ) from exc

    @staticmethod
    # boundary: 任意 awaitable；这里只负责跑它并吞异常
    async def _quietly(awaitable: Any) -> None:
        """跑一件投递/清理，异常只吞不抛。`BaseException`（取消）放行。"""
        try:
            await awaitable
        except Exception:  # noqa: BLE001 - 见模块 docstring 的最后一条
            return


def _group(batch: list[tuple[str, str, str]]) -> list[tuple[str, str, list[str]]]:
    """把一批提示按**相邻**的 `(conversation, turn)` 分组，组内保序。

    刻意不按会话聚合成字典：那会把「A 调了工具、B 调了工具、A 又调了一次」重排成
    「A 两次、B 一次」，而顺序正是用户判断 agent 在干什么的线索。
    """
    groups: list[tuple[str, str, list[str]]] = []
    for conversation_id, turn_id, tool in batch:
        if groups and groups[-1][0] == conversation_id and groups[-1][1] == turn_id:
            groups[-1][2].append(tool)
            continue
        groups.append((conversation_id, turn_id, [tool]))
    return groups
