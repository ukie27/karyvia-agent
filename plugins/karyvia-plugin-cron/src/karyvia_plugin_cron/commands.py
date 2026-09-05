"""`/cron` 命令：给**人**用的查看、暂停、立即运行与删除入口。

职责：把一次 `/cron <子命令> ...` 变成一段文本回复。
不负责：调度（`channel.py`）、存储（`store.py`）、给模型用的入口（`tools.py`）。

**默认只看本会话，`all` 要管理员。** 与三条工具的过滤同一条理由（群聊里不该看到别的
会话排了什么），但这里多一个工具侧没有的东西：`invocation.message.sender.is_operator`。
于是 `/cron list all` 可以存在，而 `cron.list` 不能。这与
`plugins/…-memory` 里「删共享分区要管理员」是同一种更细的判定，不违反「`operator_only`
由 dispatcher 前置校验、命令自己不要再抄一遍」——抄一遍指的是重复同一条判定。

**`handle()` 约定不抛**，统一出口在 `CronCommand.handle`：`KaryviaError` 原样带出，
其余异常折成 `KERNEL_INVARIANT_VIOLATED` 且**只放类型名不放异常消息**。捕 `Exception`
不捕 `BaseException`——取消与 Ctrl-C 要放行。

**没有 `add` 子命令。** 排期要写一段给模型读的指令、挑一种调度、可能还要选时区，这件事
让模型代劳（`cron.schedule`）比让人在一行命令里拼参数好。`/cron` 因此只管已有任务的
生命周期——查、停、跑、删。这条如实写在 README 里，不是遗漏。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from karyvia.contracts import (
    CancelSignal,
    CommandInvocation,
    CommandParam,
    CommandResult,
    CommandSpec,
    Disposition,
    ErrorCode,
    KaryviaError,
    SessionKey,
)

from .channel import CronScheduler
from .job import CronJob, RunRecord

__all__ = ["COMMAND_NAME", "SUBCOMMANDS", "CronCommand", "cron_spec"]

COMMAND_NAME: Final = "cron"

#: 子命令清单。它同时是 `/cron` 无参数时印出来的用法——两处各写一遍必然分叉。
SUBCOMMANDS: Final[tuple[tuple[str, str], ...]] = (
    ("list", "列出本会话的定时任务；/cron list all 列出全部（需要管理员）"),
    ("show", "看一条的详情与运行历史：/cron show <标识>"),
    ("pause", "暂停一条：/cron pause <标识>"),
    ("resume", "恢复一条：/cron resume <标识>"),
    ("run", "立刻跑一次：/cron run <标识>"),
    ("rm", "删除一条：/cron rm <标识>"),
)

#: `list` 的「看全部」开关。
_ALL: Final = "all"

_UNKNOWN_SUBCOMMAND: Final = "不认识这个子命令。"
_MISSING_ARGUMENT: Final = "这个子命令需要一个任务标识。"
_NOT_FOUND: Final = "这个会话里没有这个定时任务。"
_OPERATOR_ONLY_ALL: Final = "列出全部会话的定时任务需要管理员权限。"
_UNEXPECTED: Final = "命令执行时发生了未预期的错误。"


def cron_spec() -> CommandSpec:
    """`/cron` 的声明。

    尾参声明 `repeated=True`：`/cron list all` 是两个参数，不声明的话 dispatcher 会按
    「参数过多」把它拒掉（`plugins/…-memory` 踩过的同一条）。
    """
    return CommandSpec(
        name=COMMAND_NAME,
        description="查看、暂停、立即运行与删除定时任务。不带参数时列出可用的子命令。",
        parameters=(
            CommandParam(
                name="subcommand",
                description=" / ".join(name for name, _ in SUBCOMMANDS),
                required=False,
            ),
            CommandParam(name="rest", description="子命令的参数。", required=False, repeated=True),
        ),
        operator_only=False,
    )


class CronCommand:
    """`contracts.CommandHandler` 的实现。"""

    __slots__ = ("_scheduler",)

    def __init__(self, scheduler: CronScheduler) -> None:
        self._scheduler = scheduler

    async def handle(self, invocation: CommandInvocation, cancel: CancelSignal) -> CommandResult:
        """**约定不抛**（`CMD-003`）：一切失败折成 `REJECTED`，会话保持可用。

        **取消语义**：入口检查一次。每个子命令都是一次内存查询或一次写盘，在这之后再插
        检查点只会得到一串必然为假的判断（`builtins/commands_core` 的同一条判定）。
        """
        try:
            cancel.raise_if_requested()
            return await self._dispatch(invocation)
        except KaryviaError as error:
            return CommandResult(disposition=Disposition.REJECTED, error=error)
        except Exception as error:  # noqa: BLE001 - 统一出口，见模块 docstring
            return CommandResult(
                disposition=Disposition.REJECTED,
                error=KaryviaError(
                    ErrorCode.KERNEL_INVARIANT_VIOLATED,
                    _UNEXPECTED,
                    # 只放类型名：第三方栈里的异常文本可能带着凭据或宿主机路径。
                    detail={"cause": type(error).__name__},
                ),
            )

    async def _dispatch(self, invocation: CommandInvocation) -> CommandResult:
        args = invocation.args
        if not args:
            return _handled(_usage())
        subcommand, rest = args[0].strip().lower(), args[1:]
        key = invocation.correlation.session_key
        if subcommand == "list":
            return self._list(key, rest, operator=invocation.message.sender.is_operator)
        if subcommand == "show":
            return self._show(key, rest)
        if subcommand in {"pause", "resume"}:
            return await self._set_enabled(key, rest, enabled=subcommand == "resume")
        if subcommand == "run":
            return await self._run(key, rest)
        if subcommand == "rm":
            return await self._remove(key, rest)
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED, _UNKNOWN_SUBCOMMAND, detail={"subcommand": subcommand}
        )

    # ------------------------------------------------------------------ 子命令

    def _list(self, key: SessionKey, rest: Sequence[str], *, operator: bool) -> CommandResult:
        wants_all = bool(rest) and rest[0].strip().lower() == _ALL
        if wants_all and not operator:
            raise KaryviaError(ErrorCode.PERMISSION_DENIED, _OPERATOR_ONLY_ALL)
        degraded = self._scheduler.degraded
        if degraded is not None:
            # 降级态要说出来。一个空列表在这里是**误导**——用户会以为自己没排过任务。
            return _handled(f"[{degraded.user_message}]")
        jobs = self._scheduler.jobs() if wants_all else self._mine(key)
        if not jobs:
            return _handled("没有定时任务。" if wants_all else "当前会话没有定时任务。")
        header = "全部定时任务：" if wants_all else "当前会话的定时任务："
        return _handled("\n".join([header, *(_summary(job, verbose=wants_all) for job in jobs)]))

    def _show(self, key: SessionKey, rest: Sequence[str]) -> CommandResult:
        job = self._require(key, rest)
        lines = [
            f"[{job.job_id}] {job.name}",
            f"  调度：{job.schedule.describe()}",
            f"  状态：{'启用' if job.enabled else '已暂停'}",
            f"  下一次：{job.next_run_at.isoformat() if job.next_run_at else '无排期'}",
            f"  投递到：{job.origin.channel_id} / {job.origin.conversation_id}",
            f"  正文：{job.message}",
        ]
        if job.history:
            lines.append("  运行历史（记的是「派发」，不是 turn 的成败）：")
            lines.extend(f"    {_run_line(record)}" for record in reversed(job.history))
        return _handled("\n".join(lines))

    async def _set_enabled(
        self, key: SessionKey, rest: Sequence[str], *, enabled: bool
    ) -> CommandResult:
        job = self._require(key, rest)
        updated = await self._scheduler.set_enabled(job.job_id, enabled)
        when = updated.next_run_at.isoformat() if updated.next_run_at else "无排期"
        action = f"已恢复，下一次 {when}" if enabled else "已暂停"
        return _handled(f"[{job.job_id}] {job.name}：{action}。")

    async def _run(self, key: SessionKey, rest: Sequence[str]) -> CommandResult:
        job = self._require(key, rest)
        await self._scheduler.run_now(job.job_id)
        # 说「已排到最近一次」而不是「已运行」：这条命令只把到期时刻挪到现在，真正的
        # 派发由调度循环做，而它要等本条命令所在的 turn 让出事件循环。
        return _handled(f"[{job.job_id}] {job.name}：已排到最近一次运行。")

    async def _remove(self, key: SessionKey, rest: Sequence[str]) -> CommandResult:
        job = self._require(key, rest)
        await self._scheduler.remove(job.job_id)
        return _handled(f"[{job.job_id}] {job.name}：已删除。")

    # ------------------------------------------------------------------ 内部

    def _mine(self, key: SessionKey) -> tuple[CronJob, ...]:
        return tuple(
            job
            for job in self._scheduler.jobs()
            if job.origin.channel_id == key.channel_id
            and job.origin.conversation_id == key.conversation_id
        )

    def _require(self, key: SessionKey, rest: Sequence[str]) -> CronJob:
        """取出本会话里的一条任务。

        **「不存在」与「是别人的」给同一个回答**：分开说等于把别的会话排了哪些任务告诉
        这里的人（`tools.py` 的同一条判定）。
        """
        if not rest or not rest[0].strip():
            raise KaryviaError(ErrorCode.INPUT_MALFORMED, _MISSING_ARGUMENT)
        job_id = rest[0].strip()
        for job in self._mine(key):
            if job.job_id == job_id:
                return job
        raise KaryviaError(ErrorCode.INPUT_MALFORMED, _NOT_FOUND, detail={"job_id": job_id})


# ------------------------------------------------------------------------------ 展示


def _summary(job: CronJob, *, verbose: bool) -> str:
    state = "启用" if job.enabled else "已暂停"
    when = job.next_run_at.isoformat() if job.next_run_at else "无排期"
    line = f"- [{job.job_id}] {job.name}｜{job.schedule.describe()}｜{state}｜下一次 {when}"
    # 看全部时把 origin 印出来：这是「为什么我在这个群里没收到提醒」唯一看得见的线索
    # ——原 Channel 没加载时出站消息会被静默丢弃（`channel.py` 的模块 docstring）。
    return f"{line}｜投递到 {job.origin.channel_id}/{job.origin.conversation_id}" if verbose else line


def _run_line(record: RunRecord) -> str:
    detail = f"（{record.detail}）" if record.detail else ""
    return f"{record.fired_at.isoformat()} {record.status.value}{detail}"


def _handled(content: str) -> CommandResult:
    return CommandResult(disposition=Disposition.COMMAND_HANDLED, content=content)


def _usage() -> str:
    return "\n".join(
        ["/cron 的子命令：", *(f"  {name:<7}{description}" for name, description in SUBCOMMANDS)]
    )
