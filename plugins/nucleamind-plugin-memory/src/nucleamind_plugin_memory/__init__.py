"""官方插件 `memory`：跨 Session 的长期记忆（开发方案 `D39`，需求 §9.8 `MEM-001`–`MEM-005`）。

职责：一份 manifest 声明四类能力——存储本体（`MEMORY`）、每轮自动召回
（`CONTEXT`）、模型显式记/查/删（三条 `TOOL`）、给人用的入口（`COMMAND`）。
不负责：决定什么时候召回（`kernel/turn/`）、把片段拼进模型消息
（`kernel/turn/context_builder.py`）、决定记什么（模型与用户）。

**它取代的是 `references/nanobot/nanobot/agent/memory.py`**，但不是移植：

- 旧实现是 `MemoryStore` + `Consolidator` + `Dream` + `GitStore` 四层，1221 行缠在一个
  文件里，且直接读写 `SOUL.md` / `USER.md` / `memory/MEMORY.md` 三份固定的 Markdown。
  **这里只做「一条一条的记忆」**：固定文件名那套是 Agent 人格与用户画像，属于
  `builtins/context_basic` 的运维指令那一档，不是长期记忆机制。
- **Dream（定时让 LLM 读历史、增量改写长期记忆）本轮不做。** 它需要两样今天没有的东西：
  「插件能发起一次模型调用」——`PluginContext` 没有这条通道；以及定时触发——那是 `D40`。
  写在这里而不是留给用户发现。
- **GitStore（记忆变更的版本历史）也不做。** 它要求把记忆存成 Git 仓库里的文本文件，
  而那会把「记忆的存储形态」钉死成一种具体后端，正是 `MEM-001` 要避免的。
- 旧实现没有范围概念（全部记忆是一份 workspace 级的文件）。这里按 `FragmentScope` 分区，
  `MEM-002` 因此在存储层就成立而不是靠约定。

**三条如实记着的边界**，写在这里而不是留给用户发现：

- **`D44` 起 kernel 会消费 `CapabilityKind.MEMORY`，但默认不开。** 装配根按
  `memory.provider` 挑一条 `MEMORY` 能力交给组装器（`kernel/turn/memory.py`）；不写那个键
  就没有 kernel 侧召回，而那是默认。因此**默认配置下**记忆进到上下文仍然只靠本插件自己的
  `CONTEXT` Provider。
  **两边同时开会让同一条记忆在一轮里出现两次**：kernel 侧只召回 `agent` 范围，而本插件的
  `enabled_scopes` 默认已经包含 `agent`。要用 kernel 侧召回（例如为了让 `nm config` 里
  那几个旋钮生效）就把 `enabled_scopes` 去掉 `agent`，或者干脆不写 `memory.provider`。
  两条路径**都是对的**，只是不该同时开——这条如实写在这里与 README 里，不留给用户发现。
- **契约的 `MemoryProvider` 三个方法都不带 `SessionKey`**，因此经那条接口只能读写 `agent`
  范围（`store.ContractMemoryProvider`）。`D44` 把这一点从「目前这么理解」升成了契约上的
  **决定**（见 `contracts/protocols.py::MemoryProvider`）：会话级与工作区级归
  `ContextProvider`。插件自己的四条通路都拿得到 key，不受此限。
- **`FragmentScope.USER` 不支持**：召回路径拿不到发送者身份，按会话存会让群聊里其他人
  读到它。详见 `partition.py`。

**只 import `nucleamind.contracts` 与 `nucleamind.sdk`**（依赖规则 `R4`），
**一个第三方依赖都不引入**。**`MANIFEST` 在模块顶层且导入无副作用**（技术方案 §7.2）：
发现阶段只 import 本模块取那个对象，此时不该发生任何 IO——目录也在第一次写入时才建。
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from nucleamind.contracts import CapabilityKind
from nucleamind.sdk import (
    CapabilityDecl,
    NucleaAPI,
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
    sdk_range=">=4.0.0,<5.0.0",
    setup="nucleamind_plugin_memory:setup",
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

    **相对路径按状态目录解析**而不是按进程 cwd：`nm` 从哪个目录启动不该改变记忆存到哪里。
    绝对路径原样采纳，因为运维显式指定的位置不应再按状态目录重写。
    """
    if not settings.directory:
        return ctx.state_dir / MEMORY_DIR_NAME
    configured = Path(settings.directory)
    return configured if configured.is_absolute() else ctx.state_dir / configured


def register(api: NucleaAPI, ctx: PluginContext) -> MemorySettings:
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


def setup(api: NucleaAPI) -> None:
    """注册入口。manifest 的 `setup` 字段指向它。

    **配置在这里一次校验完**（`resolve_settings` 会抛 `CONFIG_INVALID`）；
    **目录不在这里创建**——为一个可能永远不写入的插件建目录，是在没人要求的时候动用户的
    磁盘。**六条能力一次注册齐**：外部插件用不上装配根的 `keep` 声明过滤
    （`_ENABLED_NAMES` 按内建 id 索引），声明与注册在这里必须严格相等，
    否则 `CapabilityHost.finish()` 会以 `PLUGIN_LOAD_FAILED` 挡下——那个报错是对的。
    """
    register(api, api.ctx)
