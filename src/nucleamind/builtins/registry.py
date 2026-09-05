"""内建能力的静态清单（技术方案 §7.1、§8）。

职责：以 `BUILTIN_MANIFESTS` 声明全部内建能力的 manifest——这是内建能力**唯一**的发现来源。
不负责：导入任何 `setup` 实现、决定加载顺序或注册能力本身。

**manifest 写在这里而不是各内建子包里**，因为读 manifest 不该导入实现（§7.1「发现与启用
分离」）：`import nucleamind.builtins.registry` 只需要 pydantic 与几个字面量，
`session_jsonl` 的 `setup` 只在真正加载它时才被 `import_setup()` 拉进来。

**内建不享受特权**（`BAS-005`）：这里的每一条都是普通的 `PluginManifest`，与外部插件同型，
同样要声明 `capabilities`，同样经 `runtime/wiring.py` 翻译成 `LoadRequest`
后走 `kernel/plugins/host.py` 那**一个** Host 注册。本目录受 `R4` 约束，只能 import
`sdk/` 与 `contracts/`，因此这里连 registry 长什么样都看不见。

**`priority` 一律不写**：`CapabilityDecl.priority` 的默认值是 100，而内建的基准是 0
（技术方案 §6.1 规则 1）。写了就会被原样采纳，「内建排在插件前」与「内建最后被裁」会同时
静默失效——`to_declaration()` 靠 pydantic 的 `model_fields_set` 判断作者到底写没写。
"""

from __future__ import annotations

from typing import Final

from nucleamind.contracts import CapabilityKind
from nucleamind.sdk import CapabilityDecl, PluginManifest

__all__ = [
    "BUILTIN_MANIFESTS",
    "CLI_ENTRY",
    "COMMANDS_CORE",
    "CONTEXT_BASIC",
    "CONTEXT_COMPACT_BASIC",
    "MODEL_OPENAI",
    "SESSION_JSONL",
    "TOOLS_FS",
    "TOOLS_FILE",
    "TOOLS_SHELL",
]

#: `D17` 内建 Session（技术方案 §8.1）。没有会话存储时，Runtime 的基础能力校验会拒绝
#: 启动；这不是 manifest 提供方可以选择的策略。
#:
#: 会话存储直接使用 pathlib，因为 `FileAccess` 没有追加、`fsync` 与原子替换；用门面实现
#: 追加写会迫使它每次重写整个会话文件。
SESSION_JSONL: Final = PluginManifest(
    id="session-jsonl",
    version="0.1.0",
    sdk_range=">=5.0.0,<6.0.0",
    setup="nucleamind.builtins.session_jsonl:setup",
    capabilities=(CapabilityDecl(kind=CapabilityKind.SESSION_STORE, name="jsonl"),),
    config_schema={
        "type": "object",
        "properties": {
            "dir": {
                "type": "string",
                "description": "会话文件所在目录。缺省时使用本插件的私有状态目录；"
                "装配根会把实例布局的 sessions/ 目录填在这里。",
            }
        },
        "additionalProperties": False,
    },
)

#: `D18` 内建 Context Provider（技术方案 §8.1）。
#:
#: 它是默认上下文来源，但 Context Provider 故障统一被隔离；插件不能借 manifest 获得
#: 中断 turn 或启动的权力。
#:
#: 本内建纯内存、不读盘、不出网（技术方案 §14 的「Provider 只读不写」）。
CONTEXT_BASIC: Final = PluginManifest(
    id="context-basic",
    version="0.1.0",
    sdk_range=">=5.0.0,<6.0.0",
    setup="nucleamind.builtins.context_basic:setup",
    capabilities=(CapabilityDecl(kind=CapabilityKind.CONTEXT, name="basic"),),
    config_schema={
        "type": "object",
        "properties": {
            "instructions": {
                "description": "运维自定义的指令文本。它以 TrustLevel.OPERATOR 进入上下文，"
                "不占据系统指令位置（`CMD-005`）。",
                "anyOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ],
            },
            "use_baseline_instructions": {
                "type": "boolean",
                "default": True,
                "description": "是否附上内建基线系统指令。关掉它就必须提供 instructions。",
            },
            "include_runtime_facts": {
                "type": "boolean",
                "default": True,
                "description": "是否附上当前时间、会话身份与历史条数这几项运行时事实。",
            },
        },
        "additionalProperties": False,
    },
)

