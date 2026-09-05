"""`mcp` 插件的端到端用例（开发方案 `D38-B`）：命名空间声明在**真实装配路径**上成立。

职责：验「远端工具名在 manifest 里一个字都没写，却真的进了 registry、真的转发到远端原名、
参数 schema 真的是远端那份」。
不负责：插件自身的线格式与命名规则（`plugins/karyvia-plugin-mcp/tests/`）、
机制本身的单元判定（`tests/kernel/test_host.py`）。

**这里唯一的替身是 `Connector`**（一个不碰 `mcp` SDK、也不开任何连接的假连接器）：
`wire_capabilities`、registry、`CapabilityHost` 的命名空间回查全是生产实现。
把 `CapabilityHost` 也换掉，这套用例就退化成 `tests/kernel/` 的重复。

因此本文件要求 mcp 插件已经装进当前环境：

    karyvia plugins install --no-deps plugins/karyvia-plugin-mcp
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack

from karyvia.contracts import (
    Builtin,
    CapabilityKind,
    JsonValue,
    ToolCall,
    ToolInvocation,
    ToolSpec,
)
from karyvia.kernel.registry import CapabilityRegistry
from karyvia.kernel.turn import tools_from
from karyvia.runtime.plugin_home import GlobalPluginHome
from karyvia.runtime.wiring import wire_capabilities
from karyvia.sdk.testing import FakePluginContext, ManualCancel, make_correlation

MCP_PLUGIN = "mcp"


class _Session:
    """两条远端工具，名字与 schema 都只有连上之后才知道。"""

    async def list_tools(self) -> Sequence[object]:
        from karyvia_plugin_mcp import RemoteTool

        return (
            RemoteTool(
                name="read-file",
                description="读一个文件",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            ),
            RemoteTool(name="list_dir", description="列目录"),
        )

    async def call_tool(
        self, name: str, arguments: Mapping[str, JsonValue], *, timeout_ms: int
    ) -> object:
        from karyvia_plugin_mcp import RemoteResult

        del timeout_ms
        return RemoteResult(text=f"{name} 收到 {json.dumps(dict(arguments), ensure_ascii=False)}")


class _Connector:
    async def connect(self, server: object, stack: AsyncExitStack) -> _Session:
        del server, stack
        return _Session()


class _Context(FakePluginContext):
    """真的派生后台任务：本插件的连接就在那条任务里。

    真实装配根会在 `AgentInstance.stop()` 里统一取消它们（`EDG-105`），这里由用例自己
    在 `finally` 中做同一件事。
    """

    def __init__(self) -> None:
        super().__init__(
            "mcp", config={"servers": {"files": {"type": "stdio", "command": "a"}}}
        )
        self.spawned: list[asyncio.Task[None]] = []

    def spawn_task(self, coro: object, *, name: str) -> None:
        self.tasks.append(name)
        self.spawned.append(asyncio.get_running_loop().create_task(_drive(coro)))

    async def shutdown(self) -> None:
        for task in self.spawned:
            task.cancel()
        if self.spawned:
            await asyncio.gather(*self.spawned, return_exceptions=True)


async def _drive(awaitable: object) -> None:
    await awaitable  # pyright: ignore[reportGeneralTypeIssues]


async def _wire() -> tuple[CapabilityRegistry, _Context]:
    """走生产装配路径把插件装进一个 registry。"""
    from karyvia_plugin_mcp import MANIFEST, register

    ctx = _Context()

    async def setup(api: object) -> None:
        await register(api, ctx, _Connector())  # pyright: ignore[reportArgumentType]

    wiring = await wire_capabilities(
        manifests=(MANIFEST,),
        context_for=lambda _: ctx,  # pyright: ignore[reportArgumentType]
        provider_for=lambda _: Builtin(),
        resolve_setup=lambda _: setup,  # pyright: ignore[reportArgumentType]
    )
    wiring.report.raise_if_failed()
    return wiring.registry, ctx


def _specs(registry: CapabilityRegistry) -> dict[str, ToolSpec]:
    return {tool.spec.name: tool.spec for tool in tools_from(registry)}


def test_the_mcp_plugin_is_installed_as_an_entry_point() -> None:
    """整套用例的前提。**单独成一条**：装漏了要看到一句能照做的话。"""
    names = {item.plugin_id for item in GlobalPluginHome.resolve().catalog()}
    assert MCP_PLUGIN in names, (
        "mcp 插件没装。请先跑 `karyvia plugins install --no-deps plugins/karyvia-plugin-mcp`"
    )


def test_the_manifest_declares_names_it_cannot_know() -> None:
    """整条机制的出发点：manifest 里只有一个前缀，两条远端工具名一个字都没写。"""
    from karyvia_plugin_mcp import MANIFEST

    declared = [(decl.kind, decl.name, decl.namespace) for decl in MANIFEST.capabilities]
    assert declared == [(CapabilityKind.TOOL, "mcp", True)]


async def test_remote_tools_reach_the_registry_under_the_namespace() -> None:
    """`D38-A` 的端到端形态：`CapabilityHost` 按前缀回查放行了两次未声明的注册，
    而 `finish()` 没有因为「声明了却没注册」把整个提供方判失败。"""
    registry, ctx = await _wire()
    try:
        assert set(_specs(registry)) == {"mcp.files.read_file", "mcp.files.list_dir"}
    finally:
        await ctx.shutdown()


async def test_the_remote_schema_is_what_the_kernel_will_validate_against() -> None:
    """参数校验留在 kernel 的 `ToolInvoker`——这正是不做成单条 `mcp.call` 代理工具的
    全部理由：那样模型拿不到每个远端工具的 schema，校验也就退到运行期了。"""
    registry, ctx = await _wire()
    try:
        read = _specs(registry)["mcp.files.read_file"]
        assert read.parameters["required"] == ["path"]
        assert read.parameters["additionalProperties"] is False
    finally:
        await ctx.shutdown()


async def test_a_tool_without_a_remote_schema_still_gets_an_object_schema() -> None:
    """契约要求 `spec.parameters["type"] == "object"`，而不少 server 省略它。"""
    registry, ctx = await _wire()
    try:
        listing = _specs(registry)["mcp.files.list_dir"]
        assert listing.parameters["type"] == "object"
    finally:
        await ctx.shutdown()


async def test_a_registered_tool_forwards_to_the_remote_name() -> None:
    """本地叫 `mcp.files.read_file`，远端收到的必须是它自己的 `read-file`。"""
    registry, ctx = await _wire()
    try:
        handler = {tool.spec.name: tool.handler for tool in tools_from(registry)}[
            "mcp.files.read_file"
        ]
        result = await handler.execute(_invocation({"path": "a.txt"}), ManualCancel())
        assert result.ok is True
        assert "read-file" in result.content
    finally:
        await ctx.shutdown()


async def test_cancelling_the_supervisor_task_is_how_connections_close() -> None:
    """manifest 没有 teardown 字段，`ctx.spawn_task()` 派生的那条任务是唯一的清理通道。"""
    _, ctx = await _wire()
    assert ctx.tasks == ["connections"]

    await ctx.shutdown()

    assert all(task.done() for task in ctx.spawned)


def _invocation(arguments: dict[str, JsonValue]) -> ToolInvocation:
    return ToolInvocation(
        call=ToolCall(call_id="call-1", name="mcp.files.read_file", arguments=arguments),
        correlation=make_correlation(),
        timeout_ms=5_000,
    )
