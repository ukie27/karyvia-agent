"""CLI 的控制台：stdin/stdout 与消息契约之间的那一层。

职责：把一行输入变成 `InboundMessage` 交给队列，把 `OutboundMessage` 渲染到输出流，
并让读循环知道「这一轮结束了」。
不负责：读 stdin（`entry.py`）、实现 `Channel` 的生命周期（`channel.py`）、构造实例
（`runtime/`）。

**一条输入只有一条路**：`submit()` 产出的 `InboundMessage` 与其它 Channel 产出的完全同型，
CLI 不存在「直接调 orchestrator」的近路。渲染同理只认 `OutboundMessage`。

`is_complete_answer` 为假时**必须**附加标记：被取消的半句、撞上预算上限的
回答与失败的 turn 都会以正文形式到达，不标注就等于把它们呈现成完整答案。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Final, TextIO

from karyvia.contracts import (
    InboundMessage,
    InstanceId,
    OutboundMessage,
    Sender,
    StreamState,
)

__all__ = ["ATTACHMENT_LINE", "CliConsole", "TERMINAL_MARKERS", "attachment_lines"]

#: 附件的呈现。终端里能给出的最好形态就是**路径**：字节印不出来，而
#: workspace 相对路径可以直接喂给 `fs.read`、也可以直接在文件管理器里打开。
#: 不去读字节、不去校验文件在不在——渲染层做 IO 会让「投递」有失败的可能，而
#: `deliver` 约定不抛。
ATTACHMENT_LINE: Final = "[附件] {locator}{size}"

#: 撞上 `MAX_ATTACHMENTS` 时那一行。键名与 `kernel/turn/orchestration.py` 的
#: `DROPPED_ATTACHMENTS_KEY` 逐字相同——`R4` 不让内建 import `kernel/`，因此这里写一份
#: 字面量，由 `tests/builtins/test_cli_entry.py` 的对照测试钉住。
DROPPED_ATTACHMENTS_KEY: Final = "attachments_dropped"
_DROPPED_LINE: Final = "[附件] 另有 {count} 个未随本轮发出（超过上限）"

#: 非完整回答的标记。文案里带上原因，用户才知道该重发还是该改配置。
TERMINAL_MARKERS: Final[dict[StreamState, str]] = {
    StreamState.CANCELLED: "[已中断：以上是中断前已产生的内容]",
    StreamState.FAILED: "[本轮失败]",
}

#: 终态 stream_state，收到即认为这一轮结束。`FINAL` 也覆盖 `STOPPED_BY_LIMIT`
#: （`TERMINAL_STREAM_STATES` 的映射）。
_TERMINAL: Final = (StreamState.FINAL, StreamState.CANCELLED, StreamState.FAILED)


def attachment_lines(message: OutboundMessage) -> list[str]:
    """终帧的附件呈现。没有附件时返回空列表（**不是** `[""]`）。"""
    lines = [
        ATTACHMENT_LINE.format(
            locator=item.locator,
            size="" if item.size_bytes is None else f"（{item.size_bytes} 字节）",
        )
        for item in message.attachments
    ]
    dropped = message.metadata.get(DROPPED_ATTACHMENTS_KEY)
    if isinstance(dropped, int) and dropped > 0:
        lines.append(_DROPPED_LINE.format(count=dropped))
    return lines


class CliConsole:
    """一次 `karyvia run` 的输入队列与输出渲染。

    **队列无界，背压来自读循环**：入口读完一行就等这一轮的终态消息（`entry.py::_await_turn`），
    因此队列里最多躺一条。给它一个上限反而会让 `close()` 在「还有一条没被消费」时抛
    `QueueFull`——那是关闭路径上最不需要的一种失败（这条是测试先发现的）。
    """

    def __init__(
        self,
        *,
        instance_id: InstanceId,
        channel_id: str = "cli",
        conversation_id: str = "local",
        user_id: str = "local",
        out: TextIO | None = None,
        show_reasoning: bool = False,
    ) -> None:
        self.instance_id = instance_id
        self.channel_id = channel_id
        self.conversation_id = conversation_id
        self.user_id = user_id
        self.show_reasoning = show_reasoning
        self._out = out
        self._queue: asyncio.Queue[InboundMessage | None] = asyncio.Queue()
        self._closed = False
        self._counter = 0
        self._streamed = False
        self._turn_done = asyncio.Event()
        #: 最近一轮的终态 stream_state，`karyvia -p` 的退出码由它决定。
        self.last_state: StreamState | None = None
        #: 已渲染过的全部正文，按顺序。测试与 `embed` 拿它断言，不必去抠 stdout。
        self.rendered: list[str] = []

    # ------------------------------------------------------------------ 入站

    def submit(self, content: str, *, timestamp: datetime | None = None) -> InboundMessage:
        """把一行输入变成入站消息并入队。返回它，供调用方记住 `message_id`。"""
        self._counter += 1
        message = InboundMessage(
            message_id=f"cli-{self._counter}",
            instance_id=self.instance_id,
            channel_id=self.channel_id,
            conversation_id=self.conversation_id,
            # 本地用户即实例拥有者：`/config` 这类 `operator_only` 命令在 CLI 上必须可用。
            sender=Sender(user_id=self.user_id, is_operator=True),
            content=content,
            timestamp=timestamp or datetime.now(UTC),
        )
        self._turn_done.clear()
        self._streamed = False
        self.last_state = None
        self._queue.put_nowait(message)
        return message

    def close(self) -> None:
        """结束入站流。幂等——`stop()` 与读循环退出都会调它。"""
        if self._closed:
            return
        self._closed = True
        self._queue.put_nowait(None)

    async def messages(self) -> AsyncIterator[InboundMessage]:
        """产出入站消息，直到 `close()`。这是 `Channel.receive()` 的正文。"""
        while True:
            message = await self._queue.get()
            if message is None:
                return
            yield message

    # ------------------------------------------------------------------ 出站

    async def deliver(self, message: OutboundMessage) -> None:
        """渲染一条出站消息。**约定不抛**：投递失败不该让 turn 变成失败。"""
        if message.metadata.get("reasoning") and not self.show_reasoning:
            return
        if message.stream_state is StreamState.DELTA:
            self._streamed = True
            self._write(message.content)
            return
        if message.stream_state in _TERMINAL:
            self._finish(message)

    def _finish(self, message: OutboundMessage) -> None:
        # 流式已经把正文逐片打过了，终态消息带的是同一段完整正文——再打一遍就是重复。
        if not self._streamed and message.content:
            self._write(message.content)
        marker = TERMINAL_MARKERS.get(message.stream_state)
        if marker is not None:
            self._write(("\n" if self._streamed or message.content else "") + marker)
        self._write("\n")
        for line in attachment_lines(message):
            self._write(line + "\n")
        self.rendered.append(message.content)
        self.last_state = message.stream_state
        self._turn_done.set()

    def _write(self, text: str) -> None:
        """写一段文本。**编不出来的字符降级而不是让这一轮失败**。

        Windows 中文控制台的默认编码是 GBK，而模型的输出里随时可能有 emoji 或
        `»` 这种字符——`sys.stdout.write` 会当场抛 `UnicodeEncodeError`，把一次正常的
        回答变成一条 traceback。降级成转义序列难看，但答案还在（同一条精神）。
        """
        if self._out is None:
            return
        try:
            self._out.write(text)
        except UnicodeEncodeError:
            encoding = getattr(self._out, "encoding", None) or "utf-8"
            self._out.write(text.encode(encoding, errors="backslashreplace").decode(encoding))
        self._out.flush()

    def notice(self, text: str) -> None:
        """打一条不属于任何 turn 的提示（拒绝、去重、Ctrl-C 说明）。"""
        self._write(f"{text}\n")

    def notice_prompt(self, text: str) -> None:
        """打提示符。不换行——用户接着在同一行输入。"""
        self._write(text)

    def turn_rejected(self, reason: str) -> None:
        """一条没有终态事件的 turn（去重或被队列拒）。读循环仍要能继续。"""
        self.notice(reason)
        self.last_state = None
        self._turn_done.set()

    async def wait_for_turn(self) -> None:
        await self._turn_done.wait()
