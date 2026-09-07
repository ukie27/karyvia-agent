"""进程执行与取消宽限期：终止信号 → 宽限期 → 仍不退出标 `UNKNOWN`。

职责：用 `asyncio.create_subprocess_{exec,shell}` 启动进程、并发抽干两个管道、在超时或
取消时按「终止信号 → 宽限期 → 强杀」三步收场，交回退出码、输出与「宽限期是否用尽」。
不负责：校验命令（`command.py`）、构造环境变量（`environ.py`）、校验 cwd（`paths.py`）、
截断输出与折成 `ToolResult`（`executor.py`）。

**取消宽限期的全部逻辑在这里**。四条不变量：

1. **必须在 `timeout_ms + grace_ms` 内返回**。这是 `kernel/turn/invoker.py` 那条
   「`invoke` 必须在 `timeout_ms + grace` 内返回」在本工具内部的兑现——它在外面还有一层
   同样的闸门，但等到那一层才收场就意味着本工具从没试过让进程体面退出。
2. **先送终止信号，不直接强杀**。POSIX 送 `SIGTERM`、Windows 走 `TerminateProcess`
   （`Popen.terminate()` 在两个平台的语义差异是平台事实，见下）。一条 `rm -rf` 写了
   一半就被 `SIGKILL` 停掉，不叫取消成功，叫留下了半个被删掉的目录树。宽限期正是留给
   进程自己收尾的。
3. **宽限期用尽才强杀，并如实标 `grace_expired`**。此时进程可能已经写了一半文件、改了
   一半配置，Kernel 确实不知道外部世界变成什么样了——那正是 `SideEffect.UNKNOWN` 的正主，
   也是本包与 `tools_fs`（一次 `UNKNOWN` 都不产出）唯一的语义差异。
4. **强杀之后仍然 `await` 一次**，把进程收尸。不收的话 POSIX 上会留下僵尸进程，而这个
   实例可能要跑几个月。

**为什么两个管道从一开始就并发抽干**：`process.wait()` 在管道写满时会死锁——子进程阻塞在
写 stdout 上、父进程阻塞在等它退出上，两边都不动。`communicate()` 解决了这个问题但它会
等进程退出，而宽限期用尽时进程可能还要跑一年。因此这里自己开两个抽干任务，任何时刻都能
拿到「到目前为止的输出」，包括被强杀的那一刻。

**输出按 UTF-8 宽松解码并归一换行符**（`errors="replace"`）。不强制子进程的
输出编码——那会盖掉运维在 `pass_env` 里点名转发的 `LC_ALL`。宽松解码意味着一个输出了
非法 UTF-8 的程序不会让整次调用失败，而损坏本身是可见的（`�`），与 `tools_fs.decode_text`
是同一条语义。`\r\n` 与孤立 `\r` 一律归一成 `\n`，让同一份命令在两个平台上
给模型看到的是同一段文本。

**Windows 走 `create_subprocess_shell`，POSIX 走 `exec`**——理由与注意事项见 `command.py`
的模块 docstring。代价是 Windows 的 shell 配置项不生效，那是刻意的：`cmd.exe` 与
PowerShell 的命令语法本就不兼容。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from karyvia.contracts import CancelSignal

from .command import build_argv, validate_command

__all__ = [
    "CANCEL_POLL_MS",
    "DEFAULT_GRACE_MS",
    "ProcessOutcome",
    "ProcessResult",
    "effective_timeout_ms",
    "run_process",
]

#: 取消宽限期：送出终止信号后再给进程多少时间收尾。与 `kernel/turn/cancel.py`
#: 的 `DEFAULT_TOOL_CANCEL_GRACE_MS` 取同一个值（2000 ms），但两处各写一份——`R4` 禁止
#: `builtins/` import `kernel/`，而这个常量没有进 `contracts/`。有一条对照测试钉住。
DEFAULT_GRACE_MS: Final = 2_000

#: 轮询 `cancel.requested` 的间隔。`CancelSignal` 只有一个可轮询的属性（`CancelToken` 的
#: `wait()` 属于 kernel 侧的扩展面，`R4` 够不着），因此只能轮询。50 ms 对一次动辄几百毫秒
#: 的命令执行是可忽略的开销，又让取消的响应延迟低于人的感知。
CANCEL_POLL_MS: Final = 50


class ProcessOutcome:
    """一次执行是怎么收场的。三个取值互斥，`executor.py` 按它决定 `side_effect`。"""

    #: 进程自己退出（无论退出码是几）。外部世界的变化由命令本身决定，`OCCURRED`。
    COMPLETED: Final = "completed"

    #: 收到终止信号后在宽限期内退出。它有机会收尾了，但做没做完不知道——仍按 `OCCURRED`。
    TERMINATED: Final = "terminated"

    #: 宽限期用尽被强杀。`UNKNOWN` 的正主。
    GRACE_EXPIRED: Final = "grace_expired"


class ProcessResult:
    """一次执行的产出：退出码、输出、耗时与收场方式。

    `outcome is GRACE_EXPIRED` 时 `exit_code` 为 `None`——进程是被强杀的，那个退出码
    （POSIX 上是 `-SIGKILL`）说的是「我们杀了它」而不是「它跑成什么样」，报出来只会误导。

    刻意不是 dataclass：构造点只有 `run_process()` 一处，而 `outcome` 只在那里能确定——
    让它成为必填字段可以让「忘了判宽限期」在类型层就失败。
    """

    __slots__ = ("_duration_ms", "_exit_code", "_outcome", "_stderr", "_stdout", "_timed_out")

    def __init__(
        self,
        *,
        exit_code: int | None,
        stdout: str,
        stderr: str,
        duration_ms: int,
        outcome: str,
        timed_out: bool,
    ) -> None:
        self._exit_code = exit_code
        self._stdout = stdout
        self._stderr = stderr
        self._duration_ms = duration_ms
        self._outcome = outcome
        self._timed_out = timed_out

    @property
    def exit_code(self) -> int | None:
        """进程退出码；被强杀时为 `None`。"""
        return self._exit_code

    @property
    def stdout(self) -> str:
        return self._stdout

    @property
    def stderr(self) -> str:
        return self._stderr

    @property
    def duration_ms(self) -> int:
        return self._duration_ms

    @property
    def outcome(self) -> str:
        """`ProcessOutcome` 三个取值之一。"""
        return self._outcome

    @property
    def timed_out(self) -> bool:
        """收场是由超时触发的（而不是取消）。两者的善后相同，但诊断要分得开。"""
        return self._timed_out

    @property
    def grace_expired(self) -> bool:
        """宽限期用尽被强杀——`side_effect` 必须是 `UNKNOWN`。"""
        return self._outcome == ProcessOutcome.GRACE_EXPIRED


def effective_timeout_ms(*, invocation_timeout: int, config_timeout: int) -> int:
    """两个超时的较小者——Kernel 那一侧已经把 turn 剩余预算压进 `invocation_timeout` 了。

    返回值恒 > 0：两边都是正整数（`ToolInvocation` 的构造校验 + `settings.py` 的配置校验）。
    """
    return min(invocation_timeout, config_timeout)


async def run_process(
    *,
    command: str,
    cwd: Path,
    env: Mapping[str, str],
    timeout_ms: int,
    grace_ms: int = DEFAULT_GRACE_MS,
    shell: str = "",
    cancel: CancelSignal,
) -> ProcessResult:
    """启动进程并等它完成，或在超时/取消时按三步收场。

    `command` 是命令串（本函数负责校验），`timeout_ms` 是执行本身的超时（不含宽限期），
    `grace_ms` 是送出终止信号后的宽限期，`shell` 只在 POSIX 上生效（见 `command.py`）。
    两个超时都按毫秒，调用方保证都是正整数。

    **平台分派只在这一处**：POSIX 走 `exec(<shell>, "-c", command)`，Windows 走
    `create_subprocess_shell(command)`。理由与那条 `list2cmdline` 的坑写在 `command.py`
    的模块 docstring 里——把它挪到别处就会有人"顺手统一"成 exec，而那在 Windows 上会让
    任何带引号的命令残掉。

    **取消语义**：入口检查一次；执行期间每 `CANCEL_POLL_MS` 轮询一次 `cancel.requested`。
    取消或超时都进同一条收场路径：终止信号 → 等 `grace_ms` → 强杀 + 收尸。

    **异常约定**：命令串形状非法时抛 `INPUT_MALFORMED` / `INPUT_TOO_LARGE`
    （`validate_command`），入口取消时抛 `CANCELLED` 类错误——两者都由 `executor.py` 的
    `execute()` 折成结果。除此之外不抛：进程启动失败折成 `exit_code=-1` 的结果并把异常
    类型放进 `stderr`，那是一次有结论的调用（进程根本没起来，外部世界没变），不该走
    `UNKNOWN` 那条路。
    """
    validate_command(command)
    cancel.raise_if_requested()
    started = time.perf_counter()

    try:
        process = await _spawn(command, cwd=cwd, env=env, shell=shell)
    except OSError as error:
        # 启动失败（shell 不存在、cwd 没权限、命令行太长）。`-1` 不是任何程序的真实退出码
        # （有效范围 0–255），因此诊断能区分「启动失败」与「程序返回 1」。
        return ProcessResult(
            exit_code=-1,
            stdout="",
            stderr=f"启动失败：{type(error).__name__}",
            duration_ms=_elapsed_ms(started),
            outcome=ProcessOutcome.COMPLETED,
            timed_out=False,
        )

    # 两个管道从一开始就并发抽干，否则写满时会与 `wait()` 死锁（见模块 docstring）。
    drains = (
        asyncio.ensure_future(_drain(process.stdout)),
        asyncio.ensure_future(_drain(process.stderr)),
    )
    try:
        outcome, timed_out = await _supervise(
            process, timeout_ms=timeout_ms, grace_ms=grace_ms, cancel=cancel
        )
        stdout, stderr = await _collect(drains)
        return ProcessResult(
            # 被强杀时退出码说的是「我们杀了它」，报出来只会误导。
            exit_code=None if outcome == ProcessOutcome.GRACE_EXPIRED else process.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=_elapsed_ms(started),
            outcome=outcome,
            timed_out=timed_out,
        )
    finally:
        for task in drains:
            task.cancel()
        _release(process)


def _release(process: asyncio.subprocess.Process) -> None:
    """关掉子进程的管道传输，把 fd 交回操作系统。

    **为什么要显式做这件事**：正常退出的进程由 asyncio 自己收拾，但被强杀后我们
    `cancel()` 掉了 waiter 与两个抽干任务——传输对象于是活到下一次 GC，届时事件循环
    多半已经关了，`__del__` 里那句 `call_soon` 就抛 `Event loop is closed`。表现是一串
    与真正出问题的调用**对不上号**的 `ResourceWarning`（它们挂在后面某个无辜的用例上），
    而在一个跑几个月的实例里，那是一条真实的 fd 泄漏。

    `_transport` 是私有属性，`asyncio.subprocess.Process` 没有公开的等价物
    （`Process.close()` 不存在）。用 `getattr` 取而不是直接点属性：CPython 换了内部名字
    时这里应当安静地少做一件清理，而不是让每次工具调用都炸。
    """
    transport = getattr(process, "_transport", None)
    if transport is not None:
        with contextlib.suppress(Exception):
            transport.close()


async def _spawn(
    command: str, *, cwd: Path, env: Mapping[str, str], shell: str
) -> asyncio.subprocess.Process:
    """按平台启动进程。两条路的差异见 `command.py` 的模块 docstring。"""
    pipes: dict[str, object] = {
        "stdin": asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if os.name == "nt":
        # Windows：交给 CPython 拼 `%ComSpec% /c "<命令>"`。走 exec 会被
        # `list2cmdline()` 的反斜杠转义毁掉带引号的命令。
        return await asyncio.create_subprocess_shell(
            command, cwd=cwd, env=dict(env), **pipes  # type: ignore[arg-type]
        )
    return await asyncio.create_subprocess_exec(
        *build_argv(command, shell=shell), cwd=cwd, env=dict(env), **pipes  # type: ignore[arg-type]
    )


async def _supervise(
    process: asyncio.subprocess.Process,
    *,
    timeout_ms: int,
    grace_ms: int,
    cancel: CancelSignal,
) -> tuple[str, bool]:
    """等进程退出，或在超时/取消时收场。返回 `(outcome, 是否由超时触发)`。"""
    waiter = asyncio.ensure_future(process.wait())
    try:
        timed_out = await _wait_or_cancel(waiter, timeout_ms=timeout_ms, cancel=cancel)
    except BaseException:
        waiter.cancel()
        raise
    if waiter.done():
        return ProcessOutcome.COMPLETED, False

    # 超时或取消：先送终止信号，给进程收尾的机会。
    with contextlib.suppress(ProcessLookupError, OSError):
        process.terminate()
    try:
        await asyncio.wait_for(asyncio.shield(waiter), timeout=grace_ms / 1000)
    except asyncio.TimeoutError:
        # 宽限期用尽：强杀并收尸，如实标 `GRACE_EXPIRED`。
        with contextlib.suppress(ProcessLookupError, OSError):
            process.kill()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(waiter), timeout=grace_ms / 1000)
        waiter.cancel()
        return ProcessOutcome.GRACE_EXPIRED, timed_out
    return ProcessOutcome.TERMINATED, timed_out


async def _wait_or_cancel(
    waiter: asyncio.Future[int], *, timeout_ms: int, cancel: CancelSignal
) -> bool:
    """等 `waiter` 完成、超时或被取消。返回「是否由超时触发」。

    轮询而不是 `wait_for(timeout)` 一次到底，是因为取消要在超时之前就被看见——一条
    `timeout_ms=120000` 的命令在用户按下 Ctrl-C 之后还要跑两分钟，不叫支持取消。
    """
    deadline = time.monotonic() + timeout_ms / 1000
    poll = CANCEL_POLL_MS / 1000
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(waiter), timeout=min(poll, remaining))
        if waiter.done():
            return False
        if cancel.requested:
            return False


async def _drain(stream: asyncio.StreamReader | None) -> bytes:
    """读到 EOF。管道在进程退出后关闭；被强杀时这个任务会被 `cancel()`。"""
    if stream is None:
        return b""
    return await stream.read()


async def _collect(drains: tuple[asyncio.Future[bytes], ...]) -> tuple[str, str]:
    """取两个抽干任务到目前为止的产出。

    进程已退出时它们很快就到 EOF；被强杀时管道也随之关闭，因此这里同样会返回。给一个
    短上限只是防御——一个把 stdout 继承给孙进程的命令会让管道一直开着，而我们已经
    决定不等它了。
    """
    collected: list[str] = []
    for task in drains:
        try:
            data = await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            data = b""
        collected.append(_decode(data))
    return collected[0], collected[1]


def _decode(data: bytes) -> str:
    """按 UTF-8 宽松解码并归一换行符。

    坏字节变成 `�`，不让整次调用失败。`\r\n` 与孤立 `\r` 一律归一成 `\n`
    ，让同一份命令在两个平台上给模型看到的是同一段文本。
    """
    text = data.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))
