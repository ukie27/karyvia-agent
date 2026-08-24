"""官方插件 `cron`：定时任务 / Automation（开发方案 `D40`，M5 的最后一项）。

职责：一份 manifest 声明五条能力——调度器本体（`CHANNEL`）、模型自己排期与查取消
（三条 `TOOL`）、给人用的生命周期入口（`COMMAND`）。
不负责：执行 turn（`kernel/turn/`）、把结果发回聊天平台（原 Channel 自己的
`deliver`）、决定任务该做什么（模型与用户）。

**它解决的问题**：在此之前所有 turn 都由外部输入触发（人敲字、平台来消息、HTTP 请求）。
没有任何机制能让实例在没人说话的时候自己开一条 turn，提醒 / 每日汇总 / CI 跟进这类工作
因此做不出来。

**它取代的是 `references/nanobot/nanobot/cron/`**（830 行的 `service.py` + 294 行的
`agent/tools/cron.py`），但不是移植：

- 旧实现是 gateway 里的一个常驻服务，靠 `on_job` 回调回到 agent；这里**调度器就是一条
  `CHANNEL`**，`receive()` 本身是调度循环（理由见 `channel.py`）。因此**既不需要
  `ctx.spawn_task` 也不需要 Hook**——开发方案里那条备注写在 Channel 泵扇出（`D33`）
  之前。
- 旧实现依赖 `croniter` 与 `filelock`；这里**一个第三方依赖都不引入**
  （`expr.py` 自己解析 5 字段表达式，`store.py` 用「临时文件 → `fsync` → `os.replace`」）。
  唯一的例外是 Windows 上的 `tzdata`，而**测试树不依赖它**。
- 旧实现的 `CronPayload` 有 `deliver` / `channel` / `to` / `channel_meta` /
  `origin_channel` / `origin_chat_id` 六个投递字段，外加一整套 legacy 兼容分支。
  这里只有 `Origin(channel_id, conversation_id)`：出站按 `message.channel_id` 路由是
  Kernel 既有的行为（`runtime/bootstrap.py` 的 `deliver`），插件不需要自己认路。
- 旧实现的 heartbeat（`HEARTBEAT.md`）与 local trigger（`nanobot trigger <id>`）**没有做**。
  前者是「定时 + 一段固定提示词 + 只在有结论时才说话」，用一条普通任务就能表达；
  后者要一个进程外的入队通道与至少一次投递语义，那是另一件事，不该塞进本插件。

**三条如实记着的边界**，写在这里而不是留给用户发现：

- **原 Channel 没加载时，到期 turn 的输出会被静默丢弃**（`deliver` 的既有行为）。
  turn 仍然跑完并入库。`/cron list all` 因此把 origin 印出来。
- **运行历史记的是「派发」不是「turn 的成败」**：Channel 泵吞掉 `TurnReceipt`
  （`runtime/instance.py::_fanout_for`），插件看不到 turn 的结局。
- **任务正文以命令前缀开头时会被 dispatcher 当命令分流**，而注入消息的 sender
  `is_operator=False`，因此 operator-only 命令会被拒。本插件不额外拦这件事。

**只 import `nucleamind.contracts` 与 `nucleamind.sdk`**（依赖规则 `R4`）。
**`MANIFEST` 在模块顶层且导入无副作用**（技术方案 §7.2）：发现阶段只 import 本模块取
那个对象，此时不该发生任何 IO——目录也在第一次写入时才建。
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from nucleamind.contracts import CapabilityKind, InstanceId
from nucleamind.sdk import (
    CapabilityDecl,
    NucleaAPI,
    PluginContext,
    PluginManifest,
)

from .channel import CHANNEL_NAME, METADATA_KEY, SENDER_ID, CronChannel, CronScheduler
from .commands import COMMAND_NAME, SUBCOMMANDS, CronCommand, cron_spec
from .expr import CronExpr, parse_expr
from .job import (
    MAX_HISTORY,
    MAX_MESSAGE_CHARS,
    MAX_NAME_CHARS,
    CronJob,
    Origin,
    RunRecord,
    RunStatus,
    Schedule,
    ScheduleKind,
    new_job_id,
)
from .schedule import Decision, due_decision, next_run_after, validate_schedule
from .settings import (
    CONFIG_SCHEMA,
    JOBS_DIR_NAME,
    CronSettings,
    resolve_settings,
)
from .store import JOBS_FILE, SCHEMA_VERSION, JobStore
from .tools import (
    CANCEL_TOOL,
    LIST_TOOL,
    SCHEDULE_TOOL,
    TOOL_NAMES,
    CronCancelTool,
    CronListTool,
    CronScheduleTool,
    cancel_spec,
    list_spec,
    schedule_spec,
)

__all__ = [
    "CANCEL_TOOL",
    "CHANNEL_NAME",
    "COMMAND_NAME",
    "CONFIG_SCHEMA",
    "JOBS_DIR_NAME",
    "JOBS_FILE",
    "LIST_TOOL",
    "MANIFEST",
    "MAX_HISTORY",
    "MAX_MESSAGE_CHARS",
    "MAX_NAME_CHARS",
    "METADATA_KEY",
    "SCHEDULE_TOOL",
    "SCHEMA_VERSION",
    "SENDER_ID",
    "SUBCOMMANDS",
    "TOOL_NAMES",
    "CronCancelTool",
    "CronChannel",
    "CronCommand",
    "CronExpr",
    "CronJob",
    "CronListTool",
    "CronScheduleTool",
    "CronScheduler",
    "CronSettings",
    "Decision",
    "JobStore",
    "Origin",
    "RunRecord",
    "RunStatus",
    "Schedule",
    "ScheduleKind",
    "cancel_spec",
    "cron_spec",
    "due_decision",
    "jobs_directory",
    "list_spec",
    "new_job_id",
    "next_run_after",
    "parse_expr",
    "register",
    "resolve_settings",
    "schedule_spec",
    "setup",
    "validate_schedule",
]

MANIFEST: Final = PluginManifest(
    id="cron",
    version="0.1.0",
    sdk_range=">=4.0.0,<5.0.0",
    setup="nucleamind_plugin_cron:setup",
    capabilities=(
        CapabilityDecl(kind=CapabilityKind.CHANNEL, name=CHANNEL_NAME),
        *(CapabilityDecl(kind=CapabilityKind.TOOL, name=name) for name in TOOL_NAMES),
        CapabilityDecl(kind=CapabilityKind.COMMAND, name=COMMAND_NAME),
    ),
    config_schema=CONFIG_SCHEMA,
)


def jobs_directory(ctx: PluginContext, settings: CronSettings) -> Path:
    """落点：配置的 `dir`，没配就是 `<state_dir>/cron`。

    **相对路径按状态目录解析**而不是按进程 cwd：`nm` 从哪个目录启动不该改变任务存到哪里。
    绝对路径原样采纳，因为运维显式指定的位置不应再按状态目录重写。
    """
    if not settings.directory:
        return ctx.state_dir / JOBS_DIR_NAME
    configured = Path(settings.directory)
    return configured if configured.is_absolute() else ctx.state_dir / configured


def register(api: NucleaAPI, ctx: PluginContext) -> CronScheduler:
    """真正的注册体。返回调度器，用例因此能直接驱动它。

    与 `setup()` 分开是为了让用例能在不构造整个装配根的情况下驱动它，同时保证生产路径与
    测试路径**注册的是同一批对象**（`plugins/…-memory` 的先例）。

    **五条能力共用同一个 `CronScheduler`**：Channel、三条工具与 `/cron` 看到的是同一份
    任务表。给它们各建一个会让「工具刚排的任务，命令查不到」这种问题只在并发下偶发。
    """
    settings = resolve_settings(ctx.config)
    store = JobStore(jobs_directory(ctx, settings) / JOBS_FILE)
    scheduler = CronScheduler(store, settings, InstanceId(settings.instance_id))

    api.register_channel(CHANNEL_NAME, CronChannel(scheduler))
    api.register_tool(schedule_spec(), CronScheduleTool(scheduler, settings))
    api.register_tool(list_spec(), CronListTool(scheduler, settings))
    api.register_tool(cancel_spec(), CronCancelTool(scheduler, settings))
    api.register_command(cron_spec(), CronCommand(scheduler))
    return scheduler


def setup(api: NucleaAPI) -> None:
    """注册入口。manifest 的 `setup` 字段指向它。

    **配置在这里一次校验完**（`resolve_settings` 会抛 `CONFIG_INVALID`，时区名也在那里
    解析）；**目录不在这里创建**——任务表在第一次排期时才落盘。
    **五条能力一次注册齐**：外部插件用不上装配根的 `keep` 声明过滤，声明与注册必须严格
    相等，否则 `CapabilityHost.finish()` 会以 `PLUGIN_LOAD_FAILED` 挡下——那个报错是对的。
    """
    register(api, api.ctx)
