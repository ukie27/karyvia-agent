"""`nm serve`：无头模式——装配实例、启动 Channel、等信号、停。

职责：把 `bootstrap()` → `start()` → 等待 → `stop()` 串成一条命令，并解析
`--host` / `--port` 这两个覆盖参数。
不负责：任何协议细节（那归 Channel 能力自己，例如 `openai-api` 插件）、
交互式会话（`nm run`）、生成配置（`runtime/first_run.py`）。

**它是通用的，不是给某一个插件写的**：任何 `CHANNEL` 能力（HTTP、飞书或未来的新平台）
都需要「起来、待着、能被干净地停掉」这条命令。`nm run` 做不到——它把进程交给 CLI 入口，
那条路要读 stdin，在 `nohup` / systemd 下没有意义。

**`Ctrl-C` 只有一档**，与 `nm run` 的两档不同：那边的两档是因为有一个工作线程阻塞在
`readline()` 上、没有可移植的唤醒方式；这里没有任何东西阻塞，第一次按下就能干净地
走完 `stop()`（在跑的 turn 先被请求取消，已产生的内容因此落库），因此不需要
`os._exit`。停止过程中再按一次才强制退出。
"""

from __future__ import annotations

import asyncio
import sys
from typing import Final

from nucleamind.contracts import CancelReason
from nucleamind.kernel.config import InstanceLayout

from ...bootstrap import bootstrap
from ...first_run import ensure_initial_config, guidance_lines
from ...instance import AgentInstance
from ..main import Options, install_cancel_handler
from ..plugin_warnings import write_plugin_failures

__all__ = ["serve_command"]

#: 强制退出前留给 `stop()` 的时间，与 `nm run` 同一个数。
_STOP_GRACE_S: Final = 3.0


def serve_command(options: Options) -> int:
    """同步入口。退出码：0 正常停止 / 1 没有 Channel 起来 / 2 参数或配置错 / 130 中断。"""
    overrides = _channel_overrides(options.rest)
    if overrides is None:
        sys.stderr.write(
            "用法：nm serve [--host <地址>] [--port <端口>]\n"
            "\n它启动全部已启用的 Channel 能力并常驻。协议细节归各 Channel 插件；\n"
            "例如 OpenAI 兼容接口来自 openai-api 插件。\n"
        )
        return 2
    layout = InstanceLayout.resolve(
        instance_dir=options.instance_dir, instance=options.instance
    )
    if not layout.config_path.exists():
        # 与 `nm run` 完全同一条首次运行分支：只生成、只指路（§10.1 步骤 2）。
        for line in guidance_lines(ensure_initial_config(layout)):
            sys.stdout.write(f"{line}\n")
        return 0
    return asyncio.run(_serve(options, overrides))


async def _serve(options: Options, overrides: list[str]) -> int:
    instance = await bootstrap(
        instance=options.instance,
        instance_dir=options.instance_dir,
        overrides=[*options.overrides, *overrides],
    )
    stopping = asyncio.Event()
    interrupts = _Interrupts(instance, stopping)
    install_cancel_handler(asyncio.get_running_loop(), interrupts)
    try:
        await instance.start()
        write_plugin_failures(instance)
        served = [
            channel_id for channel_id in instance.active_channel_ids if channel_id != "cli"
        ]
        if not served:
            sys.stderr.write(
                "nm: 没有可服务的 Channel。用 nm plugins list 看看有没有启用一个"
                "提供 CHANNEL 能力的插件。\n"
            )
            return 1
        sys.stdout.write(f"nm: 已启动，Channel：{', '.join(served)}。Ctrl-C 停止。\n")
        sys.stdout.flush()
        await stopping.wait()
        return 130 if interrupts.interrupted else 0
    finally:
        await _stop(instance)


async def _stop(instance: AgentInstance) -> None:
    """停实例。超时也要返回——卡住的收尾不该让进程永远退不出去。"""
    try:
        await asyncio.wait_for(instance.stop(), timeout=_STOP_GRACE_S)
    except TimeoutError:
        sys.stderr.write("nm: 停止超时，仍在跑的收尾被放弃。\n")


class _Interrupts:
    """`Ctrl-C` 的处理：第一次请求取消并开始停止，第二次强制退出。"""

    __slots__ = ("_instance", "_stopping", "interrupted")

    def __init__(self, instance: AgentInstance, stopping: asyncio.Event) -> None:
        self._instance = instance
        self._stopping = stopping
        self.interrupted = False

    def __call__(self) -> None:
        if self.interrupted:
            sys.stderr.write("\nnm: 再次中断，强制退出。\n")
            raise SystemExit(130)
        self.interrupted = True
        sys.stderr.write("\nnm: 正在停止……\n")
        for turn_id in self._instance.orchestrator.live_turns:
            self._instance.orchestrator.cancel(turn_id, CancelReason.SHUTDOWN)
        self._stopping.set()


def _channel_overrides(rest: list[str]) -> list[str] | None:
    """把 `--host` / `--port` 翻成配置覆盖。参数非法时返回 `None`。

    它们覆盖的是 **`openai-api` 插件的配置块**——这条命令本身不认识任何协议，
    但这两个参数是所有网络 Channel 的公分母，而 `--set` 的完整路径写起来太长。
    其余配置一律走 `--set`。
    """
    overrides: list[str] = []
    index = 0
    while index < len(rest):
        item = rest[index]
        if item in ("-h", "--help"):
            return None
        if item not in ("--host", "--port"):
            return None
        index += 1
        if index >= len(rest):
            return None
        key = item[2:]
        if key == "port" and not rest[index].isdigit():
            return None
        overrides.append(f"plugins.openai-api.config.{key}={rest[index]}")
        index += 1
    return overrides
