"""`karyvia` 的进程入口：argv 解析与子命令派发（技术方案 §4.2）。

职责：解析顶层 argv 与实例选择参数，把控制权交给 `run` / `config` / `session` 三个子命令，
并把未捕获的异常折成可读诊断与非零退出码。
不负责：装配实例（`runtime/bootstrap.py`）、实现交互（`builtins/cli_entry/`）、
各子命令的正文（`runtime/cli/commands/`）。

**入口与能力是两件事**：`builtins/cli_entry/` 是可被插件覆盖的
**能力**（把 stdin 变成 `InboundMessage`），本模块是不可被覆盖的**进程入口**——它决定
argv 怎么解析、实例怎么装、退出码是什么。`BAS-010` 的「插件可覆盖 CLI 实现」说的是前者。

**信号处理在这里**（进程归 `runtime/`）：首个 `Ctrl-C` 取消在跑的 turn 并让会话继续，
第二个退出进程（§10.3、`contracts.CliEntry.run` 的取消语义）。
"""

from __future__ import annotations

import asyncio
import signal
import sys
from collections.abc import Callable, Sequence
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from typing import Final

from karyvia.contracts import KaryviaError

_USAGE: Final = """用法：karyvia <命令> [参数...]

命令：
  init               生成最小可用配置（首次运行；不覆盖已有 config.json）
  run [-p 提示词]    启动实例并进入交互式会话（或跑单次执行）
  serve [--port N]   无头模式：启动已启用的 Channel 能力并常驻
  config show        打印生效配置与每个值的来源
  session list       列出本实例的会话
  session show <id>  打印一个会话的摘要
  plugins ...        全局安装 / 更新 / 卸载，以及实例启用与状态管理
  capabilities       打印覆盖解析报告（生效 / 被覆盖 / 已禁用 / 冲突）

选项：
  --instance <名字>      选实例（默认 default）
  --instance-dir <目录>  直接指定实例目录，压过 --instance
  --set <小节.键>=<值>   本次运行的临时配置覆盖，可重复
  -V, --version          打印版本
  -h, --help             打印本说明
"""


def resolve_version() -> str:
    try:
        return _dist_version("karyvia")
    except PackageNotFoundError:
        return "0+unknown"


class Options:
    """实例选择参数。三项都对应 `load_config()` 的同名形参。"""

    __slots__ = ("instance", "instance_dir", "overrides", "rest")

    def __init__(self) -> None:
        self.instance: str | None = None
        self.instance_dir: str | None = None
        self.overrides: list[str] = []
        self.rest: list[str] = []


def parse_options(argv: Sequence[str]) -> Options:
    """摘出实例选择参数，其余原样留给子命令。

    **异常约定**：缺参数值时抛 `KaryviaError(INPUT_MALFORMED)`，由 `app()` 折成退出码 2。
    """
    from karyvia.contracts import ErrorCode

    options = Options()
    items = list(argv)
    index = 0
    while index < len(items):
        item = items[index]
        if item in ("--instance", "--instance-dir", "--set"):
            index += 1
            if index >= len(items):
                raise KaryviaError(
                    ErrorCode.INPUT_MALFORMED,
                    f"{item} 后面要跟一个值。",
                    detail={"argument": item},
                )
            if item == "--instance":
                options.instance = items[index]
            elif item == "--instance-dir":
                options.instance_dir = items[index]
            else:
                options.overrides.append(items[index])
        else:
            options.rest.append(item)
        index += 1
    return options


def app(argv: list[str] | None = None) -> int:
    """`karyvia` 的进程入口。返回值即进程退出码（console_scripts 包装为 `sys.exit`）。"""
    args = list(sys.argv[1:] if argv is None else argv)

    if not args or args[0] in ("-h", "--help"):
        sys.stdout.write(_USAGE)
        return 0
    if args[0] in ("-V", "--version"):
        sys.stdout.write(f"karyvia {resolve_version()}\n")
        return 0
    try:
        options = parse_options(args[1:])
    except KaryviaError as error:
        return _report(error)

    command = args[0]
    # 子命令延迟导入：`karyvia --version` 与 `karyvia --help` 不该付出装配根那条 import 链的代价
    # （`NFR-405` 的冷启动预算）。
    if command == "run":
        from .commands.run import run_command

        return _guard(lambda: run_command(options))
    if command == "serve":
        from .commands.serve import serve_command

        return _guard(lambda: serve_command(options))
    if command == "init":
        from .commands.init import init_command

        return _guard(lambda: init_command(options))
    if command == "config":
        from .commands.config import config_command

        return _guard(lambda: config_command(options))
    if command == "session":
        from .commands.session import session_command

        return _guard(lambda: session_command(options))
    if command == "plugins":
        from .commands.plugins import plugins_command

        return _guard(lambda: plugins_command(options))
    if command == "capabilities":
        from .commands.capabilities import capabilities_command

        return _guard(lambda: capabilities_command(options))

    sys.stderr.write(f"karyvia: 未知命令 {command!r}\n\n{_USAGE}")
    return 2


def _guard(run: Callable[[], int]) -> int:
    """跑一个子命令，把 `KaryviaError` 折成诊断输出与退出码。

    **用户看到的不该是 traceback**：启动失败最常见的原因是配置写错或凭据没导出，
    而那两件事的补救办法都写在 `KaryviaError.detail` 里。
    """
    try:
        return run()
    except KaryviaError as error:
        return _report(error)
    except KeyboardInterrupt:
        sys.stderr.write("\n已中断。\n")
        return 130


def _report(error: KaryviaError) -> int:
    """打印一条可操作的错误。**只打 `user_message` 与 `detail`**，两者都已脱敏。"""
    sys.stderr.write(f"karyvia: {error.user_message}\n")
    for key, value in sorted(error.detail.items()):
        sys.stderr.write(f"  {key}: {value}\n")
    return 2


def install_cancel_handler(
    loop: asyncio.AbstractEventLoop, on_interrupt: Callable[[], None]
) -> None:
    """安装 `Ctrl-C` 处理。

    **不用 `loop.add_signal_handler`**：Windows 上它没有实现，而两个平台各写一条信号
    路径会让「按下去之后发生什么」有两套答案。`signal.signal` 在两个平台都可用，回调里
    只做一次 `call_soon_threadsafe`——真正的取消动作在事件循环里跑。
    """
    def handler(signum: int, frame: object) -> None:
        del signum, frame
        loop.call_soon_threadsafe(on_interrupt)

    signal.signal(signal.SIGINT, handler)


def main() -> None:
    """`python -m karyvia.runtime.cli.main` 入口。"""
    raise SystemExit(app())


if __name__ == "__main__":
    main()
