"""请求与 turn 的接缝：在途请求的登记处（`cli_entry/console.py` 的同位物）。

职责：把一次 HTTP 请求登记成一个 `_Waiter`，产出 `InboundMessage` 交给 Channel 的入站
队列，并把 `deliver()` 收到的 `OutboundMessage` 投给对应的等待者。
不负责：HTTP 协议（`http.py`）、Channel 生命周期（`channel.py`）、执行 turn（Kernel）。

**先登记、后提交**：`open()` 必须在 `submit()` 之前调用。反过来写会丢掉第一片增量——
Channel 泵可能在提交返回之前就已经把 delta 投递回来了。

**关联靠「同一 conversation 的最老等待者」而不是靠 turn_id 匹配**：第一条投递到达时
我们还不知道 turn_id（它由 orchestrator 在准入之后分配），而
`SessionScheduler` 保证同一 session 的严格 FIFO，因此「最老的那个」
一定就是当前正在跑的那个。turn_id 从第一条投递里学到之后记在等待者上，供
断连时取消用。

** 的泵扇出没有动摇这条**，这一点值得写下来而不是让下一个人重新推一遍：扇出是按
`conversation_id` 分 lane 的，而 `_waiting` 也是按 `conversation_id` 索引的 deque——
两条并发 turn 只可能来自不同 conversation，因此永远不碰同一个 deque；同 conversation 内
仍由 lane 与 scheduler 双重串行。本模块因此一行都没改。
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Final

from karyvia.contracts import (
    InboundMessage,
    KaryviaError,
    OutboundMessage,
    Sender,
    StreamState,
    TurnControl,
    TurnId,
)
from karyvia.sdk import PluginContext

from .settings import ApiSettings
from .usage import UsageTracker

__all__ = ["TERMINAL_STATES", "SessionHub", "Waiter"]

#: 终态：收到即这一轮结束。`FINAL` 也覆盖 `STOPPED_BY_LIMIT`（映射）。
TERMINAL_STATES: Final = (StreamState.FINAL, StreamState.CANCELLED, StreamState.FAILED)


class Waiter:
    """一次在途请求。它是 HTTP 处理器与 Channel 投递之间的唯一共享状态。"""

    __slots__ = ("_done", "_queue", "conversation_id", "turn_id")

    def __init__(self, conversation_id: str) -> None:
        self.conversation_id = conversation_id
        #: 从第一条投递里学到。断连时用它请求取消（`ctx.turns` 的  门面）。
        self.turn_id: TurnId | None = None
        self._queue: asyncio.Queue[OutboundMessage | None] = asyncio.Queue()
        self._done = False

    def push(self, message: OutboundMessage) -> None:
        if self.turn_id is None:
            self.turn_id = message.turn_id
        self._queue.put_nowait(message)
        if message.stream_state in TERMINAL_STATES:
            self._done = True
            self._queue.put_nowait(None)

    @property
    def finished(self) -> bool:
        return self._done

    def abandon(self) -> None:
        """放弃等待（超时或客户端断开）。幂等。"""
        if self._done:
            return
        self._done = True
        self._queue.put_nowait(None)

    async def stream(self, *, timeout_ms: int) -> AsyncIterator[OutboundMessage]:
        """产出这一轮的全部出站消息，终态之后结束。

        超时是**兜底**而不是主要机制：真正的上限是 `turn.turn_timeout_ms`，而被拒的
        turn 由装配根合成一条 `FAILED` 出站消息（`runtime/instance.py::_rejection`），
        因此正常路径上不存在无限等待。这里防的是 Channel 层面本身出岔子。
        """
        deadline = timeout_ms / 1000
        while True:
            try:
                message = await asyncio.wait_for(self._queue.get(), timeout=deadline)
            except TimeoutError:
                self.abandon()
                return
            if message is None:
                return
            yield message


class SessionHub:
    """一个 Channel 的在途请求登记处。"""

    def __init__(self, settings: ApiSettings, *, ctx: PluginContext | None = None) -> None:
        self.settings = settings
        #: 插件上下文。取消门面**只能惰性取**——`ctx.turns` 在 `setup()` 期间不可用
        #: （orchestrator 那时还没装好，`runtime/plugin_context.py` 会抛
        #: `KERNEL_INVARIANT_VIOLATED`）。这条是测试先发现的。
        self._ctx = ctx
        #: 用量登记（`usage.py`）。放在 hub 上是因为它与在途请求同生命周期，
        #: 而 HTTP 处理器只拿得到 hub。
        self.usage = UsageTracker()
        self._inbound: asyncio.Queue[InboundMessage | None] = asyncio.Queue()
        self._waiting: dict[str, deque[Waiter]] = {}
        self._counter = 0
        self._closed = False

    @property
    def turns(self) -> TurnControl | None:
        """取消门面。实例还没装好时返回 `None`（`setup()` 期间就是这种情况）。"""
        if self._ctx is None:
            return None
        try:
            return self._ctx.turns
        except KaryviaError:
            return None

    # ------------------------------------------------------------------ 入站

    def open(self, conversation_id: str) -> Waiter:
        waiter = Waiter(conversation_id)
        self._waiting.setdefault(conversation_id, deque()).append(waiter)
        return waiter

    def submit(self, conversation_id: str, content: str, *, user_id: str) -> InboundMessage:
        """把一次请求变成入站消息并入队。

        **`is_operator=False`**：HTTP 调用方不是实例拥有者，`/config` 这类
        `operator_only` 命令因此对它不可用。`cli-entry` 传 `True` 是因为坐在终端前的
        人就是拥有者——两者的差别是真的，不是抄漏了。
        """
        self._counter += 1
        message = InboundMessage(
            message_id=f"api-{self._counter}",
            instance_id=self.settings.instance_id,
            channel_id=self.settings.channel_id,
            conversation_id=conversation_id,
            sender=Sender(user_id=user_id, is_operator=False),
            content=content,
            timestamp=datetime.now(UTC),
        )
        self._inbound.put_nowait(message)
        return message

    def discard(self, waiter: Waiter) -> None:
        """请求结束后摘掉登记。**不取消 turn**——那是调用方的决定。"""
        pending = self._waiting.get(waiter.conversation_id)
        if pending is None:
            return
        try:
            pending.remove(waiter)
        except ValueError:
            pass
        if not pending:
            self._waiting.pop(waiter.conversation_id, None)

    def close(self) -> None:
        """结束入站流并让全部在途请求收尾。幂等。"""
        if self._closed:
            return
        self._closed = True
        self._inbound.put_nowait(None)
        for pending in list(self._waiting.values()):
            for waiter in list(pending):
                waiter.abandon()

    async def messages(self) -> AsyncIterator[InboundMessage]:
        """`Channel.receive()` 的正文。"""
        while True:
            message = await self._inbound.get()
            if message is None:
                return
            yield message

    # ------------------------------------------------------------------ 出站

    def route(self, message: OutboundMessage) -> None:
        """把一条出站消息投给对应的等待者。找不到就丢弃——**不抛**。

        找不到是正常的：请求已经断开、或者这条 turn 根本不是由 HTTP 发起的
        （同一实例上 `cli` 与 `embed` 的 turn 不会路由到这里，但被合并的批次可能带来
        意外的 turn_id）。让 `deliver()` 因此抛出会让 Kernel 记一次投递失败。
        """
        pending = self._waiting.get(message.conversation_id)
        if not pending:
            return
        for waiter in pending:
            if waiter.turn_id == message.turn_id:
                waiter.push(message)
                return
        for waiter in pending:
            if waiter.turn_id is None and not waiter.finished:
                waiter.push(message)
                return
