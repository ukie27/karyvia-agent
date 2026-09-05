"""六个内建命令的执行体（技术方案 §8.1）。

职责：`/help`、`/config`、`/session`、`/plugins`、`/capabilities`、`/cancel` 的
`CommandSpec` 与 `CommandHandler` 实现。
不负责：渲染格式（`render.py`）、解析前缀与参数、校验操作者身份与参数个数（dispatcher 在调用
之前已经做完）、取数据的实现（`ctx.instance` / `ctx.turns`）。

三条贯穿本模块的规则：

- **`handle()` 约定不抛**（`CMD-003`）。逸出的异常虽然会被 dispatcher 折成同形状的
  `REJECTED`，但那样就丢掉了实现方本可以给出的具体说明。因此这里只有一处 try/except：
  `_Handler.handle` 的统一出口。捕 `Exception` 不捕 `BaseException`——取消与 Ctrl-C 要放行。
- **不重复 dispatcher 已做的校验**。`operator_only` 与参数个数由它统一前置判定
  （`_check_preconditions`），命令自己再抄一遍只会让两份判定慢慢分叉。
- **带自由文本的命令要声明 `repeated=True` 的尾参**，否则 dispatcher 按「参数过多」拒掉
  （`D14` 写测试时踩到的）。本模块只有 `/cancel` 带参数，且它的参数是单个 turn id，
  因此六个命令里没有用到这条——但下一个加命令的人会用到。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from karyvia.contracts import (
    CancelReason,
    CancelSignal,
    CommandInvocation,
    CommandParam,
    CommandResult,
    CommandSpec,
    Disposition,
    ErrorCode,
    KaryviaError,
    TurnId,
)
from karyvia.sdk import PluginContext

from . import render
from .settings import CommandsSettings

__all__ = ["SPECS", "build_handlers"]

#: 一个命令体：拿 ctx、这次调用与本内建的设置，交出要展示的文本。
#: 设置进签名是 `D23` 加的——`/help` 要印出**生效的**命令前缀，而那是
#: `routing.command_prefix`；`R4` 让内建够不着它，因此装配根经配置块把它交下来
#: （`settings.prefix`）。`D22` 时它是硬编码的默认 `/`，改过前缀的用户会看到错的前缀。
_Body = Callable[[PluginContext, CommandInvocation, "CommandsSettings"], Awaitable[str]]

SPECS: dict[str, CommandSpec] = {
    "help": CommandSpec(
        name="help",
        description="列出全部可用命令及其用法、说明与操作者要求。",
        aliases=("h",),
    ),
    "config": CommandSpec(
        name="config",
        description="显示当前生效的完整配置（凭据以 ${VAR} 引用形式呈现，不含明文）。",
        # 配置里有端点地址、路径与运维策略。它不含明文凭据（`D11` 的结构性保证），
        # 但仍然是「实例怎么装的」这类信息——不该由随便谁在群聊里敲出来。
        operator_only=True,
    ),
    "session": CommandSpec(
        name="session",
        description="显示当前会话的记录数、压缩水位与时间戳。",
    ),
    "plugins": CommandSpec(
        name="plugins",
        description="列出已加载的插件及其状态、版本与注册的能力。",
    ),
    "capabilities": CommandSpec(
        name="capabilities",
        description="列出全部能力的解析结果，标明每项由内建还是哪个插件提供。",
        aliases=("caps",),
    ),
    "cancel": CommandSpec(
        name="cancel",
        description="取消一个正在执行的 turn；不带参数时列出可取消的 turn。",
        parameters=(
            CommandParam(
                name="turn-id",
                description="要取消的 turn 标识；省略则列出当前正在执行的 turn。",
            ),
        ),
    ),
}


def _handled(content: str, limit: int) -> CommandResult:
    """一次成功的命令输出。截断在这里统一做，六个命令不各写一遍。"""
    return CommandResult(Disposition.COMMAND_HANDLED, content=render.truncate(content, limit))


def _rejected(error: KaryviaError) -> CommandResult:
    """一次失败。`CommandResult` 的不变量要求 REJECTED 必须带 error。"""
    return CommandResult(Disposition.REJECTED, content=error.user_message, error=error)


async def _help(
    ctx: PluginContext, invocation: CommandInvocation, settings: CommandsSettings
) -> str:
    del invocation
    return render.render_help(ctx.instance.commands(), settings.prefix)


async def _config(
    ctx: PluginContext, invocation: CommandInvocation, settings: CommandsSettings
) -> str:
    del invocation, settings
    return render.render_config(ctx.instance.config_document())


async def _session(
    ctx: PluginContext, invocation: CommandInvocation, settings: CommandsSettings
) -> str:
    """当前会话来自**这条消息自己的** `session_key`，而不是某个「当前会话」全局状态。

    命令可能来自任意渠道的任意会话，实例级的「当前」在多 session 并发下没有定义
    （`KER-008` 允许同实例多 session 并发）。
    """
    del settings
    snapshot = await ctx.instance.session_snapshot(invocation.message.session_key())
    return render.render_session(snapshot)


async def _plugins(
    ctx: PluginContext, invocation: CommandInvocation, settings: CommandsSettings
) -> str:
    del invocation, settings
    return render.render_plugins(ctx.instance.plugins())


async def _capabilities(
    ctx: PluginContext, invocation: CommandInvocation, settings: CommandsSettings
) -> str:
    del invocation, settings
    return render.render_capabilities(ctx.instance.capabilities())


async def _cancel(
    ctx: PluginContext, invocation: CommandInvocation, settings: CommandsSettings
) -> str:
    """取消一个在跑的 turn。

    **不带参数时只列出而不是取消全部**：一次误敲把所有并发 turn 全掐掉是不可撤销的，
    而列出来再敲一次的代价只有一次往返。

    **`/cancel` 取消不了自己所在的 turn**：这条命令正持有 session 槽位在跑，
    `live_turns()` 里当然有它。取消自己既没有意义，也会让这条命令的输出发不出去，
    因此显式拒绝而不是让用户困惑地看着自己的命令消失。
    """
    del settings
    live = ctx.turns.live_turns()
    if not invocation.args:
        return render.render_turns(live)
    target = TurnId(invocation.args[0])
    if target == invocation.correlation.turn_id:
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            "不能取消 /cancel 命令自己所在的 turn。",
            detail={"turn_id": str(target)},
        )
    if not ctx.turns.cancel_turn(target, CancelReason.USER):
        raise KaryviaError(
            ErrorCode.CAPABILITY_MISSING,
            "该 turn 不在执行中（可能已经结束）。",
            detail={"turn_id": str(target), "live": [str(t) for t in live]},
        )
    return render.render_turns(live, cancelled=target)


#: 命令名 -> 执行体。分开写而不是塞进 `SPECS`，是因为 spec 是**数据**（要能被 registry
#: 统一列出，`CMD-001`），执行体要绑 ctx 与设置。
_BODIES: dict[str, _Body] = {
    "help": _help,
    "config": _config,
    "session": _session,
    "plugins": _plugins,
    "capabilities": _capabilities,
    "cancel": _cancel,
}


class _Handler:
    """把一个命令体包成 `CommandHandler`，并在这里唯一一次兑现「约定不抛」。"""

    def __init__(
        self,
        name: str,
        body: _Body,
        ctx: PluginContext,
        settings: CommandsSettings,
    ) -> None:
        self._name = name
        self._body = body
        self._ctx = ctx
        self._settings = settings

    async def handle(
        self, invocation: CommandInvocation, cancel: CancelSignal
    ) -> CommandResult:
        """执行命令。

        **异常约定**：不抛。`KaryviaError` 原样带出（实现方给的诊断比我们能编的更准），
        其余异常折成 `KERNEL_INVARIANT_VIOLATED` 且**只放类型名不放异常消息**——
        与 `D13` 的 dispatcher 同一条理由：自由文本可能带着凭据。
        **取消语义**：每个命令都是一次内存查询或一次会话读取，入口检查一次即可；
        在这之后再插检查点只会得到一串必然为假的判断。
        """
        try:
            cancel.raise_if_requested()
            content = await self._body(self._ctx, invocation, self._settings)
            return _handled(content, self._settings.max_output_chars)
        except KaryviaError as error:
            return _rejected(error)
        except Exception as error:  # noqa: BLE001 - 折成可诊断结果是本方法的全部职责。
            return _rejected(
                KaryviaError(
                    ErrorCode.KERNEL_INVARIANT_VIOLATED,
                    "命令执行时发生了未预期的错误。",
                    detail={"command": self._name, "error_type": type(error).__name__},
                )
            )


def build_handlers(
    ctx: PluginContext, settings: CommandsSettings
) -> dict[str, tuple[CommandSpec, _Handler]]:
    """构造本次要注册的命令。**只包含 `settings.enabled` 里的那些**。

    与 manifest 的声明过滤同源于同一份配置（见 `settings.enabled_command_names`）——
    这是 `TOL-006` 那条机制成立的全部条件。
    """
    return {
        name: (SPECS[name], _Handler(name, _BODIES[name], ctx, settings))
        for name in sorted(settings.enabled)
    }
