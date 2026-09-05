"""CLI 的 `Channel` 一侧：把控制台接到与其它平台完全相同的契约路径上（`MSG-004`、`MSG-007`）。

职责：实现 `contracts.Channel` 的四个成员，正文全部委托给 `CliConsole`。
不负责：读 stdin、渲染细节、把消息喂给 orchestrator（那是装配根的 Channel 泵）。

**CLI 是一个 Channel 而不是一条捷径**：控制台消息与其他平台经过相同的契约、Routing 和
Turn 路径，并由 `ChannelContract` 验证。`CliEntry` 因此不需要在 `PluginContext` 上另开
一条提交消息的私有通道。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from karyvia.contracts import InboundMessage, OutboundMessage

from .console import CliConsole

__all__ = ["CliChannel"]


class CliChannel:
    """本地终端接入。`start()` / `stop()` 只切换控制台的开合，没有任何连接要建。"""

    def __init__(self, console: CliConsole) -> None:
        self._console = console

    @property
    def channel_id(self) -> str:
        return self._console.channel_id

    async def start(self) -> None:
        """无连接可建。**不在这里读 stdin**——那属于 `CliEntry`，它拥有进程。"""
        return None

    async def stop(self) -> None:
        """关闭入站流。**约定不抛**且幂等（`ChannelContract` 直接测这一条）。"""
        self._console.close()

    def receive(self) -> AsyncIterator[InboundMessage]:
        return self._console.messages()

    async def deliver(self, message: OutboundMessage) -> None:
        await self._console.deliver(message)