#: 默认 Turn 内上下文压缩策略。它是必需能力的默认提供方，但不享有 Kernel 私有兜底路径。
CONTEXT_COMPACT_BASIC: Final = PluginManifest(
    id="context-compact-basic",
    version="0.1.0",
    sdk_range=">=5.0.0,<6.0.0",
    setup="nucleamind.builtins.context_compact_basic:setup",
    capabilities=(
        CapabilityDecl(kind=CapabilityKind.TURN_COMPACTOR, name="basic"),
    ),
)

#: `D19` 内建 Model Provider（技术方案 §8.1、§15 第 5 项）。
#:
#: 没有模型就没有 Agent；本项是宿主发布的内建基线，加载错误由 Runtime 原样拒绝启动。
#:
#: 直接用 httpx 而不是 `ctx.net`：模型端点由运维配置，必须支持本地 vLLM / Ollama /
#: LM Studio，而安全客户端会拒绝私有网段。凭据只从 `ctx.secret("api_key")` 来，配置块里
#:   `CFG-003`「明文不进配置文档」因此是结构性成立的而不是靠流程遵守。
MODEL_OPENAI: Final = PluginManifest(
    id="model-openai",
    version="0.1.0",
    sdk_range=">=5.0.0,<6.0.0",
    setup="nucleamind.builtins.model_openai:setup",
    capabilities=(CapabilityDecl(kind=CapabilityKind.MODEL, name="openai"),),
    config_schema={
        "type": "object",
        "properties": {
            "base_url": {
                "type": "string",
                "default": "https://api.openai.com/v1",
                "description": "端点根，原样使用不追加 /v1——各家后缀差异极大"
                "（OpenVINO 是 /v3，vLLM 没有约定）。",
            },
            "auth": {
                "type": "string",
                "enum": ["bearer", "api_key_header", "none"],
                "default": "bearer",
                "description": "认证形态：Authorization: Bearer / Azure 的 api-key 头 / "
                "无凭据（本地模型服务）。",
            },
            "models": {
                "type": "object",
                "description": "按模型标识列举窗口、上限与线格式偏好。"
                "非空即为白名单：不在其中的模型 describe() 会报能力缺失。",
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "context_window_tokens": {"type": "integer", "minimum": 1},
                        "max_output_tokens": {"type": "integer", "minimum": 1},
                        "capabilities": {"type": "array", "items": {"type": "string"}},
                        "max_tokens_field": {
                            "type": "string",
                            "enum": ["max_tokens", "max_completion_tokens"],
                        },
                        "supports_temperature": {"type": "boolean"},
                    },
                    "additionalProperties": False,
                },
            },
            "default_context_window_tokens": {
                "type": "integer",
                "minimum": 1,
                "default": 128000,
                "description": "models 未覆盖时的上下文窗口，Context 预算由它推导。",
            },
            "default_max_output_tokens": {
                "type": "integer",
                "minimum": 1,
                "default": 4096,
                "description": "models 未覆盖时的单次输出上限。",
            },
            "capabilities": {
                "type": "array",
                "items": {"type": "string"},
                "default": ["tool_calls", "streaming"],
                "description": "显式列出支持项（MOD-005）。不在表里的能力一律报缺失，"
                "不静默降级。",
            },
            "max_tokens_field": {
                "type": "string",
                "enum": ["max_tokens", "max_completion_tokens"],
                "default": "max_tokens",
                "description": "输出上限的字段名。gpt-5、o1/o3/o4 只认后者。",
            },
            "supports_temperature": {
                "type": "boolean",
                "default": True,
                "description": "关掉后 temperature 被省略而不是钳值——推理模型对它直接 400。",
            },
            "request_timeout_ms": {
                "type": "integer",
                "minimum": 1,
                "default": 120000,
                "description": "ModelRequest.timeout_ms 为 0（不限）时的兜底。",
            },
            "stream_idle_timeout_ms": {
                "type": "integer",
                "minimum": 1,
                "default": 60000,
                "description": "流打开后卡住的看门狗。请求级超时保护不了一个开了口"
                "就不再吐字的流。",
            },
            "include_usage": {
                "type": "boolean",
                "default": False,
                "description": "流式请求是否携带 stream_options.include_usage。"
                "默认关：不少兼容端点会对未知字段 400。",
            },
        },
        "additionalProperties": False,
    },
)

