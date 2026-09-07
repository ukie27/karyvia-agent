"""官方 MCP 插件：把远端 MCP tools 注册为实例工具。

职责：连接配置的 MCP server、发现工具、分配稳定的命名空间名称，并转发调用。
不负责：MCP resources、prompts、sampling、热重载或 Turn 编排。

manifest 声明 `mcp` 工具命名空间，因为具体工具名只有连接 server 后才能获知。远端工具
发生命名冲突时，各方都不生效并记录诊断。MCP 不提供可靠的副作用信息，因此工具统一声明
为 `MUTATING`，结果的 `side_effect` 为 `UNKNOWN`。插件拥有长连接及 stdio 子进程，连接和
停止均受配置预算约束。
"""

from __future__ import annotations

from typing import Final

from karyvia.contracts import CapabilityKind
from karyvia.sdk import (
    CapabilityDecl,
    KaryviaAPI,
    ManifestJsonSchema,
    PluginContext,
    PluginManifest,
)

from .naming import DEFAULT_PREFIX, NameAssignment, assign_names, normalise_segment, tool_name
from .session import Connector, McpSession, RemoteResult, RemoteTool, ServerHandle
from .settings import (
    CREDENTIAL_PLACEHOLDER,
    DEFAULT_CALL_TIMEOUT_MS,
    DEFAULT_CONNECT_TIMEOUT_MS,
    SECRET_NAME,
    TRANSPORTS,
    McpSettings,
    ServerSettings,
    needs_credential,
    resolve_settings,
    with_credential,
)
from .supervisor import ConnectionSupervisor, DiscoveredTool, Discovery
from .tool import BridgedTool, SessionSource, tool_spec
from .translate import describe_tool, render_result, summarise_parts, tool_parameters, truncate

__all__ = [
    "CONFIG_SCHEMA",
    "CREDENTIAL_PLACEHOLDER",
    "DEFAULT_CALL_TIMEOUT_MS",
    "DEFAULT_CONNECT_TIMEOUT_MS",
    "DEFAULT_PREFIX",
    "MANIFEST",
    "NAMESPACE",
    "SECRET_NAME",
    "TRANSPORTS",
    "BridgedTool",
    "ConnectionSupervisor",
    "Connector",
    "DiscoveredTool",
    "Discovery",
    "McpSession",
    "McpSettings",
    "NameAssignment",
    "RemoteResult",
    "RemoteTool",
    "ServerHandle",
    "ServerSettings",
    "SessionSource",
    "assign_names",
    "needs_credential",
    "with_credential",
    "describe_tool",
    "normalise_segment",
    "register",
    "render_result",
    "resolve_settings",
    "setup",
    "summarise_parts",
    "tool_name",
    "tool_parameters",
    "tool_spec",
    "truncate",
]

#: 命名空间前缀。它同时是 manifest 声明的那条前缀与本地工具名的第一段——写两遍字面量
#: 就会在改名时对不上，而对不上的后果是每一条注册都被判成「未声明」。
NAMESPACE: Final = DEFAULT_PREFIX

_SERVER_SCHEMA: Final[ManifestJsonSchema] = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": list(TRANSPORTS)},
        "enabled": {"type": "boolean"},
        "command": {"type": "string", "description": "stdio：要启动的可执行程序。"},
        "args": {"type": "array", "items": {"type": "string"}},
        "env": {"type": "object", "additionalProperties": {"type": "string"}},
        "cwd": {"type": "string"},
        "url": {"type": "string", "description": "sse / streamable_http：端点地址。"},
        "headers": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "值里的 {api_key} 会被替换成配置的凭据。",
        },
    },
    "additionalProperties": False,
}

#: `plugins.mcp.config` 的形状。加载前校验 用它校验，`settings.py` 再做它表达不了的那些
#: （按传输分支的必填项、server 名字能不能归一）。
#: 标注成 `ManifestJsonSchema` 而不是 `contracts.JsonSchema`：契约那个类型进不了
#: pydantic 模型（会 `RecursionError`），细节见 `sdk/manifest.py::ManifestJsonValue`。
CONFIG_SCHEMA: Final[ManifestJsonSchema] = {
    "type": "object",
    "properties": {
        "prefix": {
            "type": "string",
            "description": "本地工具名的第一段。改它要同时改 manifest 的命名空间声明。",
        },
        "connect_timeout_ms": {"type": "integer", "minimum": 1},
        "call_timeout_ms": {"type": "integer", "minimum": 1},
        "max_result_chars": {"type": "integer", "minimum": 1},
        "servers": {
            "type": "object",
            "description": "server 名 → 连接参数。名字会成为工具名的第二段。",
            "additionalProperties": _SERVER_SCHEMA,
        },
    },
    "additionalProperties": False,
}

