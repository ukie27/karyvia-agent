"""三条工具：`cron.schedule` / `cron.list` / `cron.cancel`。

职责：让模型自己排期、查看与取消定时任务。
不负责：调度（`channel.py`）、存储（`store.py`）、给人用的入口（`commands.py`）。

**任务绑定调用它的那个会话**：`Origin` 取自 `invocation.correlation.session_key`，
这是工具侧唯一的身份来源。于是「每天早上 9 点在这里提醒我」里的「这里」有确切含义，
不需要模型自己描述投递目标——它也描述不准。

**只看得见本会话的任务。** `cron.list` 与 `cron.cancel` 都按 origin 过滤：群聊里的模型
不该列出（更不该取消）另一个会话排的任务，而工具调用拿不到发送者身份，没法做更细的
判定。跨会话的查看归 `/cron list all`（那条路上有 `sender.is_operator`）。

**`execute()` 约定不抛**，三个类共用 `_Tool` 的那一个出口
（`plugins/…-memory/tools.py` 与 `builtins/tools_fs/base.py` 的同一种做法）。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import datetime, tzinfo
from typing import Final

from karyvia.contracts import (
    CancelSignal,
    Concurrency,
    ErrorCode,
    JsonValue,
    KaryviaError,
    RiskLevel,
    SessionKey,
    SideEffect,
    ToolInvocation,
    ToolResult,
    ToolSpec,
    TrustLevel,
)

from .channel import CronScheduler
from .job import (
    MAX_MESSAGE_CHARS,
    MAX_NAME_CHARS,
    CronJob,
    Origin,
    Schedule,
    ScheduleKind,
    new_job_id,
)
from .schedule import validate_message, validate_schedule
from .settings import CronSettings, TzResolver, resolve_zone, zoneinfo_resolver

__all__ = [
    "CANCEL_TOOL",
    "LIST_TOOL",
    "SCHEDULE_TOOL",
    "TOOL_NAMES",
    "CronCancelTool",
    "CronListTool",
    "CronScheduleTool",
    "cancel_spec",
    "list_spec",
    "schedule_spec",
]

SCHEDULE_TOOL: Final = "cron.schedule"
LIST_TOOL: Final = "cron.list"
CANCEL_TOOL: Final = "cron.cancel"

#: 三条工具的名字。manifest 的声明与 `register()` 的注册都从这一份来——外部插件的
#: 「声明 ⊆ 注册」是严格相等，两处各写一遍迟早分叉。
TOOL_NAMES: Final[tuple[str, ...]] = (SCHEDULE_TOOL, LIST_TOOL, CANCEL_TOOL)

#: 单次工具输出的字符上界。任务列表可能有上百条，整份塞进上下文没有意义。
_MAX_RESULT_CHARS: Final = 4_000

_UNKNOWN_ARGUMENT: Final = "出现了未知参数。"
_BAD_STRING_ARGUMENT: Final = "缺少必填参数或类型不对（应为非空字符串）。"
_BAD_INT_ARGUMENT: Final = "参数类型不对（应为正整数）。"
_MULTIPLE_SCHEDULES: Final = "只能给一种调度：every_seconds / cron_expr / at 三选一。"
_NO_SCHEDULE: Final = "必须给一种调度：every_seconds / cron_expr / at 三选一。"
_BAD_AT: Final = "at 必须是 ISO 8601 时刻，如 2026-08-20T09:30:00。"
_NOT_YOURS: Final = "这个会话里没有这个定时任务。"


def schedule_spec() -> ToolSpec:
    """`cron.schedule` 的声明。"""
    return ToolSpec(
        name=SCHEDULE_TOOL,
        description=(
            "排一个定时任务：到点时 message 会作为一条新消息发给你，你按它去做事，"
            "结果自动回到当前这个会话。适合提醒、每日汇总、周期检查。"
            "三种调度三选一：every_seconds（间隔）、cron_expr（如 '0 9 * * 1-5'）、at（一次性）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "maxLength": MAX_MESSAGE_CHARS,
                    "description": "到点时发给你的指令原文，要能独立读懂——那时没有别的上下文。",
                },
                "name": {
                    "type": "string",
                    "maxLength": MAX_NAME_CHARS,
                    "description": "任务的短名字，只用于展示。留空即取 message 的开头。",
                },
                "every_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "间隔多少秒跑一次。",
                },
                "cron_expr": {
                    "type": "string",
                    "description": (
                        "5 字段 cron 表达式：分 时 日 月 周。支持 * , - */n 与三字母星期名。"
                    ),
                },
                "tz": {
                    "type": "string",
                    "description": "cron_expr 的 IANA 时区名（如 Asia/Shanghai）。只能与 cron_expr 一起用。",
                },
                "at": {
                    "type": "string",
                    "description": "一次性任务的时刻，ISO 8601。不带时区时按默认时区解释。",
                },
            },
            "required": ["message"],
            "additionalProperties": False,
        },
        # 排期会把任务表整份写回磁盘（`store.py`），因此不是只读操作。
        read_only=False,
        risk=RiskLevel.MUTATING,
    )


def list_spec() -> ToolSpec:
    """`cron.list` 的声明。"""
    return ToolSpec(
        name=LIST_TOOL,
        description="列出当前会话里已排期的定时任务（含标识、调度与下一次运行时刻）。",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        # 任务表已经在内存里（`CronScheduler` 是它的唯一持有者），列出来不碰任何文件。
        read_only=True,
        risk=RiskLevel.SAFE,
    )


def cancel_spec() -> ToolSpec:
    """`cron.cancel` 的声明。"""
    return ToolSpec(
        name=CANCEL_TOOL,
        description="按标识取消当前会话里的一个定时任务。标识来自 cron.list。",
        parameters={
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "要取消的任务标识。"},
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
        read_only=False,
        # 取消不可撤销：任务连同它的运行历史一起消失。
        risk=RiskLevel.DESTRUCTIVE,
        concurrency=Concurrency.EXCLUSIVE,
    )


class _Tool:
    """三条工具的公共外壳：计时、入口取消检查与失败折叠。

    **失败一律 `side_effect=NONE`**：可失败的步骤（参数校验、调度校验、找任务）全部
    发生在写盘之前，而写盘走「临时文件 → `fsync` → `os.replace`」，替换成功之后没有
    可失败的步骤。判据与 `builtins/tools_fs` 逐字相同，因此本插件一次 `UNKNOWN`
    都不产出。
    """

    __slots__ = ("_scheduler", "_settings", "_tz_resolver")

    def __init__(
        self,
        scheduler: CronScheduler,
        settings: CronSettings,
        *,
        tz_resolver: TzResolver = zoneinfo_resolver,
    ) -> None:
        self._scheduler = scheduler
        self._settings = settings
        self._tz_resolver = tz_resolver

    #: 成功时的副作用档位。
    side_effect: SideEffect = SideEffect.NONE

    #: 成功时正文的可信度。默认是自己的话——三条工具交出的都是本插件渲染的
    #: 回执。**`cron.list` 例外**：它把任务正文原样印出来，而那段文字是谁创建任务谁写的
    #: （群聊里任何人都能敲 `/cron`），因此它声明 `UNTRUSTED`。
    trust: TrustLevel = TrustLevel.SYSTEM

    async def execute(self, invocation: ToolInvocation, cancel: CancelSignal) -> ToolResult:
        """**约定不抛**。**取消语义**：入口检查一次；一次调用只有一次读/写盘往返。"""
        started = time.perf_counter()
        try:
            cancel.raise_if_requested()
            content, data = await self.run(invocation, cancel)
        except KaryviaError as error:
            text, cut = _truncate(error.user_message, _MAX_RESULT_CHARS)
            return ToolResult(
                call_id=invocation.call.call_id,
                ok=False,
                content=text,
                truncated=cut,
                side_effect=SideEffect.NONE,
                error=error,
                duration_ms=_elapsed_ms(started),
                # 失败正文是本层自己写的文案，不含外部内容。
                trust=TrustLevel.SYSTEM,
            )
        text, cut = _truncate(content, _MAX_RESULT_CHARS)
        return ToolResult(
            call_id=invocation.call.call_id,
            ok=True,
            content=text,
            truncated=cut,
            side_effect=self.side_effect,
            data=data,
            duration_ms=_elapsed_ms(started),
            trust=self.trust,
        )

    async def run(
        self, invocation: ToolInvocation, cancel: CancelSignal
    ) -> tuple[str, Mapping[str, JsonValue] | None]:
        raise NotImplementedError

    def _mine(self, key: SessionKey) -> tuple[CronJob, ...]:
        """本会话的任务。过滤理由见模块 docstring。"""
        return tuple(
            job
            for job in self._scheduler.jobs()
            if job.origin.channel_id == key.channel_id
            and job.origin.conversation_id == key.conversation_id
        )


class CronScheduleTool(_Tool):
    """排一个定时任务。"""

    __slots__ = ()

    side_effect = SideEffect.OCCURRED

    async def run(
        self, invocation: ToolInvocation, cancel: CancelSignal
    ) -> tuple[str, Mapping[str, JsonValue] | None]:
        del cancel  # 写入不接受取消（契约原文）。
        arguments = invocation.call.arguments
        _reject_unknown(arguments, {"message", "name", "every_seconds", "cron_expr", "tz", "at"})
        message = _require_str(arguments, "message")
        name = _optional_str(arguments, "name") or _derive_name(message)
        validate_message(message, name)

        schedule = self._build_schedule(arguments)
        now = self._scheduler.now(self._zone_for(schedule))
        validate_schedule(schedule, now, min_interval_ms=self._settings.min_interval_ms)

        key = invocation.correlation.session_key
        job = await self._scheduler.add(
            CronJob(
                job_id=new_job_id(),
                name=name,
                message=message,
                origin=Origin(channel_id=key.channel_id, conversation_id=key.conversation_id),
                schedule=schedule,
                created_at=now,
            )
        )
        data: dict[str, JsonValue] = {
            "job_id": job.job_id,
            "schedule": job.schedule.describe(),
            "next_run_at": job.next_run_at.isoformat() if job.next_run_at else None,
        }
        when = job.next_run_at.isoformat() if job.next_run_at else "（暂无排期）"
        return f"已排期「{name}」（{job.schedule.describe()}），标识 {job.job_id}，下一次 {when}。", data

    def _build_schedule(self, arguments: Mapping[str, JsonValue]) -> Schedule:
        """三选一。**给了两种是错误而不是「取第一个」**——静默择一会让用户以为另一个
        也生效了。

        **`tz` 一律原样带上**，哪怕这一种调度用不着它：判定「tz 只能配 cron」的地方只有
        `validate_schedule` 一处，在这里顺手把它丢掉就等于让那条规则失效——
        用户会以为自己设的时区生效了。
        """
        every = _optional_int(arguments, "every_seconds")
        expr = _optional_str(arguments, "cron_expr")
        at_text = _optional_str(arguments, "at")
        tz = _optional_str(arguments, "tz") or None
        given = [item is not None and item != "" for item in (every, expr, at_text)]
        if sum(given) > 1:
            raise KaryviaError(ErrorCode.INPUT_MALFORMED, _MULTIPLE_SCHEDULES)
        if every:
            return Schedule(kind=ScheduleKind.EVERY, every_ms=every * 1000, tz=tz)
        if expr:
            return Schedule(kind=ScheduleKind.CRON, expr=expr, tz=tz)
        if at_text:
            return Schedule(kind=ScheduleKind.AT, at=self._parse_at(at_text), tz=tz)
        raise KaryviaError(ErrorCode.INPUT_MALFORMED, _NO_SCHEDULE)

    def _parse_at(self, text: str) -> datetime:
        """解析一次性任务的时刻。**不带时区时按默认时区补全**，不按 UTC——
        用户敲的 `2026-08-20T09:30` 指的是他自己的 9 点半。"""
        try:
            moment = datetime.fromisoformat(text)
        except ValueError as error:
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED, _BAD_AT, detail={"at": text}
            ) from error
        if moment.tzinfo is None:
            return moment.replace(tzinfo=self._settings.timezone)
        return moment

    def _zone_for(self, schedule: Schedule) -> tzinfo:
        if schedule.tz:
            return resolve_zone(schedule.tz, self._settings, tz_resolver=self._tz_resolver)
        return self._settings.timezone


class CronListTool(_Tool):
    """列出本会话的定时任务。"""

    __slots__ = ()

    #: **唯一一条不可信的**：列表把每个任务的正文原样印出来，而那段文字是创建任务的人
    #: 写的——群聊里任何人都能敲 `/cron`，模型自己也能调 `cron.schedule`。
    trust = TrustLevel.UNTRUSTED

    async def run(
        self, invocation: ToolInvocation, cancel: CancelSignal
    ) -> tuple[str, Mapping[str, JsonValue] | None]:
        del cancel
        _reject_unknown(invocation.call.arguments, set())
        jobs = self._mine(invocation.correlation.session_key)
        data: dict[str, JsonValue] = {
            "count": len(jobs),
            "job_ids": [job.job_id for job in jobs],
        }
        if not jobs:
            return "当前会话没有定时任务。", data
        return "\n".join(["当前会话的定时任务：", *(_describe(job) for job in jobs)]), data


class CronCancelTool(_Tool):
    """取消本会话的一个定时任务。"""

    __slots__ = ()

    side_effect = SideEffect.OCCURRED

    async def run(
        self, invocation: ToolInvocation, cancel: CancelSignal
    ) -> tuple[str, Mapping[str, JsonValue] | None]:
        del cancel  # 删除不接受取消（契约原文）。
        arguments = invocation.call.arguments
        _reject_unknown(arguments, {"job_id"})
        job_id = _require_str(arguments, "job_id")
        mine = {job.job_id for job in self._mine(invocation.correlation.session_key)}
        if job_id not in mine:
            # 「不存在」与「是别人的」对调用方是同一个结论，也应当是同一个回答：
            # 分开说等于告诉这个会话里的模型别的会话排了哪些任务。
            raise KaryviaError(ErrorCode.INPUT_MALFORMED, _NOT_YOURS, detail={"job_id": job_id})
        removed = await self._scheduler.remove(job_id)
        return "已取消这个定时任务。", {"job_id": job_id, "removed": removed}


# ------------------------------------------------------------------------------ 展示


def _describe(job: CronJob) -> str:
    """一条任务的一行摘要。工具与 `/cron` 共用它，两处各写一份必然分叉。"""
    state = "已暂停" if not job.enabled else "启用"
    when = job.next_run_at.isoformat() if job.next_run_at else "无排期"
    return f"- [{job.job_id}] {job.name}｜{job.schedule.describe()}｜{state}｜下一次 {when}"


def _derive_name(message: str) -> str:
    """没给名字时从正文取一个。截断到上界之内，避免名字校验反过来拒掉正文。"""
    first_line = message.strip().splitlines()[0].strip()
    return first_line[:MAX_NAME_CHARS] or "定时任务"


# ------------------------------------------------------------------------------ 参数


def _reject_unknown(arguments: Mapping[str, JsonValue], allowed: set[str]) -> None:
    """表外参数是错误而不是可忽略的多余字段。

    Kernel 的 `ToolInvoker` 已按 schema 校验过一遍（`additionalProperties: false`），
    这里再挡一次是因为 `ToolHandler` 是公开契约：`sdk.testing.ToolContract` 直接调它。
    """
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            _UNKNOWN_ARGUMENT,
            detail={"unknown": unknown, "allowed": sorted(allowed)},
        )


def _require_str(arguments: Mapping[str, JsonValue], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            _BAD_STRING_ARGUMENT,
            detail={"argument": key, "actual_type": type(value).__name__},
        )
    return value.strip()


def _optional_str(arguments: Mapping[str, JsonValue], key: str) -> str:
    value = arguments.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            _BAD_STRING_ARGUMENT,
            detail={"argument": key, "actual_type": type(value).__name__},
        )
    return value.strip()


def _optional_int(arguments: Mapping[str, JsonValue], key: str) -> int | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            _BAD_INT_ARGUMENT,
            detail={"argument": key, "actual_type": type(value).__name__},
        )
    return value


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))
