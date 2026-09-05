"""下一次运行时刻的计算，以及创建任务时的调度校验。**纯函数，时钟由调用方传入。**

职责：把三种调度形态（`at` / `every` / `cron`）折成同一个「下一次是什么时候」的答案，
并在任务创建时把能查的错一次查完。
不负责：cron 语法（`expr.py`）、时区名解析（`settings.py`）、什么时候去问它
（`channel.py`）。

**错过的运行默认跳过。** 进程停了三天再起来，不该炸出三天份的提醒。规则由一个旋钮表达
（`catch_up_window_ms`，默认 0）：

- `0`：按 `now` 重算下一次，错过的一律不补（与参考实现 `_recompute_next_runs` 相同）。
- `> 0`：错过的到期时刻若落在 `[now - window, now]` 内则**补跑一次**，不是逐次补齐。
  「三天没开机」与「刚重启了一下」是两种情形，一个窗口就分得开。

一次性任务（`at`）过期且不在窗口内时标 `MISSED` 并停用，**不静默消失**——用户看得到
「这条本该在昨天 9 点跑」比看到一条永远排不上队的任务有用。

**`every` 的下一次从「现在」起算而不是从上一次到期时刻起算**（与参考实现同）。代价是
长 turn 会让间隔轻微漂移，收益是一条卡了十分钟的任务不会在恢复后连发五次——后者是
用户会立刻抱怨的，前者不是。
"""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo
from enum import StrEnum
from typing import Final

from karyvia.contracts import ErrorCode, KaryviaError

from .expr import parse_expr
from .job import MAX_MESSAGE_CHARS, MAX_NAME_CHARS, Schedule, ScheduleKind

__all__ = [
    "DUE_TOLERANCE_MS",
    "MAX_EVERY_MS",
    "Decision",
    "due_decision",
    "next_run_after",
    "validate_message",
    "validate_schedule",
]

#: 「醒晚了」的容差：到期时刻已过但在这个范围内仍然算准时（见 `due_decision`）。
#: 与 `catch_up_window_ms` 是两件事——那个说的是停机之后补不补，这个说的是本次唤醒。
DUE_TOLERANCE_MS: Final = 5_000

_AT_IN_PAST: Final = "一次性任务的时间必须在将来。"
_EVERY_TOO_SHORT: Final = "间隔太短了，会把实例打满。"
_EVERY_TOO_LONG: Final = "间隔太长了；这种周期请改用 cron 表达式。"
_MISSING_SCHEDULE: Final = "必须给出一种调度：at / every_seconds / cron_expr 三选一。"
_TZ_ONLY_FOR_CRON: Final = "只有 cron 表达式能带时区；间隔与一次性任务用绝对时刻。"
_EMPTY_MESSAGE: Final = "任务正文不能为空——到点要发给模型的就是它。"
_MESSAGE_TOO_LONG: Final = "任务正文太长了。"
_NAME_TOO_LONG: Final = "任务名太长了。"

#: `every` 的上界（31 天）。再长的周期用 cron 表达式表达才不会因为进程重启而漂移。
MAX_EVERY_MS: Final = 31 * 24 * 60 * 60 * 1000


def next_run_after(schedule: Schedule, moment: datetime, *, zone: tzinfo) -> datetime | None:
    """`moment` 之后的下一次运行时刻，没有则 `None`（一次性任务已过期）。

    `zone` 是 `Schedule.tz` 解析出来的时区（解析在 `settings.py`，本模块不认识名字）；
    它只对 cron 形态有意义。
    """
    if schedule.kind is ScheduleKind.AT:
        return schedule.at if schedule.at is not None and schedule.at > moment else None
    if schedule.kind is ScheduleKind.EVERY:
        if not schedule.every_ms or schedule.every_ms <= 0:
            return None
        return moment + timedelta(milliseconds=schedule.every_ms)
    if not schedule.expr:
        return None
    return parse_expr(schedule.expr).next_after(moment.astimezone(zone))