MANIFEST: Final = PluginManifest(
    id="mcp",
    version="0.1.0",
    sdk_range=">=5.0.0,<6.0.0",
    setup="karyvia_plugin_mcp:setup",
    capabilities=(
        # **一条命名空间声明**：远端工具名要连上 server 才知道，而 manifest
        # 是静态的。零注册是合法的——server 全连不上时本插件一条工具都不注册。
        CapabilityDecl(kind=CapabilityKind.TOOL, name=NAMESPACE, namespace=True),
    ),
    config_schema=CONFIG_SCHEMA,
)


async def register(
    api: KaryviaAPI, ctx: PluginContext, connector: Connector | None = None
) -> Discovery:
    """真正的注册体。`connector` 只有测试会传（一个不碰 `mcp` SDK 的替身）。

    与 `setup()` 分开是为了让用例能在不构造整个装配根的情况下驱动它，同时保证
    生产路径与测试路径**注册的是同一批对象**。
    """
    settings = resolve_settings(ctx.config)
    if not settings.enabled_servers:
        # 一台 server 都没配：不连、不派生任务、不 import `mcp`。命名空间声明允许零注册，
        # 因此这条路径是完全合法的（一个刚装上插件、还没写 server 的实例就长这样）。
        return Discovery()
    if needs_credential(settings):
        # **只在真的用得到时才取**：一台都不需要鉴权的配置不该因为没导出那个变量而失败。
        settings = with_credential(settings, ctx.secret(SECRET_NAME).reveal())

    supervisor = ConnectionSupervisor(settings, connector or _default_connector())
    # **派生而不是 await**：连接必须由一条独立任务拥有，`AsyncExitStack` 的进入与退出
    # 才会发生在同一个任务里（`supervisor.py` 的模块 docstring 有完整解释）。
    ctx.spawn_task(supervisor.run(), name="connections")
    if not await supervisor.wait_ready(settings.connect_timeout_ms * 2):
        ctx.logger.warning("MCP：连接在预算内没有就绪，本轮不注册任何远端工具。")
        return Discovery()

    discovery = supervisor.discovery
    _report(ctx, discovery)
    sources = {
        name: SessionSource(session) for name, session in discovery.sessions.items()
    }
    for found in discovery.tools:
        api.register_tool(
            tool_spec(found.local_name, found.server, found.remote),
            BridgedTool(
                sources[found.server],
                found.server,
                found.remote.name,
                timeout_ms=settings.call_timeout_ms,
                limit=settings.max_result_chars,
            ),
        )
    return discovery


def _report(ctx: PluginContext, discovery: Discovery) -> None:
    """把连接失败与命名撞车写进日志。

    **静默丢掉一条工具会让用户在 `karyvia capabilities` 里怎么找都找不到它**，而这两类问题
    都不该让实例起不来。日志是它们唯一的出口。
    """
    for server, reason in sorted(discovery.failures.items()):
        ctx.logger.warning("MCP server %s 连接失败（%s），它的工具本轮不可用。", server, reason)
    for server, assignment in sorted(discovery.naming.items()):
        for local, originals in sorted(assignment.collisions.items()):
            ctx.logger.warning(
                "MCP server %s 的工具 %s 归一后都叫 %s，因此都没有注册。",
                server,
                "、".join(originals),
                local,
            )
        if assignment.rejected:
            ctx.logger.warning(
                "MCP server %s 的工具 %s 的名字无法归一成合法能力名，已跳过。",
                server,
                "、".join(assignment.rejected),
            )


def _default_connector() -> Connector:
    """生产用的连接器。**在这里才 import `client`**，它是唯一碰 `mcp` SDK 的模块。"""
    from .client import SdkConnector  # noqa: PLC0415 - 惰性，见模块 docstring

    return SdkConnector()


async def setup(api: KaryviaAPI) -> None:
    """注册入口。manifest 的 `setup` 字段指向它。**它是 `async` 的**——发现远端工具
    需要一次真实往返，而 registry 在解析之后只读，没有第二个注册时机。

    **配置在这里一次校验完**（`resolve_settings` 会抛 `CONFIG_INVALID`）；
    连接失败**不抛**：单台 server 连不上只让它的工具缺席，其余照常。
    """
    await register(api, api.ctx)
