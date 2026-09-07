"""官方 Cron 插件：以 Channel 能力实现定时任务与自动化。

职责：注册调度 Channel、模型工具和用户命令；持久化任务并在到期时产生入站消息。
不负责：执行 Turn、替原 Channel 投递结果或判断任务内容。

`receive()` 本身就是调度循环，实例的 Channel 泵负责扇出 Turn。投递位置只保存
`Origin(channel_id, conversation_id)`，结果仍由对应 Channel 处理。任务历史记录的是派发，
不是 Turn 成败；原 Channel 未加载时，Turn 仍执行和入库，但输出无法投递。任务正文若以命令
前缀开头，会按普通入站消息进入 Dispatcher，且定时任务不具备 operator 身份。
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from karyvia.contracts import CapabilityKind, InstanceId
from karyvia.sdk import (
    CapabilityDecl,
    KaryviaAPI,
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
    sdk_range=">=5.0.0,<6.0.0",
    setup="karyvia_plugin_cron:setup",
    capabilities=(
        CapabilityDecl(kind=CapabilityKind.CHANNEL, name=CHANNEL_NAME),
        *(CapabilityDecl(kind=CapabilityKind.TOOL, name=name) for name in TOOL_NAMES),
        CapabilityDecl(kind=CapabilityKind.COMMAND, name=COMMAND_NAME),
    ),
    config_schema=CONFIG_SCHEMA,
)


def jobs_directory(ctx: PluginContext, settings: CronSettings) -> Path:
    """落点：配置的 `dir`，没配就是 `<state_dir>/cron`。

    **相对路径按状态目录解析**而不是按进程 cwd：`karyvia` 从哪个目录启动不该改变任务存到哪里。
    绝对路径原样采纳，因为运维显式指定的位置不应再按状态目录重写。
    """
    if not settings.directory:
        return ctx.state_dir / JOBS_DIR_NAME
    configured = Path(settings.directory)
    return configured if configured.is_absolute() else ctx.state_dir / configured


def register(api: KaryviaAPI, ctx: PluginContext) -> CronScheduler:
    """真正的注册体。返回调度器，用例因此能直接驱动它。

    与 `setup()` 分开是为了让用例能在不构造整个装配根的情况下驱动它，同时保证生产路径与
    测试路径**注册的是同一批对象**（`plugins/…-memory` 的既有实现）。

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


def setup(api: KaryviaAPI) -> None:
    """注册入口。manifest 的 `setup` 字段指向它。

    **配置在这里一次校验完**（`resolve_settings` 会抛 `CONFIG_INVALID`，时区名也在那里
    解析）；**目录不在这里创建**——任务表在第一次排期时才落盘。
    **五条能力一次注册齐**：外部插件用不上装配根的 `keep` 声明过滤，声明与注册必须严格
    相等，否则 `CapabilityHost.finish()` 会以 `PLUGIN_LOAD_FAILED` 挡下——那个报错是对的。
    """
    register(api, api.ctx)
