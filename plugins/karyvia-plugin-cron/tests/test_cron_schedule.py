"""`schedule.py` 的用例：三种调度形态、创建时校验、补跑窗口与「醒晚了」的容差。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from _cron_fakes import EPOCH, FakeDst
from karyvia_plugin_cron.job import Schedule, ScheduleKind
from karyvia_plugin_cron.schedule import (
    DUE_TOLERANCE_MS,
    MAX_EVERY_MS,
    Decision,
    due_decision,
    next_run_after,
    validate_message,
    validate_schedule,
)

from karyvia.contracts import ErrorCode, KaryviaError

MIN_INTERVAL_MS = 10_000


# ------------------------------------------------------------------------------ 推进


def test_at_returns_the_moment_itself_when_still_future() -> None:
    when = EPOCH + timedelta(hours=1)
    schedule = Schedule(kind=ScheduleKind.AT, at=when)
    assert next_run_after(schedule, EPOCH, zone=UTC) == when


def test_at_returns_none_once_it_has_passed() -> None:
    """一次性任务过期就没有下一次了——`None` 是「不再排期」，不是错误。"""
    schedule = Schedule(kind=ScheduleKind.AT, at=EPOCH - timedelta(minutes=1))
    assert next_run_after(schedule, EPOCH, zone=UTC) is None


def test_every_counts_from_now_not_from_the_missed_slot() -> None:
    """间隔从「现在」起算：一条卡了十分钟的任务恢复后不该连发五次。"""
    schedule = Schedule(kind=ScheduleKind.EVERY, every_ms=60_000)
    assert next_run_after(schedule, EPOCH, zone=UTC) == EPOCH + timedelta(minutes=1)


def test_every_without_interval_has_no_next_run() -> None:
    schedule = Schedule(kind=ScheduleKind.EVERY, every_ms=None)
    assert next_run_after(schedule, EPOCH, zone=UTC) is None


def test_cron_is_evaluated_in_the_given_zone() -> None:
    """同一个瞬间在两个时区里的墙钟不同，因此「每天 9 点」的下一次也不同。"""
    schedule = Schedule(kind=ScheduleKind.CRON, expr="0 9 * * *")
    in_utc = next_run_after(schedule, EPOCH, zone=UTC)
    in_fake = next_run_after(schedule, EPOCH, zone=FakeDst())
    assert in_utc is not None and in_fake is not None
    assert in_utc != in_fake


def test_cron_without_expression_has_no_next_run() -> None:
    assert next_run_after(Schedule(kind=ScheduleKind.CRON), EPOCH, zone=UTC) is None


# ------------------------------------------------------------------------------ 到期


def test_future_is_pending() -> None:
    assert due_decision(EPOCH + timedelta(hours=1), EPOCH, catch_up_window_ms=0) is (
        Decision.PENDING
    )


def test_exactly_now_is_due() -> None:
    assert due_decision(EPOCH, EPOCH, catch_up_window_ms=0) is Decision.DUE


def test_waking_up_slightly_late_is_still_due() -> None:
    """没有容差的话 `catch_up_window_ms=0` 会把每一次正常触发都判成过期，任务永远不跑。"""
    late = EPOCH + timedelta(milliseconds=DUE_TOLERANCE_MS - 1)
    assert due_decision(EPOCH, late, catch_up_window_ms=0) is Decision.DUE


def test_long_downtime_without_a_window_is_stale() -> None:
    assert due_decision(EPOCH, EPOCH + timedelta(hours=3), catch_up_window_ms=0) is (
        Decision.STALE
    )


def test_downtime_inside_the_window_is_due() -> None:
    """窗口判的是「错过了多久」，落在窗口内就补跑一次。"""
    decision = due_decision(
        EPOCH, EPOCH + timedelta(minutes=5), catch_up_window_ms=10 * 60 * 1000
    )
    assert decision is Decision.DUE


def test_downtime_outside_the_window_is_stale() -> None:
    decision = due_decision(
        EPOCH, EPOCH + timedelta(hours=5), catch_up_window_ms=10 * 60 * 1000
    )
    assert decision is Decision.STALE


def test_no_next_run_is_stale() -> None:
    assert due_decision(None, EPOCH, catch_up_window_ms=0) is Decision.STALE


# ------------------------------------------------------------------------------ 校验


def test_accepts_the_three_shapes() -> None:
    for schedule in (
        Schedule(kind=ScheduleKind.AT, at=EPOCH + timedelta(hours=1)),
        Schedule(kind=ScheduleKind.EVERY, every_ms=MIN_INTERVAL_MS),
        Schedule(kind=ScheduleKind.CRON, expr="0 9 * * 1-5"),
    ):
        validate_schedule(schedule, EPOCH, min_interval_ms=MIN_INTERVAL_MS)


@pytest.mark.parametrize(
    "schedule",
    [
        # 一次性任务在过去
        Schedule(kind=ScheduleKind.AT, at=EPOCH - timedelta(seconds=1)),
        Schedule(kind=ScheduleKind.AT, at=None),
        # 间隔越界
        Schedule(kind=ScheduleKind.EVERY, every_ms=MIN_INTERVAL_MS - 1),
        Schedule(kind=ScheduleKind.EVERY, every_ms=MAX_EVERY_MS + 1),
        Schedule(kind=ScheduleKind.EVERY, every_ms=0),
        Schedule(kind=ScheduleKind.EVERY, every_ms=None),
        # cron 表达式缺失或写错
        Schedule(kind=ScheduleKind.CRON, expr=None),
        Schedule(kind=ScheduleKind.CRON, expr=""),
        Schedule(kind=ScheduleKind.CRON, expr="0 99 * * *"),
        # tz 只能与 cron 一起用
        Schedule(kind=ScheduleKind.EVERY, every_ms=MIN_INTERVAL_MS, tz="Asia/Shanghai"),
    ],
)
def test_rejects_bad_schedules(schedule: Schedule) -> None:
    with pytest.raises(KaryviaError) as caught:
        validate_schedule(schedule, EPOCH, min_interval_ms=MIN_INTERVAL_MS)
    assert caught.value.code is ErrorCode.INPUT_MALFORMED


def test_bad_expression_is_caught_at_creation_not_at_the_first_tick() -> None:
    """不在创建时校验的话，一条写错的表达式要等调度循环第一次算下一次时刻才炸，
    而那时候敲命令的人已经不在了。"""
    with pytest.raises(KaryviaError):
        validate_schedule(
            Schedule(kind=ScheduleKind.CRON, expr="not a cron"),
            EPOCH,
            min_interval_ms=MIN_INTERVAL_MS,
        )


def test_interval_lower_bound_names_the_minimum() -> None:
    with pytest.raises(KaryviaError) as caught:
        validate_schedule(
            Schedule(kind=ScheduleKind.EVERY, every_ms=1_000),
            EPOCH,
            min_interval_ms=MIN_INTERVAL_MS,
        )
    assert caught.value.detail["minimum_ms"] == MIN_INTERVAL_MS


@pytest.mark.parametrize(
    ("message", "name"),
    [("", "n"), ("   ", "n"), ("x" * 2_001, "n"), ("ok", "n" * 81)],
)
def test_rejects_bad_message_or_name(message: str, name: str) -> None:
    with pytest.raises(KaryviaError):
        validate_message(message, name)


def test_accepts_a_normal_message() -> None:
    validate_message("看一眼构建状态。", "构建检查")


def test_naive_at_is_never_produced_by_validation() -> None:
    """校验拿到的时刻必须是带时区的——比较一个 naive 与一个 aware 会直接 TypeError。"""
    with pytest.raises(TypeError):
        validate_schedule(
            Schedule(kind=ScheduleKind.AT, at=datetime(2027, 1, 1)),
            EPOCH,
            min_interval_ms=MIN_INTERVAL_MS,
        )
