"""官方 Memory 插件：提供跨 Session 的结构化长期记忆。

职责：注册记忆存储、自动召回 Context、模型工具和用户命令。
不负责：决定应记住什么、组装最终模型消息、定时模型整理或 Git 版本存储。

记忆按 `FragmentScope` 在存储层分区。Kernel 可通过 `memory.provider` 启用 agent 范围召回；
插件自身的 Context Provider 也可召回配置范围，两条路径不应同时包含 agent 范围，以免重复。
`MemoryProvider` 契约不携带 `SessionKey`，所以该接口只处理 agent 范围；会话与工作区范围由
插件的 Context 路径处理。`USER` 范围不受支持，因为召回阶段没有可靠的发送者身份。
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from karyvia.contracts import CapabilityKind
from karyvia.sdk import (
    CapabilityDecl,
    KaryviaAPI,
    PluginContext,
    PluginManifest,
)

from .commands import COMMAND_NAME, MemoryCommand, memory_spec
from .provider import PROVIDER_NAME, MemoryContextProvider, query_from
from .record import MAX_CONTENT_CHARS, SOURCE, MemoryRecord, estimate_tokens
from .settings import CONFIG_SCHEMA, MEMORY_DIR_NAME, MemorySettings, resolve_settings
from .store import ContractMemoryProvider, Hit, MemoryStore
from .tools import (
    FORGET_TOOL,
    RECALL_TOOL,
    REMEMBER_TOOL,
    TOOL_NAMES,
    MemoryForgetTool,
    MemoryRecallTool,
    MemoryRememberTool,
    forget_spec,
    recall_spec,
    remember_spec,
)

__all__ = [
    "COMMAND_NAME",
    "CONFIG_SCHEMA",
    "FORGET_TOOL",
    "MANIFEST",
    "MAX_CONTENT_CHARS",
    "MEMORY_DIR_NAME",
    "PROVIDER_NAME",
    "RECALL_TOOL",
    "REMEMBER_TOOL",
    "SOURCE",
    "STORE_NAME",
    "TOOL_NAMES",
    "ContractMemoryProvider",
    "Hit",
    "MemoryCommand",
    "MemoryContextProvider",
    "MemoryForgetTool",
    "MemoryRecallTool",
    "MemoryRecord",
    "MemoryRememberTool",
    "MemorySettings",
    "MemoryStore",
    "estimate_tokens",
    "forget_spec",
    "memory_directory",
    "memory_spec",
    "query_from",
    "recall_spec",
    "register",
    "remember_spec",
    "resolve_settings",
    "setup",
]

#: `MEMORY` 能力的名字。它描述的是**后端形态**而不是「记忆」这件事——第三方写一条
#: `MEMORY:sqlite` 与它并存是正常的（`MEMORY` 的 arity 是 MULTI_UNIQUE）。
STORE_NAME: Final = "jsonl"

MANIFEST: Final = PluginManifest(
    id="memory",
    version="0.1.0",
    sdk_range=">=5.0.0,<6.0.0",
    setup="karyvia_plugin_memory:setup",
    capabilities=(
        CapabilityDecl(kind=CapabilityKind.MEMORY, name=STORE_NAME),
        CapabilityDecl(kind=CapabilityKind.CONTEXT, name=PROVIDER_NAME),
        *(CapabilityDecl(kind=CapabilityKind.TOOL, name=name) for name in TOOL_NAMES),
        CapabilityDecl(kind=CapabilityKind.COMMAND, name=COMMAND_NAME),
    ),
    config_schema=CONFIG_SCHEMA,
)


def memory_directory(ctx: PluginContext, settings: MemorySettings) -> Path:
    """落点：配置的 `dir`，没配就是 `<state_dir>/memory`。

    **相对路径按状态目录解析**而不是按进程 cwd：`karyvia` 从哪个目录启动不该改变记忆存到哪里。
    绝对路径原样采纳，因为运维显式指定的位置不应再按状态目录重写。
    """
    if not settings.directory:
        return ctx.state_dir / MEMORY_DIR_NAME
    configured = Path(settings.directory)
    return configured if configured.is_absolute() else ctx.state_dir / configured


def register(api: KaryviaAPI, ctx: PluginContext) -> MemorySettings:
    """真正的注册体。

    与 `setup()` 分开是为了让用例能在不构造整个装配根的情况下驱动它，同时保证生产路径与
    测试路径**注册的是同一批对象**；测试注入点不另建注册路径。

    **四类能力共用同一个 `MemoryStore`**：契约门面、Context Provider、三条工具与 `/memory`
    看到的是同一份数据。给它们各建一个 store 会让「工具刚写的记忆，命令查不到」这种问题
    只在并发下偶发。
    """
    settings = resolve_settings(ctx.config)
    store = MemoryStore(memory_directory(ctx, settings))

    api.register_memory_provider(STORE_NAME, ContractMemoryProvider(store))
    api.register_context_provider(PROVIDER_NAME, MemoryContextProvider(store, settings))
    api.register_tool(remember_spec(), MemoryRememberTool(store, settings))
    api.register_tool(recall_spec(), MemoryRecallTool(store, settings))
    api.register_tool(forget_spec(), MemoryForgetTool(store, settings))
    api.register_command(memory_spec(), MemoryCommand(store, settings))
    return settings


def setup(api: KaryviaAPI) -> None:
    """注册入口。manifest 的 `setup` 字段指向它。

    **配置在这里一次校验完**（`resolve_settings` 会抛 `CONFIG_INVALID`）；
    **目录不在这里创建**——为一个可能永远不写入的插件建目录，是在没人要求的时候动用户的
    磁盘。**六条能力一次注册齐**：外部插件用不上装配根的 `keep` 声明过滤
    （`_ENABLED_NAMES` 按内建 id 索引），声明与注册在这里必须严格相等，
    否则 `CapabilityHost.finish()` 会以 `PLUGIN_LOAD_FAILED` 挡下——那个报错是对的。
    """
    register(api, api.ctx)
