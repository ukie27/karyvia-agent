"""连接的所有者：一条后台任务，从连上到关掉都在它里面。

职责：在**一个任务**里打开全部 server 连接、把工具表交给 `setup()`、然后一直待到被取消，
在 `finally` 里关掉连接。
不负责：怎么连（`client.py`）、连上之后叫什么名字（`naming.py`）、怎么调（`tool.py`）。

**为什么连接必须由一条后台任务拥有，而不是在 `setup()` 里打开、在别处关掉**：

- `mcp` 的三种传输都建在 anyio 的任务组上，而**任务组必须在进入它的那个任务里退出**。
  在 `setup()` 里 `enter_async_context`、再由停止路径去 `aclose()`，会在关闭时炸出
  `RuntimeError: Attempted to exit cancel scope in a different task`。因此连接必须由
  打开 transport 的同一个任务关闭。
- 本插件没有第二条清理通道：manifest **没有** teardown 字段， 的取消痕迹清理
  只作用在 `ctx.spawn_task()` 派生的任务上（`runtime/plugin_context.py::shutdown`）。

于是形状定成：`setup()` 派生这条任务 → 等它把工具表准备好 → 用那张表注册 → 返回。
停止时 `shutdown()` 取消它，`finally` 在**同一个任务**里关掉 `AsyncExitStack`。
预算是 `plugins.stop_timeout_ms`（默认 5000，每插件各算一份）。

**单个 server 连不上不致命**：记进 `failures`、跳过它的工具，其余照常。一个拼错的
server 配置不该让另外三个也用不成。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

from .naming import NameAssignment, assign_names
from .session import Connector, McpSession, RemoteTool
from .settings import McpSettings, ServerSettings

__all__ = ["ConnectionSupervisor", "DiscoveredTool", "Discovery"]


@dataclass(frozen=True, slots=True)
class DiscoveredTool:
    """一条准备注册的桥接工具。"""

    local_name: str
    server: str
    remote: RemoteTool


@dataclass(frozen=True, slots=True)
class Discovery:
    """一次发现的全部产出：能注册的工具、每个 server 的会话、以及各类问题。"""

    tools: tuple[DiscoveredTool, ...] = ()
    sessions: Mapping[str, McpSession] = field(default_factory=dict[str, McpSession])
    #: server 名 → 失败原因的**类型名**。不放异常消息：第三方 server 的异常文本可能
    #: 带着它自己的凭据。
    failures: Mapping[str, str] = field(default_factory=dict[str, str])
    #: server 名 → 那个 server 的命名结果（撞车与被拒的原名都在里面）。
    naming: Mapping[str, NameAssignment] = field(default_factory=dict[str, NameAssignment])


class ConnectionSupervisor:
    """拥有全部连接的后台任务。"""

    __slots__ = ("_connector", "_discovery", "_ready", "_settings")

    def __init__(self, settings: McpSettings, connector: Connector) -> None:
        self._settings = settings
        self._connector = connector
        self._ready = asyncio.Event()
        self._discovery = Discovery()

    @property
    def discovery(self) -> Discovery:
        """发现结果。只有在 `wait_ready()` 返回之后才有意义。"""
        return self._discovery

    async def run(self) -> None:
        """后台任务的主体。**由 `ctx.spawn_task()` 派生，不要直接 await 它。**

        **异常约定**：不抛。连接失败逐个记进 `Discovery.failures`；无论成败都要
        `set()` 那个事件，否则 `setup()` 会一直等下去（那是启动路径上最糟的一种失败）。
        """
        async with AsyncExitStack() as stack:
            try:
                self._discovery = await self._connect_all(stack)
            finally:
                # **无论如何都放行 `setup()`**：连接全挂了也要让实例起得来。
                self._ready.set()
            # 一直待着直到被取消。取消时 `async with` 的退出发生在**这个任务**里，
            # 那正是本类存在的理由（见模块 docstring）。用一个永不被 `set()` 的事件
            # 而不是 `while True: sleep()`：后者每隔一段就醒一次，只为了再睡一次。
            await asyncio.Event().wait()

    async def wait_ready(self, timeout_ms: int) -> bool:
        """等发现完成。返回 `False` 表示等超时了（那时 `discovery` 还是空的）。

        超时**不取消那条任务**：它可能只是慢，而取消它会把已经连上的 server 一起关掉。
        插件因此在这一轮少注册几条工具，下次启动再试——这与「单个 server 连不上不致命」
        是同一条取舍。
        """
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout_ms / 1000)
        except TimeoutError:
            return False
        return True

    async def _connect_all(self, stack: AsyncExitStack) -> Discovery:
        tools: list[DiscoveredTool] = []
        sessions: dict[str, McpSession] = {}
        failures: dict[str, str] = {}
        naming: dict[str, NameAssignment] = {}
        for server in self._settings.enabled_servers:
            outcome = await self._connect_one(server, stack)
            if isinstance(outcome, str):
                failures[server.name] = outcome
                continue
            session, remote_tools = outcome
            sessions[server.name] = session
            assignment = assign_names(self._settings.prefix, server.name, remote_tools)
            naming[server.name] = assignment
            tools.extend(
                DiscoveredTool(local_name=local, server=server.name, remote=remote)
                for local, remote in sorted(assignment.assigned.items())
            )
        return Discovery(
            tools=tuple(tools), sessions=sessions, failures=failures, naming=naming
        )

    async def _connect_one(
        self, server: ServerSettings, stack: AsyncExitStack
    ) -> tuple[McpSession, Sequence[RemoteTool]] | str:
        """连上一个 server 并取它的工具表。失败时返回失败原因的**类型名**。"""
        budget = self._settings.connect_timeout_ms / 1000
        try:
            async with asyncio.timeout(budget):
                session = await self._connector.connect(server, stack)
                return session, await session.list_tools()
        except TimeoutError:
            return "TimeoutError"
        except Exception as error:  # noqa: BLE001 - 第三方传输什么都可能抛
            # **捕 `Exception` 不捕 `BaseException`**：取消要放行，否则停机路径会被这里
            # 吞掉一次（engine 与 dispatcher 采用的相同规则）。
            return type(error).__name__
