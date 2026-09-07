"""`ctx.shell` 的生产实现：受限的子进程门面（`sdk.ShellAccess`）。

职责：`GuardedShellAccess`——校验 cwd、构造白名单环境、起进程（**不经 shell**）、并发抽干
两个管道、超时后走「终止信号 → 宽限期 → 强杀」三步收尾。
不负责：判定门面能不能拿到（`RuntimePluginContext.shell`）、路径判定（`paths.py`）、
给模型提供 `shell.exec` 工具（那是 `builtins/tools_shell/`）。

**与 `builtins/tools_shell` 的两处刻意差异**：

1. **参数是列表，因此走 `exec` 而不是 shell**（契约原文：「不存在 shell 注入面」）。
   `tools_shell` 收的是模型写的一整行命令，只能交给 shell 解释；这里收的是插件作者写的
   argv，没有理由再过一次 `cmd.exe` / `sh -c`。副产品是 Windows 那条 `list2cmdline()` 的坑
   （`tools_shell/command.py` 的模块 docstring）在这里根本不存在。
2. **超时不抛异常**（契约原文）：返回 `timed_out=True` 的结果，调用方拿得到超时前已经产生
   的输出。`ShellResult` 没有 `side_effect` 字段——那个三档判定是 `ToolResult` 的事。

**环境是白名单，不是黑名单**：父进程的环境默认一个字节都不进子进程。基线名单
在 `builtins/tools_shell/environ.py` 与这里各写一份（`R4` 让内建够不着 `runtime/`，而让
`runtime/` 去 import 一个内建的私有模块等于把门面的安全策略绑在一个可被禁用的提供方上），
由 `tests/runtime/test_access.py::test_shell_baseline_matches_the_builtin_tool` 逐条对照。

**守住 cwd 不等于守住命令能碰到的文件**：一条 `cat /etc/shadow` 用绝对路径，与 cwd 无关。
cwd 边界限制的是「命令默认在哪里落地」；更严格的控制是不启用该能力，或使用独立宿主 /
部署沙箱（§13.7）。这句如实写在这里，不假装挡得住。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from karyvia.contracts import ErrorCode, KaryviaError
from karyvia.sdk import ShellResult

from .paths import PathGuard

__all__ = [
    "BASELINE_NAMES",
    "DEFAULT_GRACE_MS",
    "MAX_OUTPUT_CHARS",
    "GuardedShellAccess",
]

#: 被强杀之前留给进程自己收尾的时间。与 `kernel/turn/cancel.py` 和
#: `builtins/tools_shell/process.py` 各写一份，有对照测试。
DEFAULT_GRACE_MS: Final = 2_000

#: stdout / stderr 各自的字符上限。超出即截断——一条 `find /` 的输出足以撑爆内存，
#: 而插件拿到的应当是「能用的那部分」而不是一次 OOM。
MAX_OUTPUT_CHARS: Final = 200_000

_POSIX_BASELINE: Final[tuple[str, ...]] = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TERM")

_WINDOWS_BASELINE: Final[tuple[str, ...]] = (
    "PATH",
    "PATHEXT",
    "SystemRoot",
    "SystemDrive",
    "ComSpec",
    "WINDIR",
    "TEMP",
    "TMP",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
)

#: 当前平台的基线名单。按名从 `os.environ` 取，取不到就不设——不编造默认值。
BASELINE_NAMES: Final[tuple[str, ...]] = _WINDOWS_BASELINE if os.name == "nt" else _POSIX_BASELINE

#: 起不来时写进 stderr 的那句话。命令名原样带上——插件作者要的就是「哪个命令没找到」。
_SPAWN_FAILED: Final = "命令起不来（不存在、不可执行，或平台不认识它）：{}"


class GuardedShellAccess:
    """`ShellAccess` 的生产实现。结构化满足契约，不继承任何宿主基类。"""

    __slots__ = ("_grace_ms", "_guard", "_plugin_id", "_root", "_source")

    def __init__(
        self,
        root: Path,
        *,
        plugin_id: str,
        grace_ms: int = DEFAULT_GRACE_MS,
        env_source: Mapping[str, str] | None = None,
    ) -> None:
        self._root = Path(root).resolve()
        self._guard = PathGuard(self._root)
        self._plugin_id = plugin_id
        self._grace_ms = grace_ms
        #: 父环境。参数化只为让测试喂一份确定的环境而不去改真实进程环境。
        self._source = env_source

    async def run(
        self,
        command: Sequence[str],
        *,
        cwd: str | None = None,
        timeout_ms: int = 60_000,
    ) -> ShellResult:
        argv = tuple(command)
        if not argv:
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                "命令必须是一个非空的字符串列表。",
                detail={"plugin": self._plugin_id},
            )
        workdir = self._root if cwd is None else self._guard.resolve(cwd)
        process = await self._spawn(argv, workdir)
        if process is None:
            return ShellResult(exit_code=-1, stdout="", stderr=_SPAWN_FAILED.format(argv[0]))
        return await self._supervise(process, timeout_ms)

    # ------------------------------------------------------------------ 进程

    async def _spawn(
        self, argv: Sequence[str], workdir: Path
    ) -> asyncio.subprocess.Process | None:
        """起进程。起不来交回 `None`，由调用方折成 `exit_code=-1`。

        **起不来不抛异常**：契约把「非零退出码不是异常」写死了，而一个拼错的命令名与一次
        返回 127 的执行对调用方是同一件事——都要读 stderr 再决定下一步。-1 不在 0–255 内，
        因此「起不来」与「程序真的返回了 1」仍然分得开（`builtins/tools_shell` 采用的相同判据）。
        """
        try:
            return await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(workdir),
                env=self.build_environment(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return None

    async def _supervise(
        self, process: asyncio.subprocess.Process, timeout_ms: int
    ) -> ShellResult:
        """等进程结束，超时则三步收尾。

        **两个管道从一开始就并发抽干**：`process.wait()` 在管道写满时会与子进程死锁，而
        `communicate()` 会等进程退出——超时的那条命令可能还要跑一年。自己开两个抽干任务，
        任何时刻都拿得到「到目前为止的输出」，包括被强杀的那一刻。
        """
        out = asyncio.ensure_future(_drain(process.stdout))
        err = asyncio.ensure_future(_drain(process.stderr))
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout_ms / 1000)
        except TimeoutError:
            timed_out = True
            await self._terminate(process)
        finally:
            await asyncio.gather(out, err, return_exceptions=True)
            _release(process)
        return ShellResult(
            exit_code=process.returncode if process.returncode is not None else -1,
            stdout=_text(out),
            stderr=_text(err),
            timed_out=timed_out,
        )

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        """终止信号 → 宽限期 → 强杀 + 收尸。

        直接 `kill()` 会让一条写了一半的命令就地停下——那不叫超时收尾，叫留下半份产物。
        宽限期正是留给进程自己收尾的（`builtins/tools_shell/process.py` 采用的相同判据）。
        """
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), self._grace_ms / 1000)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()

    # ------------------------------------------------------------------ 环境

    def build_environment(self) -> dict[str, str]:
        """交给子进程的那份环境：只有平台基线。

        `ctx.shell` **不提供 `pass_env`**：`tools_shell` 那个开关的读者是运维（他知道自己
        在转发什么），而这里的调用方是插件代码——让插件自己决定要继承哪些父进程变量，
        白名单就名存实亡了。插件要传值就在 `command` 里传。
        """
        parent = os.environ if self._source is None else self._source
        env: dict[str, str] = {}
        for name in BASELINE_NAMES:
            value = parent.get(name)
            if value is not None:
                env[name] = value
        env["PYTHONUNBUFFERED"] = "1"
        return env


async def _drain(stream: asyncio.StreamReader | None) -> bytes:
    if stream is None:
        return b""
    return await stream.read()


def _text(task: asyncio.Future[bytes]) -> str:
    """宽松解码 + 截断。解码失败不该让一次成功的执行变成异常。"""
    if task.cancelled():
        return ""
    error = task.exception()
    if error is not None:
        return ""
    decoded = task.result().decode("utf-8", errors="replace")
    if len(decoded) <= MAX_OUTPUT_CHARS:
        return decoded
    dropped = len(decoded) - MAX_OUTPUT_CHARS
    return f"{decoded[:MAX_OUTPUT_CHARS]}\n[输出已截断，省略 {dropped} 个字符]"


def _release(process: asyncio.subprocess.Process) -> None:
    """显式关掉传输。

    被强杀之后传输对象会活到下一次 GC，届时事件循环多半已关，`__del__` 里那句 `call_soon`
    会抛 `Event loop is closed`——表现是一串挂在无辜用例上的 `ResourceWarning`，而在一个
    长期运行的实例中这会造成真实的 fd 泄漏。`_transport` 是私有属性，
    用 `getattr` 取：CPython 换内部名字时应当安静地少做一件清理，而不是让每次调用都炸。
    """
    transport = getattr(process, "_transport", None)
    if transport is not None:
        with contextlib.suppress(Exception):
            transport.close()