#: `D20` 内建文件工具（技术方案 §8.2 的冻结清单，`shell.exec` 是 `D21`）。
#:
#: 没有文件工具的 Agent 仍然能对话。一份写错的 workspace 路径只让这一项加载失败并
#: 留下诊断。
#:
#: **五条声明必须与 `tools_fs.TOOL_NAMES` 逐一对应**，由测试对照。被 `disable` 关掉的
#: 工具**不会**被注册，因此装配根必须用同一份配置过滤这里的声明
#: （`runtime/wiring.py` 的 `keep` 参数 + `tools_fs.enabled_tool_names()`）——否则
#: `D16` 的 `CapabilityHost.finish()` 会以 `PLUGIN_LOAD_FAILED` 拒绝加载，而那个报错是对的。
#:
#: 文件工具直接实现自己的 workspace 守卫，因为它还需要目录遍历、原子替换和按字节数
#: 截断读取；这些是模型工具的行为，不应依赖插件便利门面的较窄接口。
TOOLS_FS: Final = PluginManifest(
    id="tools-fs",
    version="0.1.0",
    sdk_range=">=5.0.0,<6.0.0",
    setup="nucleamind.builtins.tools_fs:setup",
    capabilities=(
        CapabilityDecl(kind=CapabilityKind.TOOL, name="fs.read"),
        CapabilityDecl(kind=CapabilityKind.TOOL, name="fs.write"),
        CapabilityDecl(kind=CapabilityKind.TOOL, name="fs.edit"),
        CapabilityDecl(kind=CapabilityKind.TOOL, name="fs.list"),
        CapabilityDecl(kind=CapabilityKind.TOOL, name="fs.grep"),
    ),
    config_schema={
        "type": "object",
        "properties": {
            "workspace": {
                "type": "string",
                "description": "全部路径解析的根。缺省时使用本插件的私有状态目录；"
                "装配根会把配置里的 workspace.root 填在这里。",
            },
            "disable": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要关掉的工具名（TOL-006）。被关掉的工具不会注册，"
                "因此也不出现在模型可见的工具列表里。表外的名字会被拒绝。",
            },
            "max_read_bytes": {
                "type": "integer",
                "minimum": 1,
                "default": 1048576,
                "description": "单次从磁盘读取的字节上限。超出是截断而不是失败。",
            },
            "max_result_chars": {
                "type": "integer",
                "minimum": 1,
                "default": 32768,
                "description": "单个工具结果的字符上限，不得超过契约的 65536。",
            },
            "max_entries": {
                "type": "integer",
                "minimum": 1,
                "default": 500,
                "description": "fs.list 单次返回的条目数上限。",
            },
            "max_matches": {
                "type": "integer",
                "minimum": 1,
                "default": 200,
                "description": "fs.grep 单次返回的匹配数上限。",
            },
        },
        "additionalProperties": False,
    },
)

