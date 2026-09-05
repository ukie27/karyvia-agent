"""`mcp` 插件用例的替身与工厂。**模块名带插件前缀**是刻意的。

`testpaths` 一次收集整个 `plugins/`，而 pytest 按模块名去重：两个插件各有一个
`_fakes.py` 时，先导入的会顶掉后一个，另一棵测试树整体 `ImportError`。
**单独跑各自目录看不出来，跑全量才炸**（`D34` 就是这么发现的）。

职责：一个不碰 `mcp` SDK 的 `McpSession` / `Connector` 替身、一个记录注册动作的
`KaryviaAPI` 替身、以及构造 `ToolInvocation` 的小工厂。
不负责：断言（在各 `test_mcp_*.py` 里）。

**这些替身是「不装 `mcp` 也能全绿」这条承诺的兑现方式**：它们只实现 `session.py` 的两个
Protocol，一个 SDK 类型都不认识。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from pathlib import Path

from karyvia_plugin_mcp import RemoteResult, RemoteTool, ServerSettings

from karyvia.contracts import (
    JsonValue,
    ToolCall,
    ToolHandler,
    ToolInvocation,
    ToolSpec,
)
from karyvia.sdk.testing import FakePluginContext, make_correlation

READ_TOOL = RemoteTool(
    name="read_file",
    description="读一个文件",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
)
WRITE_TOOL = RemoteTool(name="write-file", description="写一个文件")


class FakeSession:
    """一个不碰 SDK 的 `McpSession`。"""

    def __init__(
        self,
        tools: Sequence[RemoteTool] = (READ_TOOL,),
        *,
        result: RemoteResult | None = None,
        error: BaseException | None = None,
        delay: float = 0.0,
    ) -> None:
        self._tools = tuple(tools)
        self._result = result if result is not None else RemoteResult(text="ok")
        self._error = error
        self._delay = delay
        self.calls: list[tuple[str, Mapping[str, JsonValue], int]] = []
        self.closed = False

    async def list_tools(self) -> Sequence[RemoteTool]:
        return self._tools

    async def call_tool(
        self, name: str, arguments: Mapping[str, JsonValue], *, timeout_ms: int
    ) -> RemoteResult:
        self.calls.append((name, dict(arguments), timeout_ms))
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return self._result


class FakeConnector:
    """按 server 名回一个会话，或抛一个预置的错误。

    **它把关闭动作登记进 `AsyncExitStack`**，因此「取消那条后台任务真的关掉了连接」
    在用例里是可断言的——那正是 `supervisor.py` 存在的全部理由。
    """

    def __init__(
        self,
        sessions: Mapping[str, FakeSession] | None = None,
        *,
        failures: Mapping[str, BaseException] | None = None,
        delay: float = 0.0,
    ) -> None:
        self._sessions = dict(sessions or {})
        self._failures = dict(failures or {})
        self._delay = delay
        self.connected: list[ServerSettings] = []

    async def connect(self, server: object, stack: AsyncExitStack) -> FakeSession:
        assert isinstance(server, ServerSettings)
        self.connected.append(server)
        if self._delay:
            await asyncio.sleep(self._delay)
        failure = self._failures.get(server.name)
        if failure is not None:
            raise failure
        session = self._sessions.get(server.name) or FakeSession()
        stack.push_async_callback(_closer(session))
        return session


def _closer(session: FakeSession):
    async def close() -> None:
        session.closed = True

    return close


class RecordingApi:
    """只记录注册动作的最小 `KaryviaAPI` 替身。"""

    def __init__(self, ctx: FakePluginContext) -> None:
        self._ctx = ctx
        self.tools: list[tuple[ToolSpec, ToolHandler]] = []

    @property
    def ctx(self) -> FakePluginContext:
        return self._ctx

    def register_tool(self, spec: ToolSpec, handler: ToolHandler) -> None:
        self.tools.append((spec, handler))

    @property
    def names(self) -> list[str]:
        return [spec.name for spec, _ in self.tools]


class McpContext(FakePluginContext):
    """真的派生后台任务的上下文。

    `FakePluginContext.spawn_task` 只记名字不起协程（`D16` 的它不需要真跑），而本插件的
    连接**就在那条任务里**——用那个替身会让整套生命周期用例验的是一台不存在的机器。
    """

    def __init__(
        self,
        *,
        config: Mapping[str, JsonValue] | None = None,
        secrets: Mapping[str, str] | None = None,
        state_dir: Path | None = None,
    ) -> None:
        super().__init__(
            "mcp", config=config, secrets=secrets, state_dir=state_dir
        )
        self.spawned: list[asyncio.Task[None]] = []

    def spawn_task(self, coro: object, *, name: str) -> None:
        self.tasks.append(name)
        self.spawned.append(asyncio.get_running_loop().create_task(_as_coroutine(coro)))

    async def shutdown(self) -> None:
        """`RuntimePluginContext.shutdown()` 的最小同位物：取消并等回收。"""
        for task in self.spawned:
            task.cancel()
        if self.spawned:
            await asyncio.gather(*self.spawned, return_exceptions=True)


async def _as_coroutine(awaitable: object) -> None:
    assert hasattr(awaitable, "__await__")
    await awaitable  # pyright: ignore[reportGeneralTypeIssues]


def invocation(name: str, arguments: Mapping[str, JsonValue]) -> ToolInvocation:
    return ToolInvocation(
        call=ToolCall(call_id="call-1", name=name, arguments=dict(arguments)),
        correlation=make_correlation(),
        timeout_ms=5_000,
    )
