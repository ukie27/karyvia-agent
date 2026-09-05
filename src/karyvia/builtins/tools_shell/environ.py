"""子进程环境变量的构造：默认全部不继承（`NFR-307`、`MOD-002`）。

职责：按平台基线 + 运维显式列举，构造交给子进程的那份 `env`。
不负责：执行命令（`process.py`）、决定 cwd（`executor.py`）、校验配置（`settings.py`）。

**默认拒绝，不是默认允许**。`NFR-307` 要求子进程默认环境必须保守，扩大继承范围必须由
用户显式配置。做成黑名单（「过滤掉看起来像密钥的变量名」）在结构上就是错的：
它要求这份名单穷举出所有会泄漏的变量名，而漏掉一个的代价是把父进程的凭据交给一条模型
写的命令。这里反过来——**父进程的环境默认一个字节都不进子进程**，子进程只拿到
`_BASELINE_NAMES` 那几个让 shell 能正常启动的变量，其余要靠 `pass_env` 逐个点名。

因此「哨兵变量不进子进程」不是一条需要维护的过滤规则，而是**没有路径**：
`os.environ` 只在 `_BASELINE_NAMES` 与 `pass_env` 两处被按名读取，两处都是白名单。

**基线名单本身不含任何凭据类变量**：它只有让 `sh` / `cmd.exe` 找得到自己、解得开路径、
按 UTF-8 输出的那几项。两个平台各一份是因为它们对「shell 能跑起来」的最低要求不同
（Windows 的 `cmd.exe` 缺了 `SystemRoot` 直接起不来），但**对外行为契约一致**
（`NFR-605`）：两边都是白名单，两边都不继承未点名的变量。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Final

__all__ = [
    "BASELINE_NAMES",
    "FORCED_ENV",
    "build_environment",
]

#: POSIX 上让一个非登录 shell 正常工作的最小集合。
#: 刻意**不含** `SHELL`、`USER`、`LOGNAME`、`SSH_*`——它们对执行命令没有必要，而
#: `SSH_AUTH_SOCK` 之类恰好是一条能被用来横向移动的凭据通道。
_POSIX_BASELINE: Final[tuple[str, ...]] = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TERM")

#: Windows 上让 `cmd.exe` 起得来、解得开路径的最小集合。
#: 比 POSIX 那份长是平台事实而不是放宽：缺 `SystemRoot` 时 `cmd.exe` 直接失败，
#: 缺 `PATHEXT` 时 `where` 与命令解析行为都会变。
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

#: 当前平台的基线名单。按名从 `os.environ` 取，取不到就不设——不编造默认值：
#: 一个编出来的 `PATH` 会让命令找不到自己，而那个失败比「没有 PATH」更难诊断。
BASELINE_NAMES: Final[tuple[str, ...]] = (
    _WINDOWS_BASELINE if os.name == "nt" else _POSIX_BASELINE
)

#: 无论父进程有没有，都强制写进子进程的值。
#:
#: - `PYTHONUNBUFFERED`：子进程是 Python 时不缓冲输出。命令被超时杀掉时，缓冲区里那部分
#:   输出就永远拿不到了，而那往往正是诊断需要的部分。
#: - `PYTHONIOENCODING` / `LC_ALL` 不在这里：前者只对 Python 子进程有意义，后者会盖掉
#:   运维在 `pass_env` 里点名转发的区域设置。编码归一在读的一侧做（`process.py` 按 UTF-8
#:   宽松解码），那样对任何语言写的程序都成立。
FORCED_ENV: Final[Mapping[str, str]] = {"PYTHONUNBUFFERED": "1"}


def build_environment(
    *,
    pass_env: tuple[str, ...] = (),
    overrides: Mapping[str, str] | None = None,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """构造交给子进程的环境。

    三层，后者覆盖前者：**平台基线 → 运维点名转发（`pass_env`）→ 显式写死（`env`）**。
    `FORCED_ENV` 在基线之后、`pass_env` 之前——运维仍然能覆盖掉它。

    `source` 默认是 `os.environ`，参数化只为让测试能喂一份确定的父环境而不去改真实的
    进程环境（改它会污染同进程里并发跑的其他用例）。

    **异常约定**：不抛。`pass_env` 里点名了一个父进程没有的变量时**不设它也不报错**——
    「转发 `CARGO_HOME`，如果有的话」是这个配置项唯一合理的语义，而一份在开发机上能用、
    在 CI 上因为少一个可选变量就启动失败的配置不是好设计。
    """
    parent = os.environ if source is None else source
    env: dict[str, str] = {}

    for name in BASELINE_NAMES:
        value = parent.get(name)
        if value is not None:
            env[name] = value

    env.update(FORCED_ENV)

    for name in pass_env:
        value = parent.get(name)
        if value is not None:
            env[name] = value

    if overrides:
        env.update(overrides)

    return env
