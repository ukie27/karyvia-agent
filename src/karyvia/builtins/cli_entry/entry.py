"""CLI 入口能力：读 stdin、驱动一轮轮对话、返回进程退出码（`BAS-009`、技术方案 §8.1）。

职责：解析 `karyvia run` 之后的参数，跑单次执行（`-p`）或交互式会话，把每行输入交给
`CliConsole`，等这一轮的终态消息到达后再读下一行。
不负责：渲染（`console.py`）、把消息送进 orchestrator（`channel.py` + 装配根的泵）、
安装信号处理（`runtime/`——进程是它的）。

**约定不抛**（`contracts.CliEntry.run` 写死）：参数错误与执行失败都打印可诊断信息并返回
非零退出码。用户看到 traceback 说明这条约定破了。

**stdin 一律走 `asyncio.to_thread`**：Windows 的控制台句柄不支持
`loop.connect_read_pipe`，而两个平台各写一条读路径只会让「输入怎么进来的」有两套答案。
代价是阻塞中的读线程不响应取消——Ctrl-C 在等输入时由 Runtime 负责终止进程，这条如实
写在下面的 `_readline` 里。
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from typing import Final, TextIO

from karyvia.contracts import CancelSignal, StreamState

from .console import CliConsole

__all__ = ["QUIT_WORDS", "StdioCliEntry", "USAGE"]

#: 结束会话的本地词。它们**不是**斜杠命令：命令由 dispatcher 分流并产生一个 turn，
#: 而「退出」要做的事是让 `run()` 返回——那是进程控制，不是 Agent 能力。
QUIT_WORDS: Final = ("/exit", "/quit")

USAGE: Final = """用法：karyvia run [-p 提示词]

选项：
  -p, --prompt TEXT   单次执行：跑完这一条就退出（退出码反映 turn 终态）
      --reasoning     显示模型的推理片段
  -h, --help          打印本说明

不带 -p 时进入交互式会话：每行输入是一轮对话，Ctrl-C 中断当前轮并继续，
再次 Ctrl-C 退出；输入 /exit 或 /quit 也可退出。
"""

#: 取消轮询间隔。`CancelSignal` 只有 `requested` 可轮询（`CancelToken.wait()` 属 kernel
#: 扩展面，`R4` 够不着），与 `tools_shell` 同一条理由与同一个数量级。
_CANCEL_POLL_MS: Final = 50


class _Args:
    """解析结果。不用 argparse：它在错误时 `SystemExit`，而本入口约定不抛。"""

    __slots__ = ("error", "help", "prompt", "reasoning")

    def __init__(self) -> None:
        self.prompt: str | None = None
        self.help = False
        self.reasoning = False
        self.error: str | None = None


def _parse(argv: Sequence[str]) -> _Args:
    args = _Args()
    items = list(argv)
    index = 0
    while index < len(items):
        item = items[index]
        if item in ("-h", "--help"):
            args.help = True
        elif item in ("-p", "--prompt"):
            index += 1
            if index >= len(items):
                args.error = "-p/--prompt 后面要跟提示词。"
                return args
            args.prompt = items[index]
        elif item == "--reasoning":
            args.reasoning = True
        else:
            args.error = f"未知参数 {item!r}。"
            return args
        index += 1
    return args


class StdioCliEntry:
    """`contracts.CliEntry` 的内建实现。

    与 `CliChannel` 共用一个 `CliConsole`：入口负责「什么时候读下一行」，Channel 负责
    「这些消息怎么进出 Kernel」。两者拆开的分界线就是进程所有权。
    """

    def __init__(
        self,
        console: CliConsole,
        *,
        stdin: TextIO | None = None,
        prompt: str = "> ",
    ) -> None:
        self._console = console
        self._stdin = stdin
        self._prompt = prompt

    async def run(self, argv: Sequence[str], cancel: CancelSignal) -> int:
        """接管本次 `karyvia` 调用，返回退出码。"""
        args = _parse(argv)
        if args.error is not None:
            self._console.notice(f"{args.error}\n\n{USAGE}")
            return 2
        if args.help:
            self._console.notice(USAGE)
            return 0
        self._console.show_reasoning = args.reasoning

        try:
            if args.prompt is not None:
                return await self._once(args.prompt, cancel)
            return await self._interactive(cancel)
        finally:
            # 入站流必须关掉，否则装配根的 Channel 泵会一直等下一条消息。
            self._console.close()

    async def _once(self, prompt: str, cancel: CancelSignal) -> int:
        """单次执行：跑完一条就走。退出码来自终态，脚本因此判得出成败。"""
        if cancel.requested:
            return 130
        self._console.submit(prompt)
        await self._await_turn(cancel)
        return _exit_code(self._console.last_state)

    async def _interactive(self, cancel: CancelSignal) -> int:
        """交互式会话：一行一轮，直到 EOF、`/exit` 或取消。"""
        while not cancel.requested:
            self._console.notice_prompt(self._prompt)
            line = await asyncio.to_thread(self._readline)
            if line is None:
                break
            text = line.strip()
            if not text:
                continue
            if text.lower() in QUIT_WORDS:
                break
            if cancel.requested:
                break
            self._console.submit(text)
            await self._await_turn(cancel)
        return 130 if cancel.requested else 0

    def _readline(self) -> str | None:
        """读一行，EOF 返回 `None`。**在工作线程里跑，阻塞期间不响应取消**。

        Ctrl-C 落在等输入的时刻时，由 Runtime 结束进程——一个卡在 `readline()` 上的
        线程没有可移植的唤醒方式，假装能唤醒它只会让退出路径多一个不成立的假设。
        """
        stream = self._stdin if self._stdin is not None else sys.stdin
        line = stream.readline()
        return None if line == "" else line

    async def _await_turn(self, cancel: CancelSignal) -> None:
        """等这一轮的终态消息。取消后不再干等——那一轮的终态会由 Kernel 发出，
        但如果连 Kernel 都停了，干等就是挂死。"""
        waiter = asyncio.ensure_future(self._console.wait_for_turn())
        try:
            while not waiter.done():
                done, _ = await asyncio.wait({waiter}, timeout=_CANCEL_POLL_MS / 1000)
                if done:
                    return
                if cancel.requested:
                    return
        finally:
            if not waiter.done():
                waiter.cancel()


def _exit_code(state: StreamState | None) -> int:
    """终态 → 退出码。`FINAL` 也覆盖撞上预算上限的那种（`TERMINAL_STREAM_STATES`）。"""
    if state is StreamState.FINAL:
        return 0
    if state is StreamState.CANCELLED:
        return 130
    return 1
