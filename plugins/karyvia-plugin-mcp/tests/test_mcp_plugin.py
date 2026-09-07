"""行为用例：桥接工具、连接生命周期、注册面、manifest。

**一个 `mcp` SDK 符号都不碰**——全部走 `_mcp_fakes.py` 的 `Connector` / `McpSession` 替身。
唯一需要碰 SDK 的是最后那条「没装它时说什么」。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from _mcp_fakes import (
    READ_TOOL,
    WRITE_TOOL,
    FakeConnector,
    FakeSession,
    McpContext,
    RecordingApi,
    invocation,
)
from karyvia_plugin_mcp import (
    CONFIG_SCHEMA,
    MANIFEST,
    NAMESPACE,
    BridgedTool,
    RemoteResult,
    RemoteTool,
    SessionSource,
    register,
    tool_spec,
)

from karyvia.contracts import (
    CapabilityKind,
    Concurrency,
    ErrorCode,
    JsonValue,
    KaryviaError,
    RiskLevel,
    SideEffect,
    ToolResult,
)
from karyvia.sdk.testing import ManualCancel, ToolContract


def _bridged(session: FakeSession | None, **kwargs: object) -> BridgedTool:
    return BridgedTool(
        SessionSource(session),
        "files",
        "read_file",
        timeout_ms=int(kwargs.get("timeout_ms", 1_000)),  # pyright: ignore[reportArgumentType]
        limit=int(kwargs.get("limit", 1_000)),  # pyright: ignore[reportArgumentType]
    )


async def _call(tool: BridgedTool, arguments: dict[str, JsonValue] | None = None) -> ToolResult:
    return await tool.execute(
        invocation("mcp.files.read_file", arguments or {"path": "a.txt"}), ManualCancel()
    )


class TestBridgedTool:
    async def test_a_call_is_forwarded_with_the_remote_name(self) -> None:
        """模型看到的是本地名，远端收到的必须是它自己的原名。"""
        session = FakeSession(result=RemoteResult(text="hello"))
        result = await _call(_bridged(session))
        assert result.ok is True
        assert result.content == "hello"
        assert session.calls[0][0] == "read_file"
        assert session.calls[0][1] == {"path": "a.txt"}

    async def test_success_still_reports_an_unknown_side_effect(self) -> None:
        """MCP 协议不报告副作用：一个写文件的远端工具与一个只读的长得一模一样。"""
        result = await _call(_bridged(FakeSession()))
        assert result.side_effect is SideEffect.UNKNOWN

    async def test_a_remote_reported_failure_is_not_a_transport_failure(self) -> None:
        """模型该看到那段文本并据此改主意，而不是只看到「调用失败了」。"""
        session = FakeSession(result=RemoteResult(text="路径不存在", is_error=True))
        result = await _call(_bridged(session))
        assert result.ok is False
        assert "路径不存在" in result.content
        assert result.error is not None
        assert result.error.code is ErrorCode.EXTERNAL_TOOL_SERVER

    async def test_structured_content_lands_in_data(self) -> None:
        session = FakeSession(result=RemoteResult(text="ok", structured={"rows": 2}))
        result = await _call(_bridged(session))
        assert result.data is not None
        assert result.data["structured"] == {"rows": 2}

    async def test_a_missing_session_fails_before_anything_goes_out(self) -> None:
        """断连之后每次调用都如实报，而不是拿着一个死对象。"""
        result = await _call(_bridged(None))
        assert result.ok is False
        assert result.side_effect is SideEffect.NONE
        assert result.error is not None
        assert result.error.code is ErrorCode.CAPABILITY_MISSING

    async def test_a_timeout_is_unknown_because_the_request_already_went_out(self) -> None:
        """谎报 `NONE` 会让编排层以为可以安全重试一次可能已经生效的写操作。"""
        session = FakeSession(error=TimeoutError())
        result = await _call(_bridged(session))
        assert result.side_effect is SideEffect.UNKNOWN
        assert result.error is not None
        assert result.error.code is ErrorCode.TIMEOUT_TOOL_CALL

    async def test_a_transport_error_reports_only_the_exception_type(self) -> None:
        """第三方 server 的异常文本可能带着它自己的凭据。"""
        session = FakeSession(error=RuntimeError("token=sk-abcdefghijklmnop failed"))
        result = await _call(_bridged(session))
        assert result.error is not None
        assert result.error.detail["cause"] == "RuntimeError"
        assert "sk-abcdefghijklmnop" not in repr(result.error)
        assert result.side_effect is SideEffect.UNKNOWN

    async def test_cancellation_at_the_entry_never_reaches_the_server(self) -> None:
        session = FakeSession()
        cancel = ManualCancel()
        cancel.request()
        result = await _bridged(session).execute(
            invocation("mcp.files.read_file", {"path": "a"}), cancel
        )
        assert result.ok is False
        assert result.side_effect is SideEffect.NONE
        assert session.calls == []

    async def test_the_call_timeout_is_handed_to_the_session(self) -> None:
        session = FakeSession()
        await _call(_bridged(session, timeout_ms=1234))
        assert session.calls[0][2] == 1234

    async def test_a_long_result_is_truncated(self) -> None:
        session = FakeSession(result=RemoteResult(text="x" * 5000))
        result = await _call(_bridged(session, limit=200))
        assert result.truncated is True
        assert len(result.content) <= 200


class TestToolSpec:
    def test_it_is_never_read_only(self) -> None:
        """远端的 `readOnlyHint` 是它自己说的，而它正是那个不可信的一方。"""
        spec = tool_spec("mcp.files.read_file", "files", READ_TOOL)
        assert spec.read_only is False
        assert spec.risk is RiskLevel.MUTATING

    def test_it_runs_exclusively(self) -> None:
        """远端 server 的并发安全性未知，逐个串行是唯一不用赌的选择。"""
        assert tool_spec("mcp.files.x", "files", READ_TOOL).concurrency is Concurrency.EXCLUSIVE

    def test_the_remote_schema_survives(self) -> None:
        spec = tool_spec("mcp.files.read_file", "files", READ_TOOL)
        assert spec.parameters["required"] == ["path"]


class TestRegistration:
    async def test_tools_from_a_server_are_registered_under_the_namespace(self) -> None:
        ctx = McpContext(config={"servers": {"files": {"type": "stdio", "command": "a"}}})
        api = RecordingApi(ctx)
        connector = FakeConnector({"files": FakeSession([READ_TOOL, WRITE_TOOL])})
        try:
            await register(api, ctx, connector)  # pyright: ignore[reportArgumentType]
            assert api.names == ["mcp.files.read_file", "mcp.files.write_file"]
        finally:
            await ctx.shutdown()

    async def test_no_servers_means_no_task_and_no_tools(self) -> None:
        """一个刚装上插件、还没写 server 的实例就长这样。命名空间声明允许零注册。"""
        ctx = McpContext()
        api = RecordingApi(ctx)
        await register(api, ctx, FakeConnector())  # pyright: ignore[reportArgumentType]
        assert api.names == []
        assert ctx.spawned == []

    async def test_one_broken_server_does_not_take_down_the_others(self) -> None:
        ctx = McpContext(
            config={
                "servers": {
                    "bad": {"type": "stdio", "command": "a"},
                    "good": {"type": "stdio", "command": "b"},
                }
            }
        )
        api = RecordingApi(ctx)
        connector = FakeConnector(
            {"good": FakeSession([READ_TOOL])}, failures={"bad": RuntimeError("nope")}
        )
        try:
            discovery = await register(api, ctx, connector)  # pyright: ignore[reportArgumentType]
            assert api.names == ["mcp.good.read_file"]
            assert discovery.failures == {"bad": "RuntimeError"}
        finally:
            await ctx.shutdown()

    async def test_a_failure_reason_is_only_a_type_name(self) -> None:
        ctx = McpContext(config={"servers": {"bad": {"type": "stdio", "command": "a"}}})
        connector = FakeConnector(failures={"bad": RuntimeError("token=sk-abcdefghijklmnop")})
        try:
            discovery = await register(RecordingApi(ctx), ctx, connector)  # pyright: ignore[reportArgumentType]
            assert discovery.failures == {"bad": "RuntimeError"}
        finally:
            await ctx.shutdown()

    async def test_a_disabled_server_is_never_connected(self) -> None:
        ctx = McpContext(
            config={"servers": {"files": {"type": "stdio", "command": "a", "enabled": False}}}
        )
        connector = FakeConnector()
        await register(RecordingApi(ctx), ctx, connector)  # pyright: ignore[reportArgumentType]
        assert connector.connected == []

    async def test_a_slow_server_is_skipped_rather_than_hanging_startup(self) -> None:
        """超时**不取消那条任务**：它可能只是慢，取消会把已经连上的 server 一起关掉。"""
        ctx = McpContext(
            config={
                "connect_timeout_ms": 20,
                "servers": {"files": {"type": "stdio", "command": "a"}},
            }
        )
        api = RecordingApi(ctx)
        connector = FakeConnector({"files": FakeSession()}, delay=5.0)
        try:
            await register(api, ctx, connector)  # pyright: ignore[reportArgumentType]
            assert api.names == []
        finally:
            await ctx.shutdown()

    async def test_a_colliding_pair_is_dropped_and_recorded(self) -> None:
        ctx = McpContext(config={"servers": {"files": {"type": "stdio", "command": "a"}}})
        api = RecordingApi(ctx)
        tools = [RemoteTool(name="get-file", description=""), RemoteTool(name="get_file", description="")]
        connector = FakeConnector({"files": FakeSession(tools)})
        try:
            discovery = await register(api, ctx, connector)  # pyright: ignore[reportArgumentType]
            assert api.names == []
            assert discovery.naming["files"].collisions
        finally:
            await ctx.shutdown()

    async def test_the_credential_reaches_the_headers(self) -> None:
        ctx = McpContext(
            config={
                "servers": {
                    "docs": {
                        "type": "sse",
                        "url": "https://x",
                        "headers": {"Authorization": "Bearer {api_key}"},
                    }
                }
            },
            secrets={"api_key": "sk-token"},
        )
        connector = FakeConnector({"docs": FakeSession()})
        try:
            await register(RecordingApi(ctx), ctx, connector)  # pyright: ignore[reportArgumentType]
            assert connector.connected[0].headers["Authorization"] == "Bearer sk-token"
        finally:
            await ctx.shutdown()

    async def test_no_placeholder_means_no_secret_lookup(self) -> None:
        """一台都不需要鉴权的配置不该因为没导出那个变量而连不上。"""
        ctx = McpContext(
            config={"servers": {"docs": {"type": "sse", "url": "https://x"}}},
            secrets={},
        )
        connector = FakeConnector({"docs": FakeSession()})
        try:
            await register(RecordingApi(ctx), ctx, connector)  # pyright: ignore[reportArgumentType]
            assert connector.connected
        finally:
            await ctx.shutdown()

    async def test_a_broken_config_stops_registration_entirely(self) -> None:
        ctx = McpContext(config={"servers": {"files": {"type": "stdio"}}})
        with pytest.raises(KaryviaError) as caught:
            await register(RecordingApi(ctx), ctx, FakeConnector())  # pyright: ignore[reportArgumentType]

        assert caught.value.code is ErrorCode.CONFIG_INVALID


class TestLifecycle:
    async def test_connections_are_closed_when_the_task_is_cancelled(self) -> None:
        """`AsyncExitStack` 的进入与退出发生在**同一个任务**里，这是 `supervisor.py`
        存在的全部理由（anyio 的任务组必须那样用）。"""
        session = FakeSession()
        ctx = McpContext(config={"servers": {"files": {"type": "stdio", "command": "a"}}})
        await register(RecordingApi(ctx), ctx, FakeConnector({"files": session}))  # pyright: ignore[reportArgumentType]
        assert session.closed is False

        await ctx.shutdown()

        assert session.closed is True

    async def test_the_supervisor_task_survives_until_cancelled(self) -> None:
        ctx = McpContext(config={"servers": {"files": {"type": "stdio", "command": "a"}}})
        try:
            await register(RecordingApi(ctx), ctx, FakeConnector())  # pyright: ignore[reportArgumentType]

            await asyncio.sleep(0)
            assert not ctx.spawned[0].done()
        finally:
            await ctx.shutdown()

    async def test_a_registered_tool_keeps_working_while_connected(self) -> None:
        session = FakeSession(result=RemoteResult(text="live"))
        ctx = McpContext(config={"servers": {"files": {"type": "stdio", "command": "a"}}})
        api = RecordingApi(ctx)
        try:
            await register(api, ctx, FakeConnector({"files": session}))  # pyright: ignore[reportArgumentType]
            _, handler = api.tools[0]
            result = await handler.execute(
                invocation("mcp.files.read_file", {"path": "a"}), ManualCancel()
            )
            assert result.content == "live"
        finally:
            await ctx.shutdown()


class TestManifest:
    def test_it_declares_exactly_one_namespace(self) -> None:
        """ 机制的第一个使用者：远端工具名要连上 server 才知道。"""
        assert len(MANIFEST.capabilities) == 1
        decl = MANIFEST.capabilities[0]
        assert (decl.kind, decl.name, decl.namespace) == (CapabilityKind.TOOL, NAMESPACE, True)

    def test_it_does_not_declare_a_priority(self) -> None:
        assert "priority" not in MANIFEST.capabilities[0].model_fields_set

    def test_the_config_schema_forbids_unknown_keys(self) -> None:
        assert CONFIG_SCHEMA["additionalProperties"] is False

    def test_the_config_schema_matches_what_settings_accepts(self) -> None:
        import jsonschema
        from karyvia_plugin_mcp import resolve_settings

        jsonschema.Draft202012Validator.check_schema(CONFIG_SCHEMA)
        sample: dict[str, JsonValue] = {
            "prefix": "mcp",
            "connect_timeout_ms": 100,
            "call_timeout_ms": 100,
            "max_result_chars": 100,
            "servers": {
                "files": {
                    "type": "stdio",
                    "enabled": True,
                    "command": "npx",
                    "args": ["-y", "x"],
                    "env": {"A": "b"},
                    "cwd": "/tmp",
                },
                "docs": {
                    "type": "streamable_http",
                    "url": "https://x",
                    "headers": {"Authorization": "Bearer {api_key}"},
                },
            },
        }
        jsonschema.validate(sample, CONFIG_SCHEMA)
        assert len(resolve_settings(sample).servers) == 2


class TestSdkBoundary:
    """整棵测试树里**唯一**碰 `mcp` SDK 的一节，而且只验「没装它时说什么」。"""

    def test_a_missing_sdk_says_what_to_install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib.util

        from karyvia_plugin_mcp.client import require_mcp

        monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
        with pytest.raises(KaryviaError) as caught:
            require_mcp()
        assert caught.value.code is ErrorCode.CAPABILITY_MISSING
        assert "pip install" in caught.value.user_message

    def test_only_one_module_imports_the_sdk(self) -> None:
        """本插件的测试树能在没装 `mcp` 的环境里全绿，靠的就是这条边界（CI 用
        `--no-deps` 装插件）。

        判据是 **AST 里的 import 语句**而不是文本包含：docstring 里提到 `mcp` 是正常的，
        而 `from mcp import ...` 不是（除了 `client.py`）。
        """
        import ast
        from pathlib import Path

        package = Path(__file__).resolve().parents[1] / "src" / "karyvia_plugin_mcp"
        offenders: list[str] = []
        for path in sorted(package.glob("*.py")):
            if path.name == "client.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import) and any(
                    alias.name == "mcp" or alias.name.startswith("mcp.") for alias in node.names
                ):
                    offenders.append(path.name)
                if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "mcp":
                    offenders.append(path.name)
        assert offenders == []

    def test_the_guard_would_catch_a_real_import(self, tmp_path: Path) -> None:
        """自证：上一条在任何实现下都通过的话，它证明不了任何事。"""
        import ast

        tree = ast.parse("from mcp.client.stdio import stdio_client\n")
        found = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "mcp"
        ]
        del tmp_path
        assert found


class TestBridgedToolContract(ToolContract):
    """SDK 的通用工具契约。基类**不 import pytest**，只是普通类 + `assert`。"""

    def make_tool(self):  # pyright: ignore[reportMissingParameterType]
        return tool_spec("mcp.files.read_file", "files", READ_TOOL), _bridged(FakeSession())

    def valid_arguments(self) -> dict[str, JsonValue]:
        return {"path": "a.txt"}

    def invalid_arguments(self) -> None:
        """本工具把参数原样转给远端——参数合法性由远端的 schema 与 kernel 的
        `ToolInvoker` 判定，桥接层自己没有可失败的输入。"""
        return None