class Decision(StrEnum):
    """一条任务此刻该不该跑的三种结论。"""

    #: 到点了，派发。
    DUE = "due"
    #: 还没到点。
    PENDING = "pending"
    #: 到期时刻已经过去且在补跑窗口之外。
    STALE = "stale"


def due_decision(
    next_run_at: datetime | None, now: datetime, *, catch_up_window_ms: int
) -> Decision:
    """一条任务此刻该不该跑。

    **窗口判的是「错过了多久」而不是「错过了几次」**：一条每分钟一次的任务停机一小时，
    补跑六十遍没有任何意义，补跑一遍才是用户想要的。

    **`DUE_TOLERANCE_MS` 不是窗口的一部分，而是「醒晚了」的容差。** 调度循环睡到到期
    时刻醒来，实际总会晚上几毫秒到几十毫秒；不给容差的话 `catch_up_window_ms=0`
    会把每一次正常触发都判成 `STALE`，任务永远不跑。
    """
    if next_run_at is None:
        return Decision.STALE
    if next_run_at > now:
        return Decision.PENDING
    missed_by_ms = (now - next_run_at).total_seconds() * 1000
    if missed_by_ms <= max(catch_up_window_ms, DUE_TOLERANCE_MS):
        return Decision.DUE
    return Decision.STALE


def validate_schedule(schedule: Schedule, now: datetime, *, min_interval_ms: int) -> None:
    """创建任务时的校验。**异常约定**：不合法抛 `INPUT_MALFORMED`。

    cron 表达式在这里解析一次就丢掉：解析结果不进存储（`jobs.json` 存的是原串），
    但**不校验就意味着一条写错的表达式要等到调度循环第一次算下一次时刻才炸**，
    而那时候敲命令的人已经不在了。
    """
    if schedule.tz is not None and schedule.kind is not ScheduleKind.CRON:
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED, _TZ_ONLY_FOR_CRON, detail={"kind": schedule.kind.value}
        )
    if schedule.kind is ScheduleKind.AT:
        if schedule.at is None:
            raise KaryviaError(ErrorCode.INPUT_MALFORMED, _MISSING_SCHEDULE)
        if schedule.at <= now:
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED, _AT_IN_PAST, detail={"at": schedule.at.isoformat()}
            )
        return
    if schedule.kind is ScheduleKind.EVERY:
        _validate_every(schedule.every_ms, min_interval_ms)
        return
    if not schedule.expr:
        raise KaryviaError(ErrorCode.INPUT_MALFORMED, _MISSING_SCHEDULE)
    parse_expr(schedule.expr)


def _validate_every(every_ms: int | None, min_interval_ms: int) -> None:
    """间隔的上下界。

    下界不是洁癖：`every_seconds: 1` 会让实例每秒开一条 turn，把 session 队列打满之后
    连人敲的消息都进不来。**这是本插件唯一自己写的闸门**——同会话的串行由
    `SessionScheduler` 负责，那一层不需要在这里抄一遍。
    """
    if every_ms is None or every_ms <= 0:
        raise KaryviaError(ErrorCode.INPUT_MALFORMED, _MISSING_SCHEDULE)
    if every_ms < min_interval_ms:
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            _EVERY_TOO_SHORT,
            detail={"every_ms": every_ms, "minimum_ms": min_interval_ms},
        )
    if every_ms > MAX_EVERY_MS:
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            _EVERY_TOO_LONG,
            detail={"every_ms": every_ms, "maximum_ms": MAX_EVERY_MS},
        )


def validate_message(message: str, name: str) -> None:
    """任务正文与名字的上界。**异常约定**：不合法抛 `INPUT_MALFORMED`。"""
    if not message.strip():
        raise KaryviaError(ErrorCode.INPUT_MALFORMED, _EMPTY_MESSAGE)
    if len(message) > MAX_MESSAGE_CHARS:
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            _MESSAGE_TOO_LONG,
            detail={"length": len(message), "maximum": MAX_MESSAGE_CHARS},
        )
    if len(name) > MAX_NAME_CHARS:
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            _NAME_TOO_LONG,
            detail={"length": len(name), "maximum": MAX_NAME_CHARS},
        )
