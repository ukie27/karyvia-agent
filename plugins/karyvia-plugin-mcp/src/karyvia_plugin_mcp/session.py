"""本插件自己的会话抽象：`McpSession` / `Connector` 两个 Protocol 与三个纯数据类型。

职责：描述「一个已经连上的 MCP server 能做什么」，不引用 `mcp` 包的任何类型。
不负责：真的连上去（`client.py`）、翻译线格式（`translate.py`）、管理生命周期
（`supervisor.py`）。

**这是整棵测试树的支点。** 只有 `client.py` import `mcp`，其余全部对这两个 Protocol
编程，因此**没装 `mcp` 包的环境里测试仍须全绿**（CI 用 `--no-deps` 装插件）。legacy
的做法是在测试文件第 11 行写
`pytest.importorskip("mcp")`，CI 没装依赖时整棵树静默全跳。

**平台 SDK 对象在 `to_remote_tool` 之后就不存在了**（同一条精神）：
判定与归一化只认识这里的 frozen dataclass。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from karyvia.contracts import JsonSchema, JsonValue

__all__ = [
    "Connector",
    "McpSession",
    "RemoteResult",
    "RemoteTool",
    "ServerHandle",
]


@dataclass(frozen=True, slots=True)
class RemoteTool:
    """远端 server 上的一个工具。

    `input_schema` 保持**原样的 JSON Schema**：kernel 的 `ToolInvoker._compile()` 会拿它
    校验参数，我们不该在中间改写它的语义。归一化（补 `type: object` 之类）在
    `translate.py` 一处完成。
    """

    name: str
    description: str
    input_schema: JsonSchema = field(default_factory=lambda: {"type": "object"})


@dataclass(frozen=True, slots=True)
class RemoteResult:
    """一次远端调用的结果。

    `is_error` 是 MCP 的 `isError`：它表示**工具自己报告失败**，而不是传输层出错。
    两者必须分得开——前者是模型该看到并据此改主意的产出，后者是这次调用没能给出结论。
    """

    text: str
    is_error: bool = False
    structured: JsonValue | None = None
    #: 非文本内容部件的摘要（图像、资源引用）。契约的 `ToolResult.content` 是纯文本，
    #: 因此这些部件只能以一行说明的形式出现——**说明它存在，而不是假装没有**。
    attachments: tuple[str, ...] = ()


@runtime_checkable
class McpSession(Protocol):
    """一个已经初始化完成的 MCP 会话。"""

    async def list_tools(self) -> Sequence[RemoteTool]:
        """列出远端工具。**异常约定**：连接问题原样抛出，由调用方折成本 server 的失败。"""
        ...

    async def call_tool(
        self, name: str, arguments: Mapping[str, JsonValue], *, timeout_ms: int
    ) -> RemoteResult:
        """调用一个远端工具。`name` 是**远端原名**，不是本地注册名。

        **异常约定**：超时抛 `TimeoutError`，其余传输层问题原样抛出。工具自身报告的失败
        不抛，走 `RemoteResult.is_error`。
        """
        ...


@dataclass(frozen=True, slots=True)
class ServerHandle:
    """一个连上了的 server：它的会话与它交出来的工具表。"""

    server: str
    session: McpSession
    tools: tuple[RemoteTool, ...]


class Connector(Protocol):
    """把一份 server 配置变成一个会话。

    `stack` 由调用方拥有：**进入与退出必须发生在同一个任务里**（anyio 的任务组要求，
    `supervisor.py` 的 docstring 有完整解释），因此这里只接收它、不创建也不关闭它。
    """

    async def connect(self, server: object, stack: AsyncExitStack) -> McpSession:
        """连上并完成初始化。**异常约定**：连不上原样抛出。"""
        ...
