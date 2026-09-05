"""`cron` 插件用例的替身与工厂。**模块名带插件前缀**是刻意的。

`testpaths` 一次收集整个 `plugins/`，而 pytest 按模块名去重：两个插件各有一个
`_fakes.py` 时，先导入的会顶掉后一个，另一棵测试树整体 `ImportError`。
**单独跑各自目录看不出来，跑全量才炸**（`D34` 就是这么发现的）。

职责：一个带 `state_dir` 的 `PluginContext`、可控时钟与 `sleep`、一个手写的 DST 时区，
以及构造 `ToolInvocation` / `CommandInvocation` 的小工厂。
不负责：断言（在各 `test_cron_*.py` 里）。

**手写 DST 时区（`FakeDst`）是本插件最关键的一个替身**：Windows 上没有系统时区库，
`ZoneInfo("America/Vancouver")` 需要 `tzdata`，而 CI 用 `--no-deps` 装插件。用它驱动
DST 用例，验的是**真的跳表行为**而不是「本机装没装 tzdata」。

**本插件不出网**，因此这里没有那份零网络 autouse 夹具——它连一行 httpx 都没有。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path

from karyvia_plugin_cron.job import CronJob, Origin, Schedule, ScheduleKind, new_job_id

from karyvia.contracts import (
    CommandInvocation,
    Correlation,
    InboundMessage,
    InstanceId,
    JsonValue,
    Sender,
    SessionKey,
    ToolCall,
    ToolInvocation,
    TurnId,
)
from karyvia.sdk.testing import FakePluginContext

#: 用例统一用它。三个分量都是 `validate_identifier` 认得的普通标识。
KEY = SessionKey(channel_id="cli", conversation_id="local", scope="proj")

#: 另一个会话，用来验「只看得见本会话的任务」。
OTHER_KEY = SessionKey(channel_id="chat", conversation_id="42", scope="proj")

#: 固定的时间基点：2026-08-15 是周六。
EPOCH = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)


class Clock:
    """手动推进的假时钟。**不自动前进**：调度用例要能断言「此刻还没到点」。"""

    def __init__(self, start: datetime = EPOCH) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> datetime:
        self.now += timedelta(**delta)
        return self.now


class Sleeper:
    """记录被要求睡多久的假 `sleep`，**并且真的挂起**直到用例放行或调度器被叫醒。

    挂起而不是立即返回是硬要求：调度循环是 `while True` + `await sleep`，一个立即返回的
    替身会把它变成占满事件循环的忙等——`sleeper.calls` 会涨到几万条，而「循环有没有真的
    停下来等」这件事就再也断言不了了。
    """

    def __init__(self) -> None:
        self.calls: list[float] = []
        self._gate = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)
        self._gate.clear()
        await self._gate.wait()

    def release(self) -> None:
        """让当前这次「睡眠」到点。"""
        self._gate.set()


class FakeDst(tzinfo):
    """一个手写的、会跳表的时区：UTC-8，夏令时期间 UTC-7。

    跳表时刻刻意与北美西部一致（3 月第二个周日 02:00 前跳、11 月第一个周日 02:00 回跳），
    因为那两个日期是 cron 表达式最容易出错的地方：02:30 在春季那天不存在，
    01:30 在秋季那天出现两次。
    """

    _STANDARD = timedelta(hours=-8)
    _DAYLIGHT = timedelta(hours=-7)

    def utcoffset(self, dt: datetime | None) -> timedelta:
        return self._DAYLIGHT if self._is_daylight(dt) else self._STANDARD

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(hours=1) if self._is_daylight(dt) else timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        return "FDT" if self._is_daylight(dt) else "FST"

    def _is_daylight(self, dt: datetime | None) -> bool:
        """按**本地墙钟**判断。`fold` 在这里不参与——本插件对回表那小时统一取第一次
        （`expr._localize` 用 `fold=0`），因此替身也只需要一种答案。"""
        if dt is None:
            return False
        start = self._second_sunday(dt.year, 3).replace(hour=2)
        end = self._first_sunday(dt.year, 11).replace(hour=2)
        naive = dt.replace(tzinfo=None)
        return start <= naive < end

    @staticmethod
    def _first_sunday(year: int, month: int) -> datetime:
        day = datetime(year, month, 1)
        return day + timedelta(days=(6 - day.weekday()) % 7)

    @classmethod
    def _second_sunday(cls, year: int, month: int) -> datetime:
        return cls._first_sunday(year, month) + timedelta(days=7)


#: 名字 → 时区的替身解析器。**不碰 tzdata**，理由见模块 docstring。
def fake_tz_resolver(name: str) -> tzinfo:
    if name == "Fake/Dst":
        return FakeDst()
    if name == "Fake/Utc":
        return UTC
    raise KeyError(name)


class CronContext(FakePluginContext):
    """带真实 `state_dir` 的上下文。"""

    def __init__(
        self,
        state_dir: Path,
        *,
        config: Mapping[str, JsonValue] | None = None,
    ) -> None:
        super().__init__(
            "cron",
            config=config,
            state_dir=state_dir,
        )


class Api:
    """一个只记录注册了什么的 `KaryviaAPI` 替身。

    刻意不用生产的 `CapabilityHost`：`R4` 禁止插件的测试树 import `kernel/`，而
    「声明 ⊆ 注册」那条不变量由 `test_cron_plugin.py` 自己按 manifest 对照。
    """

    def __init__(self, ctx: FakePluginContext) -> None:
        self._ctx = ctx
        self.tools: dict[str, object] = {}
        self.tool_specs: dict[str, object] = {}
        self.commands: dict[str, object] = {}
        self.command_specs: dict[str, object] = {}
        self.channels: dict[str, object] = {}

    @property
    def ctx(self) -> FakePluginContext:
        return self._ctx

    def register_tool(self, spec: object, handler: object) -> None:
        name = getattr(spec, "name")
        self.tools[name] = handler
        self.tool_specs[name] = spec

    def register_command(self, spec: object, handler: object) -> None:
        name = getattr(spec, "name")
        self.commands[name] = handler
        self.command_specs[name] = spec

    def register_channel(self, name: str, channel: object) -> None:
        self.channels[name] = channel

    @property
    def registered(self) -> frozenset[tuple[str, str]]:
        """全部注册项的 `(kind, name)`，用于与 manifest 声明逐条对照。"""
        return frozenset(
            [
                *(("channel", name) for name in self.channels),
                *(("tool", name) for name in self.tools),
                *(("command", name) for name in self.commands),
            ]
        )


def make_job(
    *,
    every_seconds: int | None = 60,
    expr: str | None = None,
    at: datetime | None = None,
    tz: str | None = None,
    key: SessionKey = KEY,
    name: str = "测试任务",
    message: str = "看一眼构建状态。",
    created_at: datetime = EPOCH,
    enabled: bool = True,
    next_run_at: datetime | None = None,
) -> CronJob:
    if at is not None:
        schedule = Schedule(kind=ScheduleKind.AT, at=at)
    elif expr is not None:
        schedule = Schedule(kind=ScheduleKind.CRON, expr=expr, tz=tz)
    else:
        schedule = Schedule(kind=ScheduleKind.EVERY, every_ms=(every_seconds or 60) * 1000)
    return CronJob(
        job_id=new_job_id(),
        name=name,
        message=message,
        origin=Origin(channel_id=key.channel_id, conversation_id=key.conversation_id),
        schedule=schedule,
        created_at=created_at,
        enabled=enabled,
        next_run_at=next_run_at,
    )


def make_correlation(key: SessionKey = KEY) -> Correlation:
    return Correlation(
        session_key=key, turn_id=TurnId("turn-1"), instance_id=InstanceId("inst-1")
    )


def make_invocation(
    name: str, arguments: Mapping[str, JsonValue], *, key: SessionKey = KEY
) -> ToolInvocation:
    return ToolInvocation(
        call=ToolCall(call_id="call-1", name=name, arguments=arguments),
        correlation=make_correlation(key),
        timeout_ms=5_000,
    )


def make_command(
    args: Sequence[str], *, is_operator: bool = False, key: SessionKey = KEY
) -> CommandInvocation:
    message = InboundMessage(
        message_id="msg-1",
        instance_id=InstanceId("inst-1"),
        channel_id=key.channel_id,
        conversation_id=key.conversation_id,
        sender=Sender(user_id="someone", is_operator=is_operator),
        content="/cron " + " ".join(args),
        timestamp=EPOCH,
    )
    return CommandInvocation(
        name="cron",
        args=tuple(args),
        raw_text=message.content,
        message=message,
        correlation=make_correlation(key),
    )


class NoCancel:
    """从不取消的 `CancelSignal`。"""

    requested = False

    def raise_if_requested(self) -> None:
        return None


class Cancelled:
    """已经被请求取消的 `CancelSignal`。"""

    requested = True

    def raise_if_requested(self) -> None:
        from karyvia.contracts import ErrorCode, KaryviaError

        raise KaryviaError(ErrorCode.CANCELLED_BY_USER, "已请求取消。")
