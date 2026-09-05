"""`contracts.Channel` 的实现：起 HTTP 服务、接入站、投出站。

职责：实现 `Channel` 的四个成员；`start()` 里惰性 import `aiohttp` 并绑端口，
`stop()` 收尾（约定不抛、幂等），`receive()` / `deliver()` 委托给 `SessionHub`。
不负责：路由与协议细节（`http.py`）、配置校验（`settings.py`）。

**`aiohttp` 在这里才 import**：插件被发现、被加载、被注册的路径上都不该付这笔钱，
而且没装 `[server]` extra 的用户应当拿到一句能照做的话，不是一条 traceback。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Final, cast

from karyvia.contracts import ErrorCode, InboundMessage, KaryviaError, OutboundMessage

from .hub import SessionHub

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from aiohttp import web

__all__ = ["ApiChannel"]

_MISSING_AIOHTTP: Final = (
    "OpenAI 兼容接口需要 aiohttp，当前环境没有装。"
)


class ApiChannel:
    """HTTP 接入。一个实例最多一个——它绑一个端口。"""

    def __init__(self, hub: SessionHub, *, api_key: str | None = None) -> None:
        self._hub = hub
        self._api_key = api_key
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        #: 实际绑到的端口。配 `port=0` 时才与配置值不同（测试用它）。
        self.bound_port: int | None = None

    @property
    def channel_id(self) -> str:
        return self._hub.settings.channel_id

    async def start(self) -> None:
        """绑端口。失败时抛 `EXTERNAL_CHANNEL`（`Channel.start` 的异常约定）。"""
        try:
            from aiohttp import web
        except ImportError as exc:
            raise KaryviaError(
                ErrorCode.EXTERNAL_CHANNEL,
                _MISSING_AIOHTTP,
                detail={
                    "package": "aiohttp",
                    "fix": 'pip install "karyvia-plugin-openai-api[server]"',
                },
            ) from exc

        from .http import build_app

        settings = self._hub.settings
        runner = web.AppRunner(build_app(self._hub, api_key=self._api_key))
        await runner.setup()
        site = web.TCPSite(runner, settings.host, settings.port)
        try:
            await site.start()
        except OSError as exc:
            await runner.cleanup()
            raise KaryviaError(
                ErrorCode.EXTERNAL_CHANNEL,
                "OpenAI 兼容接口绑定端口失败。",
                detail={"host": settings.host, "port": settings.port, "errno": exc.errno},
            ) from exc
        self._runner = runner
        self._site = site
        self.bound_port = _actual_port(site, default=settings.port)

    async def stop(self) -> None:
        """关服务与入站流。**约定不抛**且幂等（`ChannelContract` 直接测这一条）。"""
        self._hub.close()
        site, self._site = self._site, None
        runner, self._runner = self._runner, None
        if site is not None:
            await _quietly(site.stop())
        if runner is not None:
            # Windows 的 ProactorEventLoop 上 cleanup() 偶尔会在关闭中的传输上抛，
            # 而这条路径的约定是不抛——一次收尾失败不该盖住其余收尾。
            await _quietly(runner.cleanup())

    def receive(self) -> AsyncIterator[InboundMessage]:
        return self._hub.messages()

    async def deliver(self, message: OutboundMessage) -> None:
        """投给对应的在途请求。**约定不抛**。"""
        if message.metadata.get("reasoning") and not self._hub.settings.show_reasoning:
            return
        self._hub.route(message)


def _actual_port(site: web.TCPSite, *, default: int) -> int:
    """取真实绑到的端口。`port=0` 时配置值是 0，真实值只能问 socket。"""
    server: object = getattr(site, "_server", None)
    sockets: object = getattr(server, "sockets", None) or ()
    if not isinstance(sockets, Sequence):
        return default
    for sock in cast("Sequence[object]", sockets):
        # boundary: `getattr` 交回 `Any`，这里逐层收窄——`getsockname()` 的返回形状
        # 按协议族不同（AF_INET 是二元组、AF_INET6 是四元组），只取第二项。
        address: object = getattr(sock, "getsockname", lambda: None)()
        if not isinstance(address, tuple):
            continue
        parts = cast("tuple[object, ...]", address)
        if len(parts) >= 2 and isinstance(parts[1], int):
            return parts[1]
    return default


async def _quietly(awaitable: object) -> None:
    """跑一件收尾，异常吞掉。`BaseException` 放行（取消要能穿透）。"""
    try:
        await awaitable  # type: ignore[misc]  # boundary: aiohttp 的收尾协程无统一类型
    except Exception:  # noqa: BLE001 - 见 docstring
        return
