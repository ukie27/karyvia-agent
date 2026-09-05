"""官方插件 `mcp`：把 MCP server 的工具接进实例（开发方案 `D38-B`）。

职责：连上配置好的 MCP server，把它们的工具注册成本实例的 `TOOL` 能力，
并在调用时转发过去。
不负责：MCP 的 resources / prompts / sampling（见下面「不做的事」）、
执行 turn、决定什么时候调这些工具。

**它取代的是 `references/nanobot/nanobot/agent/tools/mcp.py`**（1573 行），但不是移植：

- 旧实现同时桥接 **tools / resources / prompts** 三种远端对象（三个 wrapper 类）。
  这里只做 tools：`resources` 与 `prompts` 在新层没有对应的能力种类，把它们也伪装成工具
  会让模型拿到一堆语义不明的调用（`AGENTS.md` 原则 3「机制优先于功能」）。
- 旧实现有一条 `RUNTIME_CONTROL_MCP_RELOAD` 的热重载通道。这里没有：registry 解析后
  只读（`NFR-403`）且首版不热更新（技术方案 §10.4），做一条只在自己这一层成立的
  「重载」等于让 `nm capabilities` 印的东西与实际生效的不一致。改配置后重启实例。
- 旧实现的工具名去重靠 `hashlib` 生成后缀。这里**撞车的各方都不生效**并记进日志，
  与 `kernel/registry/resolution.py` 对同名冲突的判定一致：选任何一边都是替用户做决定。

**这个插件是 `D38-A` 命名空间声明机制的第一个使用者**：manifest 只声明一个前缀
（`CapabilityDecl(kind=TOOL, name="mcp", namespace=True)`），远端工具名要连上 server、
`list_tools()` 之后才可知，而 manifest 是静态的。

**四条如实记着的边界**，写在这里而不是留给用户发现：

- **`side_effect` 恒为 `UNKNOWN`。** MCP 协议不报告副作用，一个写文件的远端工具与一个
  只读的在线格式上长得一模一样。因此本插件的每一次调用都对编排层说「不知道外部世界变了
  没有」，`read_only` 恒为 `False`、`risk` 恒为 `MUTATING`。远端的 `readOnlyHint` 是**它
  自己说的**，而它正是那个不可信的一方。
- **插件自己拥有 MCP 连接。** stdio 传输需要长驻子进程与管道，HTTP 传输由 `mcp` SDK
  自己建立；一次性 `ctx.shell` 和带 SSRF 守卫的 `ctx.net` 都不适合这两种连接。
  真正的边界是「你配置了哪些 server」。
- **启动路径上多一次往返。** 连接发生在 `setup()` 里（registry 冻结后只读，没有第二个
  注册时机），因此每台 server 都会给冷启动加上它自己的连接时间。`connect_timeout_ms`
  是上界，超时即跳过那台 server。
- **停止预算是每插件 5000 ms**（`plugins.stop_timeout_ms`）。一台赖着不退的 stdio server
  会让这个插件的停止超时，`StopOutcome.timed_out` 如实标着——那时进程可能还在跑。

**只 import `nucleamind.contracts` 与 `nucleamind.sdk`**（依赖规则 `R4`）；`mcp` 只在
`client.py` 里惰性 import。**`MANIFEST` 在模块顶层且导入无副作用**（技术方案 §7.2）。
"""

from __future__ import annotations

from typing import Final

from nucleamind.contracts import CapabilityKind
from nucleamind.sdk import (
    CapabilityDecl,
    ManifestJsonSchema,
    NucleaAPI,
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

#: `plugins.mcp.config` 的形状。阶段 A 用它校验，`settings.py` 再做它表达不了的那些
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
    setup="nucleamind_plugin_mcp:setup",
    capabilities=(
        # **一条命名空间声明**（`D38-A`）：远端工具名要连上 server 才知道，而 manifest
        # 是静态的。零注册是合法的——server 全连不上时本插件一条工具都不注册。
        CapabilityDecl(kind=CapabilityKind.TOOL, name=NAMESPACE, namespace=True),
    ),
    config_schema=CONFIG_SCHEMA,
)


async def register(
    api: NucleaAPI, ctx: PluginContext, connector: Connector | None = None
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

    **静默丢掉一条工具会让用户在 `nm capabilities` 里怎么找都找不到它**，而这两类问题
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


async def setup(api: NucleaAPI) -> None:
    """注册入口。manifest 的 `setup` 字段指向它。**它是 `async` 的**——发现远端工具
    需要一次真实往返，而 registry 在解析之后只读，没有第二个注册时机。

    **配置在这里一次校验完**（`resolve_settings` 会抛 `CONFIG_INVALID`）；
    连接失败**不抛**：单台 server 连不上只让它的工具缺席，其余照常。
    """
    await register(api, api.ctx)
