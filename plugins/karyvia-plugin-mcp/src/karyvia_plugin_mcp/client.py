"""`mcp` SDK 的适配层。**全插件唯一 import `mcp` 的模块。**

职责：按传输类型连上一个 server，把 `mcp` 的 `ClientSession` 包成本插件的 `McpSession`。
不负责：判定配置（`settings.py`）、命名（`naming.py`）、生命周期（`supervisor.py`）。

**这是整棵测试树的支点**：其余模块全部对 `session.py` 的两个 Protocol 编程，因此
**没装 `mcp` 包的环境里测试仍须全绿**——
CI 用 `--no-deps` 装插件，而本模块只在真的要连一个 server 时才被 import。
唯一需要碰它的用例是「没装它时说什么」。

**`mcp` 惰性 import**：一个启用了本插件但一台 server 都没配的实例，不该为它付
导入开销。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from typing import Any, Final

from karyvia.contracts import ErrorCode, JsonValue, KaryviaError

from .session import McpSession, RemoteResult, RemoteTool
from .settings import ServerSettings
from .translate import summarise_parts

__all__ = ["SdkConnector", "SdkSession", "require_mcp"]

_MISSING_SDK: Final = (
    "没有安装 mcp 客户端库。请运行 "
    "`pip install 'karyvia-plugin-mcp[client]'`，或直接 `pip install mcp`。"
)


def require_mcp() -> None:
    """确认 `mcp` 装上了。**异常约定**：没装抛 `CAPABILITY_MISSING` 并给出安装命令。

    这条错误的价值全在那句能照做的话上——`ImportError: No module named 'mcp'`
    从装配根冒出来时，用户看不出该装哪个包。
    """
    import importlib.util  # noqa: PLC0415 - 惰性，见模块 docstring

    if importlib.util.find_spec("mcp") is None:
        raise KaryviaError(ErrorCode.CAPABILITY_MISSING, _MISSING_SDK)


class SdkSession:
    """把 `mcp.ClientSession` 包成本插件的 `McpSession`。

    **SDK 对象到这里为止**：`list_tools()` / `call_tool()` 交出去的全是本插件自己的
    frozen dataclass（同一条精神）。`Any` 只出现在这一层，每一处都带
    `# boundary:`。
    """

    __slots__ = ("_session",)

    def __init__(self, session: object) -> None:
        self._session = session

    async def list_tools(self) -> Sequence[RemoteTool]:
        session: Any = self._session  # boundary: mcp 的 ClientSession 没有可依赖的类型
        listing = await session.list_tools()
        return tuple(
            RemoteTool(
                name=str(tool.name),
                description=str(getattr(tool, "description", "") or ""),
                input_schema=_schema_of(tool),
            )
            for tool in listing.tools
        )

    async def call_tool(
        self, name: str, arguments: Mapping[str, JsonValue], *, timeout_ms: int
    ) -> RemoteResult:
        import asyncio  # noqa: PLC0415 - 与 mcp 同批惰性

        session: Any = self._session  # boundary: 同上
        async with asyncio.timeout(timeout_ms / 1000):
            raw = await session.call_tool(name, dict(arguments))
        text, attachments = summarise_parts(_parts_of(raw))
        return RemoteResult(
            text=text,
            is_error=bool(getattr(raw, "isError", False)),
            structured=_structured_of(raw),
            attachments=attachments,
        )


class SdkConnector:
    """按传输类型建立连接。三种传输各写各的——它们的参数集合没有公共部分。"""

    async def connect(self, server: object, stack: AsyncExitStack) -> McpSession:
        """连上并完成初始化。**异常约定**：连不上原样抛出，由 `supervisor.py` 折成失败。"""
        require_mcp()
        assert isinstance(server, ServerSettings)  # noqa: S101 - Protocol 用 object 收窄
        from mcp import ClientSession  # noqa: PLC0415 - 惰性，见模块 docstring

        read, write = await self._streams(server, stack)
        session: Any = await stack.enter_async_context(  # boundary: SDK 无类型
            ClientSession(read, write)
        )
        await session.initialize()
        return SdkSession(session)

    async def _streams(
        self, server: ServerSettings, stack: AsyncExitStack
    ) -> tuple[object, object]:
        if server.transport == "stdio":
            return await self._stdio(server, stack)
        if server.transport == "sse":
            from mcp.client.sse import sse_client  # noqa: PLC0415

            streams: Any = await stack.enter_async_context(  # boundary: SDK 无类型
                sse_client(server.url, headers=dict(server.headers))
            )
            return streams[0], streams[1]
        from mcp.client.streamable_http import streamablehttp_client  # noqa: PLC0415

        opened: Any = await stack.enter_async_context(  # boundary: SDK 无类型
            streamablehttp_client(server.url, headers=dict(server.headers))
        )
        # streamable HTTP 交出三个：读、写、以及一个取 session id 的回调。
        return opened[0], opened[1]

    async def _stdio(
        self, server: ServerSettings, stack: AsyncExitStack
    ) -> tuple[object, object]:
        from mcp import StdioServerParameters  # noqa: PLC0415
        from mcp.client.stdio import stdio_client  # noqa: PLC0415

        parameters = StdioServerParameters(
            command=server.command,
            args=list(server.args),
            # **`env=None` 时 SDK 用它自己的安全基线**（一小组让程序起得来的变量）。
            # 配了 `env` 就在那之上叠加——`tools_shell/environ.py` 的白名单精神：
            # 父进程的凭据默认一个字节都不进子进程。
            env=dict(server.env) or None,
            cwd=server.cwd or None,
        )
        streams: Any = await stack.enter_async_context(  # boundary: SDK 无类型
            stdio_client(parameters)
        )
        return streams[0], streams[1]


def _schema_of(tool: object) -> dict[str, JsonValue]:
    schema: Any = getattr(tool, "inputSchema", None)  # boundary: SDK 无类型
    return dict(schema) if isinstance(schema, Mapping) else {}


def _parts_of(raw: object) -> tuple[Mapping[str, JsonValue], ...]:
    """把 SDK 的 content 部件摊成普通映射。

    用 `model_dump()` 而不是逐个 `getattr`：部件类型有五六种且还在增加，而
    `summarise_parts()` 已经按 `type` 字段分派——摊成 JSON 之后那条分派对新类型
    自动成立（认不出的类型会得到一行「有一个 X 类型的部件」）。
    """
    content: Any = getattr(raw, "content", ()) or ()  # boundary: SDK 无类型

    parts: list[Mapping[str, JsonValue]] = []
    for part in content:
        dump = getattr(part, "model_dump", None)
        parts.append(dump(mode="json") if callable(dump) else {"type": "未知"})
    return tuple(parts)


def _structured_of(raw: object) -> JsonValue | None:
    value: Any = getattr(raw, "structuredContent", None)  # boundary: SDK 无类型
    return dict(value) if isinstance(value, Mapping) else None