#: 默认文件投递工具。它只表达“把这个 workspace 文件附加到当前回复”，实际上传仍走
#: `ToolResult.attachments -> OutboundMessage -> Channel.deliver()` 的统一链路。
TOOLS_FILE: Final = PluginManifest(
    id="tools-file",
    version="0.1.0",
    sdk_range=">=5.0.0,<6.0.0",
    setup="nucleamind.builtins.tools_file:setup",
    capabilities=(CapabilityDecl(kind=CapabilityKind.TOOL, name="file.send"),),
    config_schema={
        "type": "object",
        "properties": {
            "max_file_bytes": {
                "type": "integer",
                "minimum": 1,
                "default": 26214400,
                "description": "file.send 接受的单文件字节上限。",
            }
        },
        "additionalProperties": False,
    },
)

#: `D21` 内建 shell 工具（技术方案 §8.2 冻结清单的第 6 项，至此六件套齐）。
#:
#: 没有 shell 工具的 Agent 仍然能对话，与 `tools_fs` 同一条理由。
#:
#: 子进程环境默认一个变量都不继承（`environ.py` 是白名单，不是黑名单）；这是真正影响
#: 执行边界的机制，与插件加载状态无关。
TOOLS_SHELL: Final = PluginManifest(
    id="tools-shell",
    version="0.1.0",
    sdk_range=">=5.0.0,<6.0.0",
    setup="nucleamind.builtins.tools_shell:setup",
    capabilities=(CapabilityDecl(kind=CapabilityKind.TOOL, name="shell.exec"),),
    config_schema={
        "type": "object",
        "properties": {
            "workspace": {
                "type": "string",
                "description": "命令的默认 cwd，也是 cwd 参数必须落在其内的根。"
                "缺省时使用本插件的私有状态目录；装配根会把配置里的 workspace.root 填在这里。",
            },
            "disable": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要关掉的工具名（TOL-006）。被关掉的工具不会注册，"
                "因此也不出现在模型可见的工具列表里。表外的名字会被拒绝。",
            },
            "timeout_ms": {
                "type": "integer",
                "minimum": 1,
                "default": 120000,
                "description": "单次命令的执行超时。实际生效值是它与 "
                "ToolInvocation.timeout_ms 的较小者。",
            },
            "max_output_chars": {
                "type": "integer",
                "minimum": 1,
                "default": 32768,
                "description": "单个工具结果的字符上限，不得超过契约的 65536。",
            },
            "pass_env": {
                "type": "array",
                "items": {"type": "string"},
                "description": "额外从父进程转发给子进程的环境变量名。"
                "默认一个都不转发（NFR-307）；父进程里没有的名字会被静默跳过。",
            },
            "env": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "显式写死的环境变量，覆盖平台基线与 pass_env。",
            },
            "shell": {
                "type": "string",
                "description": "shell 程序路径。缺省时 POSIX 用 /bin/sh、Windows 用 %ComSpec%。",
            },
        },
        "additionalProperties": False,
    },
)

#: `D22` 内建命令集（技术方案 §8.1）。
#:
#: 没有斜杠命令的 Agent 仍然能对话。一份写错的 `disable` 只让这一项加载失败并留下诊断。
#:
#: 六个命令的数据全部来自 `ctx.instance` / `ctx.turns`。本内建不读盘、不出网、
#: 不起子进程。
#:
#: **六条声明必须与 `commands_core.COMMAND_NAMES` 逐一对应**，由测试对照。被 `disable`
#: 关掉的命令**不会**被注册，因此装配根必须用同一份配置过滤这里的声明
#: （`runtime/wiring.py` 的 `keep` + `commands_core.enabled_command_names()`）——否则
#: `CapabilityHost.finish()` 会以 `PLUGIN_LOAD_FAILED` 拒绝加载，而那个报错是对的。
COMMANDS_CORE: Final = PluginManifest(
    id="commands-core",
    version="0.1.0",
    sdk_range=">=5.0.0,<6.0.0",
    setup="nucleamind.builtins.commands_core:setup",
    capabilities=(
        CapabilityDecl(kind=CapabilityKind.COMMAND, name="help"),
        CapabilityDecl(kind=CapabilityKind.COMMAND, name="config"),
        CapabilityDecl(kind=CapabilityKind.COMMAND, name="session"),
        CapabilityDecl(kind=CapabilityKind.COMMAND, name="plugins"),
        CapabilityDecl(kind=CapabilityKind.COMMAND, name="capabilities"),
        CapabilityDecl(kind=CapabilityKind.COMMAND, name="cancel"),
    ),
    config_schema={
        "type": "object",
        "properties": {
            "disable": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要关掉的命令名。被关掉的命令不会注册，因此也不出现在 "
                "/help 里。表外的名字会被拒绝。",
            },
            "max_output_chars": {
                "type": "integer",
                "minimum": 1,
                "default": 16384,
                "description": "单条命令输出的字符上限，超出即截断并标注。",
            },
            "prefix": {
                "type": "string",
                "default": "/",
                "description": "/help 里印出的命令前缀。装配根会把 routing.command_prefix "
                "填在这里——内建够不着那一节配置（R4）。",
            },
        },
        "additionalProperties": False,
    },
)

