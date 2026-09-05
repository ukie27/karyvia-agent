"""跨平台命令构造：命令串的形状校验与 POSIX argv（`EDG-404`、`NFR-605`）。

职责：校验命令串的形状，并在 POSIX 上拼出 `<shell> -c <command>` 的 argv。
不负责：执行与平台分派（`process.py`）、环境变量（`environ.py`）、cwd 判定（`paths.py`）。

**两个平台的启动方式不同，但对外行为契约一致**（技术方案 §8.3）：同样的 `command` 参数
产生同样的退出码语义、同样的输出截断规则、同样的超时行为。差别在于——

- **POSIX**：`create_subprocess_exec(<shell>, "-c", <command>)`。shell 可配置。
- **Windows**：`create_subprocess_shell(<command>)`，由 CPython 拼成 `%ComSpec% /c "<命令>"`。

**Windows 为什么不能走 exec**（这是本模块最容易被"顺手改回去"的一处）：`cmd.exe` 接在
`/c` 之后的是**原始命令行尾巴**而不是解析好的 argv，而 `subprocess` 在 Windows 上要用
`list2cmdline()` 把 argv 拼回字符串——它按 MSVC 规则把内层引号转义成 `\\"`，而 `cmd.exe`
不认识反斜杠转义。于是一条 `"C:\\path with space\\python.exe" -c "print(1)"` 交到 cmd
手上就成了残句，当场以 exit 1 失败。CPython 的 `shell=True` 分支拼的是
`%ComSpec% /c "<原样命令>"`，外层那对引号正好抵消 `cmd` 的首尾引号剥离规则——这是
Windows 上唯一可靠的那条路。代价是拿不到 `/d`（跳过 AutoRun 注册表项），如实记在这里。

**因此 `shell` 配置项只对 POSIX 有效**，Windows 上恒为 `%ComSpec%`。这不是疏漏：
`cmd.exe` 与 PowerShell 的命令语法本就不兼容，让用户在 Windows 上把 shell 换成
PowerShell，只会让模型按 sh 语法写的命令以另一种方式失败。

**不做命令内容的安全过滤**。legacy 的 `_guard_command` 维护了一张 `rm -rf` 之类的模式
黑名单，本内建刻意不移植：模型能写出的绕过形式是无穷的（换行、变量展开、base64 管道），
而一张挡不住的黑名单会让人以为挡住了。真正的边界是 workspace（cwd 限定）与
`TOL-004` 的确认策略——`shell.exec` 因此是
`DESTRUCTIVE` + `EXCLUSIVE`。
"""

from __future__ import annotations

import os
import shutil
from typing import Final

from karyvia.contracts import ErrorCode, KaryviaError

__all__ = [
    "MAX_COMMAND_LENGTH",
    "build_argv",
    "default_shell",
    "validate_command",
]

#: 命令串长度上限。够写任何正经命令，又挡住了把一个几 MB 的串塞进命令行——
#: 两个平台对命令行总长都有限制，超了之后的失败信息指向的是操作系统而不是这行参数。
MAX_COMMAND_LENGTH: Final = 16_384

#: POSIX 下的兜底 shell。`/bin/sh` 而不是 bash：POSIX 保证它存在，而 bash 在
#: Alpine 一类的镜像里没有。运维要 bash 就在配置里写 `shell`。
_POSIX_FALLBACK: Final = "/bin/sh"


def default_shell() -> str:
    """POSIX 下按存在性选出 shell 程序。Windows 上不调用本函数（见模块 docstring）。

    **不读 `$SHELL`**：那是用户的交互式登录 shell（可能是 fish、nushell），语法与 `-c`
    的行为都未必兼容。命令是模型按 POSIX sh 写的，就该交给 POSIX sh。
    """
    if os.path.exists(_POSIX_FALLBACK):
        return _POSIX_FALLBACK
    return shutil.which("sh") or _POSIX_FALLBACK


def validate_command(command: str) -> str:
    """校验命令串的形状，原样返回。

    **异常约定**：空串、只有空白、含 NUL 字节、超过 `MAX_COMMAND_LENGTH` 一律抛
    `INPUT_MALFORMED`（超长是 `INPUT_TOO_LARGE`）——NUL 会在命令行边界被截断，让实际
    执行的命令与模型写的那条不同，这是必须挡在执行之前的一类输入。
    """
    if not command or not command.strip():
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED, "命令不能为空。", detail={"command": command}
        )
    if "\x00" in command:
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            "命令不得包含 NUL 字节。",
            detail={"command": "<binary>"},
        )
    if len(command) > MAX_COMMAND_LENGTH:
        raise KaryviaError(
            ErrorCode.INPUT_TOO_LARGE,
            "命令过长。",
            detail={"length": len(command), "limit": MAX_COMMAND_LENGTH},
        )
    return command


def build_argv(command: str, *, shell: str = "") -> tuple[str, ...]:
    """POSIX 上把一条命令串拼成 argv：`<shell> -c <command>`。

    `shell` 为空时按存在性自动选（`default_shell()`）。命令串先过 `validate_command()`。

    **Windows 上不调用本函数**——那条路走 `create_subprocess_shell`，理由见模块 docstring。
    保留它在 Windows 上也能返回一个形状正确的 argv，只为让纯函数测试两个平台都能跑。
    """
    validate_command(command)
    return (shell or default_shell(), "-c", command)
