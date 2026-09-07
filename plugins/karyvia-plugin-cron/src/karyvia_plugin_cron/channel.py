"""`CHANNEL:cron`：调度循环本身，以及供工具与命令调用的任务表 API。

职责：持有任务表、睡到下一次到期、把到期的任务变成一条 `InboundMessage` 交给 Kernel。
不负责：算下一次是什么时候（`schedule.py`）、存储（`store.py`）、参数校验
（`tools.py` / `commands.py`）。

**为什么调度器是一条 Channel。** Channel 的入站是**拉模型**
（`contracts/protocols.py::Channel.receive` 的原文），因此 `receive()` 可以直接就是
「睡到下一个到期时刻 → 产出一条消息」的异步生成器：`AgentInstance.start()` 已经会
`channel.start()` 并为它派生泵，而泵把消息投进 lane 之后**立即回来接着拉**
（`runtime/instance.py::_fanout_for`），因此一条跑十分钟的 turn 不会堵住调度。

本插件**既不需要 `ctx.spawn_task` 也不需要 Hook**：Channel 泵已按 conversation
扇出 turn，`receive` 只需作为调度循环产生入站消息。

**到期的任务注入的是「原会话」的消息**，即创建它时那个 `channel_id + conversation_id`
（见 `job.Origin`）。出站按 `message.channel_id` 路由回对应 Channel
（`runtime/bootstrap.py` 的 `deliver`），因此「每天 9 点在这个群里提醒我」是自然成立的，
不需要给装配根开任何新口子。**代价如实记着**：原 Channel 没加载时那条出站消息会被
静默丢弃（`deliver` 的既有行为，那是 `embed.submit()` 的正常情形），turn 仍然跑完并入库。
插件**不试图检测**这件事——能力名与 `channel_id` 不是一回事（`cli_entry` 的注册名是
`CHANNEL_NAME`，`channel_id` 来自它自己的配置），检测只会给出错误的把握。
`/cron list` 因此把 origin 印出来，让人自己看得见。

**任务表损坏时不让实例起不来。** `AgentInstance.start()` 里的 `await channel.start()`
没有 try/except，在这里抛异常会连 CLI 一起带走，而  要求任何配置下都存在本地
交互入口。因此 `start()` 读不出任务表时进入**降级态**：零任务、不调度、任何改动都以
`PERSISTENCE_READ_FAILED` 拒绝并指向那份 `.corrupt-<时间戳>` 备份。**不静默用空表覆盖**。

**时钟与 `sleep` 都是注入点**：用例不真的等到明天早上 9 点。注入的 `sleep` 必须真的让出
事件循环——一个不让出的替身会把调度循环变成饿死事件循环的死循环。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import replace
from datetime import datetime, tzinfo
from typing import Final

from karyvia.contracts import (
    ErrorCode,
    InboundMessage,
    InstanceId,
    JsonValue,
    KaryviaError,
    OutboundMessage,
    Sender,
)

from .job import CronJob, RunRecord, RunStatus, ScheduleKind
from .schedule import Decision, due_decision, next_run_after
from .settings import CronSettings, TzResolver, resolve_zone, zoneinfo_resolver
from .store import JobStore, utc_now

__all__ = [
    "CHANNEL_NAME",
    "METADATA_KEY",
    "SENDER_ID",
    "CronChannel",
    "CronScheduler",
]

#: 能力名，同时是默认的 `channel_id`。
CHANNEL_NAME: Final = "cron"

#: 注入消息的发送者标识。
SENDER_ID: Final = "cron"

#: 注入消息 `metadata` 里的命名空间键（平台私有字段只能落在命名空间下）。
METADATA_KEY: Final = "cron"

_DEGRADED: Final = "定时任务表当前不可用（启动时读取失败），已保全备份；修复后重启实例。"
_TOO_MANY_JOBS: Final = "定时任务数量已达上限。"
_NOT_FOUND: Final = "没有这个定时任务。"

#: 注入消息的发送者。`is_operator=False` 是刻意的：一条定时消息不该能执行 operator-only
#: 命令。`is_bot=True` 是如实标注——它确实不是人敲的。
_SENDER: Final = Sender(
    user_id=SENDER_ID, display_name="定时任务", is_operator=False, is_bot=True
)

Sleeper = Callable[[float], Awaitable[None]]


class CronScheduler:
    """任务表的唯一持有者。工具、命令与调度循环都经它读写。

    **一把锁串起「改内存 + 写盘」**：工具与命令在调度循环睡觉时会改任务表，两者交错会
    让 `jobs.json` 与内存不一致。锁的粒度是整个操作而不是只护写盘——先改内存再写盘的
    中间态被别人读到，与没有锁是一样的。
    """

    __slots__ = (
        "_degraded",
        "_instance_id",
        "_jobs",
        "_lock",
        "_now",
        "_settings",
        "_sleep",
        "_stopped",
        "_store",
        "_tz_resolver",
        "_wake",
    )

    def __init__(
        self,
        store: JobStore,
        settings: CronSettings,
        instance_id: InstanceId,
        *,
        now: Callable[[], datetime] = utc_now,
        sleep: Sleeper = asyncio.sleep,
        tz_resolver: TzResolver = zoneinfo_resolver,
    ) -> None:
        self._store = store
        self._settings = settings
        self._instance_id = instance_id
        self._now = now
        self._sleep = sleep
        self._tz_resolver = tz_resolver
        self._jobs: dict[str, CronJob] = {}
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._stopped = False
        self._degraded: KaryviaError | None = None

    # ------------------------------------------------------------------ 生命周期

    async def load(self) -> None:
        """读任务表并做一次对账。**不抛**：读不出来就进降级态（见模块 docstring）。"""
        try:
            jobs = await self._store.load()
        except KaryviaError as error:
            self._degraded = error
            self._jobs = {}
            return
        self._jobs = {job.job_id: job for job in jobs}
        if self._reconcile(self._now()):
            await self._save()

    async def stop(self) -> None:
        """让 `receive()` 结束。**不抛**（契约原文）。"""
        self._stopped = True
        self._wake.set()

    @property
    def degraded(self) -> KaryviaError | None:
        """降级态的原因，正常时为 `None`。"""
        return self._degraded

    def now(self, zone: tzinfo | None = None) -> datetime:
        """当前时刻，**本插件唯一的时钟出口**。

        工具与命令都经它取时间而不是自己调 `datetime.now()`：那样会有两个时钟，
        用例注入的那个就只管调度循环，而「排一条一分钟后的一次性任务」会去和真实墙钟比。
        `zone` 非空时换算到那个时区——报错文本里的时间因此与用户敲的对得上。
        """
        moment = self._now()
        return moment if zone is None else moment.astimezone(zone)

    # ------------------------------------------------------------------ 调度循环

    async def next_due(self) -> InboundMessage | None:
        """睡到下一个到期时刻并返回要注入的消息。`stop()` 之后返回 `None`。

        **降级态直接返回 `None`**：不调度，也不假装在等什么。
        """
        if self._degraded is not None:
            return None
        while not self._stopped:
            message = await self._take_due()
            if message is not None:
                return message
            await self._wait(self._delay_seconds())
        return None

    async def _take_due(self) -> InboundMessage | None:
        """取一条到期任务并推进它。没有到期任务时返回 `None`。

        对账与派发在同一把锁里：一条任务的「已经跑过」与「下一次是什么时候」必须一起
        落盘，否则崩在中间会让它下次启动时再跑一遍。
        """
        async with self._lock:
            now = self._now()
            changed = self._reconcile(now)
            job = self._earliest_due(now)
            if job is None:
                if changed:
                    await self._save()
                return None
            message = self._compose(job, now)
            record = RunRecord(fired_at=now, status=RunStatus.DISPATCHED)
            self._jobs[job.job_id] = job.with_run(record, next_run_at=self._next_after(job, now))
            await self._save()
            return message

    def _earliest_due(self, now: datetime) -> CronJob | None:
        """到期任务里最早的那个。一次只取一条——泵拉一次就该拿一条，
        而两条同时到期的任务差一个循环轮次派发不改变任何用户可见的结论。"""
        due = [
            job
            for job in self._jobs.values()
            if job.enabled
            and due_decision(
                job.next_run_at, now, catch_up_window_ms=self._settings.catch_up_window_ms
            )
            is Decision.DUE
        ]
        if not due:
            return None
        # `next_run_at` 在 DUE 分支上必然非 None，`or now` 只为让类型检查器闭嘴。
        return min(due, key=lambda job: job.next_run_at or now)

    def _delay_seconds(self) -> float:
        """睡多久。最近的到期时刻与 `tick_ceiling_ms` 取小。

        上界不是为了轮询：循环本来精确睡到到期时刻，它兜的是系统时钟跳变、休眠唤醒与
        DST——没有它，一次向前跳表会让循环睡过头。
        """
        ceiling = self._settings.tick_ceiling_ms / 1000
        now = self._now()
        upcoming = [
            job.next_run_at
            for job in self._jobs.values()
            if job.enabled and job.next_run_at is not None
        ]
        if not upcoming:
            return ceiling
        delay = (min(upcoming) - now).total_seconds()
        return max(0.0, min(ceiling, delay))

    async def _wait(self, delay: float) -> None:
        """睡 `delay` 秒，或被 `wake()` 提前叫醒。

        用「谁先到算谁」而不是 `wait_for`：后者拿不到注入的 `sleep`，而用例要能在不等
        真实时间的前提下驱动整个循环。
        """
        self._wake.clear()
        waker = asyncio.ensure_future(self._wake.wait())
        timer = asyncio.ensure_future(self._sleep(delay))
        try:
            await asyncio.wait({waker, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (waker, timer):
                if not task.done():
                    task.cancel()

    def wake(self) -> None:
        """叫醒调度循环。任何改动了任务表的操作都要调它，否则新任务要等到下一次
        `tick_ceiling_ms` 才被看见。"""
        self._wake.set()

    # ------------------------------------------------------------------ 任务表 API

    def jobs(self) -> tuple[CronJob, ...]:
        """全部任务，按创建时间排序。排序是输出契约的一部分——`/cron list` 的顺序
        不该随字典迭代顺序漂移。"""
        return tuple(sorted(self._jobs.values(), key=lambda job: (job.created_at, job.job_id)))

    def get(self, job_id: str) -> CronJob | None:
        return self._jobs.get(job_id)

    async def add(self, job: CronJob) -> CronJob:
        """加一条任务并算出它的第一次运行时刻。

        **异常约定**：降级态或超出条数上界抛（分别是 `PERSISTENCE_READ_FAILED` 与
        `INPUT_MALFORMED`）。
        """
        self._require_available()
        async with self._lock:
            if len(self._jobs) >= self._settings.max_jobs:
                raise KaryviaError(
                    ErrorCode.INPUT_MALFORMED,
                    _TOO_MANY_JOBS,
                    detail={"maximum": self._settings.max_jobs},
                )
            scheduled = replace(job, next_run_at=self._next_after(job, self._now()))
            self._jobs[scheduled.job_id] = scheduled
            await self._save()
        self.wake()
        return scheduled

    async def remove(self, job_id: str) -> bool:
        """删一条任务，返回它当时存在过没有。"""
        self._require_available()
        async with self._lock:
            if self._jobs.pop(job_id, None) is None:
                return False
            await self._save()
        self.wake()
        return True

    async def set_enabled(self, job_id: str, enabled: bool) -> CronJob:
        """暂停或恢复一条任务。恢复时重算下一次运行时刻——按暂停前的旧时刻恢复，
        会让一条停了一周的任务立刻补跑一次。

        **异常约定**：任务不存在抛 `INPUT_MALFORMED`。
        """
        self._require_available()
        async with self._lock:
            job = self._require(job_id)
            now = self._now()
            updated = replace(
                job,
                enabled=enabled,
                next_run_at=self._next_after(job, now) if enabled else None,
            )
            self._jobs[job_id] = updated
            await self._save()
        self.wake()
        return updated

    async def run_now(self, job_id: str) -> CronJob:
        """立刻跑一次：把下一次运行时刻挪到现在，剩下的交给调度循环。

        **刻意不自己派发一次**：那会是第二条产出消息的路径，两条路径的记账、历史与
        取消行为迟早分叉。

        **异常约定**：任务不存在抛 `INPUT_MALFORMED`。
        """
        self._require_available()
        async with self._lock:
            job = self._require(job_id)
            updated = replace(job, enabled=True, next_run_at=self._now())
            self._jobs[job_id] = updated
            await self._save()
        self.wake()
        return updated

    # ------------------------------------------------------------------ 内部

    def _require(self, job_id: str) -> CronJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED, _NOT_FOUND, detail={"job_id": job_id}
            )
        return job

    def _require_available(self) -> None:
        if self._degraded is not None:
            raise KaryviaError(
                ErrorCode.PERSISTENCE_READ_FAILED,
                _DEGRADED,
                detail=dict(self._degraded.detail),
            )

    def _reconcile(self, now: datetime) -> bool:
        """对账：补上缺失的下一次时刻，处理停机期间错过的运行。返回是否改动过。

        **三种结局分得开**（`schedule.due_decision` + `RunStatus`）：一次性任务过期是
        `MISSED` 并停用，周期任务错过窗口是 `SKIPPED` 并按 now 重排，其余不动。
        一条都不记的话，用户看到的就是一个「下一次在明天」的任务，完全看不出它昨天
        本该跑过。
        """
        changed = False
        for job_id, job in tuple(self._jobs.items()):
            if not job.enabled:
                continue
            if job.next_run_at is None:
                self._jobs[job_id] = replace(job, next_run_at=self._next_after(job, now))
                changed = True
                continue
            decision = due_decision(
                job.next_run_at, now, catch_up_window_ms=self._settings.catch_up_window_ms
            )
            if decision is not Decision.STALE:
                continue
            one_shot = job.schedule.kind is ScheduleKind.AT
            record = RunRecord(
                fired_at=job.next_run_at,
                status=RunStatus.MISSED if one_shot else RunStatus.SKIPPED,
                detail="错过了到期时刻，且不在补跑窗口内。",
            )
            self._jobs[job_id] = job.with_run(
                record, next_run_at=None if one_shot else self._next_after(job, now)
            )
            changed = True
        return changed

    def _next_after(self, job: CronJob, moment: datetime) -> datetime | None:
        """一条任务在 `moment` 之后的下一次运行时刻。

        **算不出来不等于要让调度循环崩掉**：表达式在创建时已经校验过，这里再失败只可能
        是有人手改了 `jobs.json`。折成 `None`（= 不再排期）并停用，比让整个实例的调度
        停摆好。
        """
        zone = self._settings.timezone
        try:
            if job.schedule.tz:
                zone = resolve_zone(job.schedule.tz, self._settings, tz_resolver=self._tz_resolver)
            return next_run_after(job.schedule, moment, zone=zone)
        except KaryviaError:
            return None

    def _compose(self, job: CronJob, now: datetime) -> InboundMessage:
        """把一条到期任务变成入站消息。

        `message_id` 每次都不同：去重缓存按它索引（`kernel/routing/dedup.py`），
        复用一个固定 id 会让第二次触发被当成重复投递丢掉。
        """
        metadata: Mapping[str, JsonValue] = {
            METADATA_KEY: {
                "job_id": job.job_id,
                "name": job.name,
                "schedule": job.schedule.describe(),
            }
        }
        return InboundMessage(
            message_id=f"cron-{uuid.uuid4().hex}",
            instance_id=self._instance_id,
            channel_id=job.origin.channel_id,
            conversation_id=job.origin.conversation_id,
            sender=_SENDER,
            content=job.message,
            timestamp=now,
            metadata=metadata,
        )

    async def _save(self) -> None:
        await self._store.save(self.jobs())


class CronChannel:
    """`contracts.Channel` 的实现，注册为 `CHANNEL:cron`。它只是调度器的一层门面。"""

    __slots__ = ("_channel_id", "_scheduler")

    def __init__(self, scheduler: CronScheduler, *, channel_id: str = CHANNEL_NAME) -> None:
        self._scheduler = scheduler
        self._channel_id = channel_id

    @property
    def channel_id(self) -> str:
        return self._channel_id

    async def start(self) -> None:
        """读任务表。**不抛**——读失败进降级态，理由见模块 docstring。"""
        await self._scheduler.load()

    async def stop(self) -> None:
        """让 `receive()` 结束。**不抛**（契约原文）。"""
        await self._scheduler.stop()

    async def receive(self) -> AsyncIterator[InboundMessage]:
        """调度循环。`stop()` 之后结束。

        **一条坏任务不该终止整条 Channel**：构造消息时出错的任务在
        `_next_after` / `_compose` 那一层就已经被折成「不再排期」，因此这里不需要
        再包一层 try。
        """
        while True:
            message = await self._scheduler.next_due()
            if message is None:
                return
            yield message

    async def deliver(self, message: OutboundMessage) -> None:
        """**空实现，这是刻意的。**

        到期任务注入的是原会话的消息，因此它的出站按 `message.channel_id` 路由回**那条**
        Channel，不会回到这里。本 Channel 需要一个 `channel_id` 只是因为 `Channel` 协议
        要求，它的入站是自产的。

        真正会走到这里的只有一种情况：有人手改 `jobs.json` 把 origin 的 channel 写成了
        `cron` 自己。那条消息没有可投递的去处，丢掉它比编一个去处诚实。

        **不抛**（投递失败不该把一次成功的 turn 变成失败）。
        """
        del message


def iter_job_ids(jobs: Iterable[CronJob]) -> tuple[str, ...]:
    """任务标识列表。工具与命令的返回载荷共用它。"""
    return tuple(job.job_id for job in jobs)