#: `D23` 内建 CLI 入口（技术方案 §8.1，需求 `BAS-009`、`BAS-010`、`EDG-108`）。
#:
#: CLI 入口不可禁用；它加载失败后，Runtime 的基础能力校验会拒绝启动。装配根另有一条
#: 守卫：`plugins.disable` 试图关掉本项时显式拒绝配置（`EDG-108`）。
#:
#: **两条能力，一份 manifest**：`CLI_ENTRY` 拥有进程（决定 `nm` 返回什么退出码），
#: `CHANNEL` 拥有消息路径（`MSG-007`：CLI 的输入输出与其它平台走同一条契约）。
#: 拆成两份 manifest 会让它们可以各自被禁用，而它们共用一个控制台对象——那种组合无意义。
#:
#: stdin/stdout 是进程自己的 IO，不经过 Workspace 资源门面。
CLI_ENTRY: Final = PluginManifest(
    id="cli-entry",
    version="0.1.0",
    sdk_range=">=5.0.0,<6.0.0",
    setup="nucleamind.builtins.cli_entry:setup",
    capabilities=(
        CapabilityDecl(kind=CapabilityKind.CLI_ENTRY, name="stdio"),
        CapabilityDecl(kind=CapabilityKind.CHANNEL, name="cli"),
    ),
    config_schema={
        "type": "object",
        "properties": {
            "instance_id": {
                "type": "string",
                "description": "入站消息上标注的实例标识。装配根会填；编排用的是 "
                "OrchestratorDeps.instance_id，这里只是可追溯的标签。",
            },
            "channel_id": {
                "type": "string",
                "default": "cli",
                "description": "SessionKey 的 channel_id 分量。改它等于换一条会话线。",
            },
            "conversation_id": {
                "type": "string",
                "default": "local",
                "description": "SessionKey 的 conversation_id 分量。",
            },
            "user_id": {
                "type": "string",
                "default": "local",
                "description": "发送者标识。本地用户恒为实例拥有者（operator）。",
            },
            "prompt": {
                "type": "string",
                "default": "> ",
                "description": "交互式会话的提示符。允许空串。"
                "默认只用 ASCII——Windows 中文控制台是 GBK。",
            },
            "show_reasoning": {
                "type": "boolean",
                "default": False,
                "description": "是否显示模型的推理片段。命令行的 --reasoning 也能打开。",
            },
        },
        "additionalProperties": False,
    },
)

#: 全部内建能力的 manifest。
BUILTIN_MANIFESTS: Final[tuple[PluginManifest, ...]] = (
    SESSION_JSONL,
    CONTEXT_BASIC,
    CONTEXT_COMPACT_BASIC,
    MODEL_OPENAI,
    TOOLS_FS,
    TOOLS_FILE,
    TOOLS_SHELL,
    COMMANDS_CORE,
    CLI_ENTRY,
)
