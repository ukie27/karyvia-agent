"""三条工具的用例：参数校验、三选一、会话隔离与失败折叠。"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from _cron_fakes import (
    EPOCH,
    KEY,
    OTHER_KEY,
    Cancelled,
    Clock,
    NoCancel,
    Sleeper,
    fake_tz_resolver,
    make_invocation,
    make_job,
)
from karyvia_plugin_cron.channel import CronScheduler
from karyvia_plugin_cron.job import ScheduleKind
from karyvia_plugin_cron.settings import resolve_settings
from karyvia_plugin_cron.store import JOBS_FILE, JobStore
from karyvia_plugin_cron.tools import (
    CANCEL_TOOL,
    LIST_TOOL,
    SCHEDULE_TOOL,
    CronCancelTool,
    CronListTool,
    CronScheduleTool,
    cancel_spec,
    list_spec,
    schedule_spec,
)

from karyvia.contracts import ErrorCode, InstanceId, JsonValue, SideEffect

INSTANCE = InstanceId("inst-1")


async def build(
    tmp_path: Path, *, config: dict[str, JsonValue] | None = None
) -> tuple[CronScheduler, object, object, object, Clock]:
    clock = Clock()
    settings = resolve_settings(config or {}, tz_resolver=fake_tz_resolver)
    store = JobStore(tmp_path / JOBS_FILE, now=clock)
    scheduler = CronScheduler(
        store, settings, INSTANCE, now=clock, sleep=Sleeper(), tz_resolver=fake_tz_resolver
    )
    await scheduler.load()
    return (
        scheduler,
        CronScheduleTool(scheduler, settings, tz_resolver=fake_tz_resolver),
        CronListTool(scheduler, settings, tz_resolver=fake_tz_resolver),
        CronCancelTool(scheduler, settings, tz_resolver=fake_tz_resolver),
        clock,
    )


# ------------------------------------------------------------------------------ 声明


def test_specs_are_named_as_declared() -> None:
    assert (schedule_spec().name, list_spec().name, cancel_spec().name) == (
        SCHEDULE_TOOL,
        LIST_TOOL,
        CANCEL_TOOL,
    )


def test_list_is_read_only_and_touches_no_file() -> None:
    """任务表已经在内存里，列出来不碰任何文件。"""
    spec = list_spec()
    assert spec.read_only is True


# ------------------------------------------------------------------------------ 排期


async def test_schedules_an_interval_job(tmp_path: Path) -> None:
    scheduler, schedule, _, _, _ = await build(tmp_path)
    result = await schedule.execute(  # type: ignore[attr-defined]
        make_invocation(SCHEDULE_TOOL, {"message": "看构建", "every_seconds": 60}), NoCancel()
    )
    assert result.ok is True
    assert result.side_effect is SideEffect.OCCURRED
    (job,) = scheduler.jobs()
    assert job.schedule.kind is ScheduleKind.EVERY
    assert job.message == "看构建"


async def test_schedules_a_cron_job_with_a_timezone(tmp_path: Path) -> None:
    scheduler, schedule, _, _, _ = await build(tmp_path)
    result = await schedule.execute(  # type: ignore[attr-defined]
        make_invocation(
            SCHEDULE_TOOL,
            {"message": "晨会提醒", "cron_expr": "0 9 * * 1-5", "tz": "Fake/Dst"},
        ),
        NoCancel(),
    )
    assert result.ok is True
    (job,) = scheduler.jobs()
    assert (job.schedule.expr, job.schedule.tz) == ("0 9 * * 1-5", "Fake/Dst")
    assert job.next_run_at is not None


async def test_schedules_a_one_shot_job(tmp_path: Path) -> None:
    scheduler, schedule, _, _, _ = await build(tmp_path)
    when = (EPOCH + timedelta(days=1)).isoformat()
    result = await schedule.execute(  # type: ignore[attr-defined]
        make_invocation(SCHEDULE_TOOL, {"message": "发版本说明", "at": when}), NoCancel()
    )
    assert result.ok is True
    (job,) = scheduler.jobs()
    assert job.schedule.kind is ScheduleKind.AT


async def test_a_naive_at_is_read_in_the_default_timezone(tmp_path: Path) -> None:
    """用户敲的 `2026-08-20T09:30` 指的是他自己的 9 点半，不是 UTC 的。"""
    scheduler, schedule, _, _, _ = await build(tmp_path, config={"timezone": "Fake/Dst"})
    result = await schedule.execute(  # type: ignore[attr-defined]
        make_invocation(SCHEDULE_TOOL, {"message": "提醒", "at": "2027-08-20T09:30:00"}),
        NoCancel(),
    )
    assert result.ok is True
    (job,) = scheduler.jobs()
    assert job.schedule.at is not None
    assert job.schedule.at.utcoffset() == timedelta(hours=-7)


async def test_the_job_is_bound_to_the_calling_session(tmp_path: Path) -> None:
    """「每天 9 点在这里提醒我」里的「这里」由 `correlation.session_key` 定。"""
    scheduler, schedule, _, _, _ = await build(tmp_path)
    await schedule.execute(  # type: ignore[attr-defined]
        make_invocation(SCHEDULE_TOOL, {"message": "看构建", "every_seconds": 60}, key=OTHER_KEY),
        NoCancel(),
    )
    (job,) = scheduler.jobs()
    assert job.origin.channel_id == OTHER_KEY.channel_id
    assert job.origin.conversation_id == OTHER_KEY.conversation_id


async def test_the_name_defaults_to_the_first_line(tmp_path: Path) -> None:
    scheduler, schedule, _, _, _ = await build(tmp_path)
    await schedule.execute(  # type: ignore[attr-defined]
        make_invocation(
            SCHEDULE_TOOL, {"message": "看一眼构建\n然后汇报", "every_seconds": 60}
        ),
        NoCancel(),
    )
    (job,) = scheduler.jobs()
    assert job.name == "看一眼构建"


@pytest.mark.parametrize(
    "arguments",
    [
        {},  # 缺 message
        {"message": ""},
        {"message": "x"},  # 没给调度
        {"message": "x", "every_seconds": 60, "cron_expr": "0 9 * * *"},  # 给了两种
        {"message": "x", "every_seconds": 1},  # 低于下界
        {"message": "x", "cron_expr": "not a cron"},
        {"message": "x", "cron_expr": "0 9 * * *", "tz": "Nope/Nowhere"},
        {"message": "x", "every_seconds": 60, "tz": "Fake/Utc"},  # tz 只能配 cron
        {"message": "x", "at": "明天下午"},
        {"message": "x", "at": "2020-01-01T00:00:00+00:00"},  # 过去
        {"message": "x", "every_seconds": "60"},  # 类型不对
        {"message": "x", "every_seconds": 60, "unknown": 1},  # 表外参数
    ],
)
async def test_rejects_bad_schedule_arguments(
    tmp_path: Path, arguments: dict[str, JsonValue]
) -> None:
    """**失败一律 `side_effect=NONE`**：可失败的步骤全在写盘之前。"""
    scheduler, schedule, _, _, _ = await build(tmp_path)
    result = await schedule.execute(  # type: ignore[attr-defined]
        make_invocation(SCHEDULE_TOOL, arguments), NoCancel()
    )
    assert result.ok is False
    assert result.error is not None
    assert result.side_effect is SideEffect.NONE
    assert scheduler.jobs() == ()


async def test_giving_two_schedules_is_not_silently_resolved(tmp_path: Path) -> None:
    """静默择一会让用户以为另一个也生效了。"""
    _, schedule, _, _, _ = await build(tmp_path)
    result = await schedule.execute(  # type: ignore[attr-defined]
        make_invocation(
            SCHEDULE_TOOL, {"message": "x", "every_seconds": 60, "at": "2027-01-01T00:00:00+00:00"}
        ),
        NoCancel(),
    )
    assert result.ok is False


async def test_the_job_cap_is_reported_as_a_tool_failure(tmp_path: Path) -> None:
    _, schedule, _, _, _ = await build(tmp_path, config={"max_jobs": 1})
    first = await schedule.execute(  # type: ignore[attr-defined]
        make_invocation(SCHEDULE_TOOL, {"message": "一", "every_seconds": 60}), NoCancel()
    )
    second = await schedule.execute(  # type: ignore[attr-defined]
        make_invocation(SCHEDULE_TOOL, {"message": "二", "every_seconds": 60}), NoCancel()
    )
    assert (first.ok, second.ok) == (True, False)


# ------------------------------------------------------------------------------ 列出


async def test_list_is_empty_without_jobs(tmp_path: Path) -> None:
    _, _, listing, _, _ = await build(tmp_path)
    result = await listing.execute(  # type: ignore[attr-defined]
        make_invocation(LIST_TOOL, {}), NoCancel()
    )
    assert result.ok is True
    assert result.data is not None and result.data["count"] == 0


async def test_list_only_shows_this_session(tmp_path: Path) -> None:
    """群聊里的模型不该列出另一个会话排的任务。"""
    scheduler, _, listing, _, _ = await build(tmp_path)
    mine = await scheduler.add(make_job(name="我的", key=KEY))
    await scheduler.add(make_job(name="别人的", key=OTHER_KEY))

    result = await listing.execute(  # type: ignore[attr-defined]
        make_invocation(LIST_TOOL, {}, key=KEY), NoCancel()
    )
    assert result.data is not None
    assert result.data["job_ids"] == (mine.job_id,)
    assert "别人的" not in result.content


async def test_list_rejects_unknown_arguments(tmp_path: Path) -> None:
    _, _, listing, _, _ = await build(tmp_path)
    result = await listing.execute(  # type: ignore[attr-defined]
        make_invocation(LIST_TOOL, {"all": True}), NoCancel()
    )
    assert result.ok is False


# ------------------------------------------------------------------------------ 取消


async def test_cancel_removes_the_job(tmp_path: Path) -> None:
    scheduler, _, _, cancel_tool, _ = await build(tmp_path)
    job = await scheduler.add(make_job(key=KEY))
    result = await cancel_tool.execute(  # type: ignore[attr-defined]
        make_invocation(CANCEL_TOOL, {"job_id": job.job_id}, key=KEY), NoCancel()
    )
    assert result.ok is True
    assert result.side_effect is SideEffect.OCCURRED
    assert scheduler.jobs() == ()


async def test_cancel_refuses_another_sessions_job(tmp_path: Path) -> None:
    """「不存在」与「是别人的」给同一个回答——分开说等于泄漏别的会话排了什么。"""
    scheduler, _, _, cancel_tool, _ = await build(tmp_path)
    other = await scheduler.add(make_job(key=OTHER_KEY))

    result = await cancel_tool.execute(  # type: ignore[attr-defined]
        make_invocation(CANCEL_TOOL, {"job_id": other.job_id}, key=KEY), NoCancel()
    )
    missing = await cancel_tool.execute(  # type: ignore[attr-defined]
        make_invocation(CANCEL_TOOL, {"job_id": "cj-nope"}, key=KEY), NoCancel()
    )
    assert (result.ok, missing.ok) == (False, False)
    assert result.content == missing.content
    assert len(scheduler.jobs()) == 1


async def test_cancel_needs_a_job_id(tmp_path: Path) -> None:
    _, _, _, cancel_tool, _ = await build(tmp_path)
    result = await cancel_tool.execute(  # type: ignore[attr-defined]
        make_invocation(CANCEL_TOOL, {}), NoCancel()
    )
    assert result.ok is False


# ------------------------------------------------------------------------------ 取消信号


async def test_cancellation_is_checked_at_the_entrance(tmp_path: Path) -> None:
    """取消后仍必须返回 `ToolResult`，并如实标 `side_effect`（这里确定什么都没做）。"""
    scheduler, schedule, listing, cancel_tool, _ = await build(tmp_path)
    for tool, arguments in (
        (schedule, {"message": "x", "every_seconds": 60}),
        (listing, {}),
        (cancel_tool, {"job_id": "cj-nope"}),
    ):
        result = await tool.execute(  # type: ignore[attr-defined]
            make_invocation(SCHEDULE_TOOL, arguments), Cancelled()
        )
        assert result.ok is False
        assert result.error is not None
        assert result.error.code is ErrorCode.CANCELLED_BY_USER
        assert result.side_effect is SideEffect.NONE
    assert scheduler.jobs() == ()


async def test_a_degraded_table_fails_the_tool_not_the_turn(tmp_path: Path) -> None:
    (tmp_path / JOBS_FILE).write_text("{ not json", encoding="utf-8")
    _, schedule, _, _, _ = await build(tmp_path)
    result = await schedule.execute(  # type: ignore[attr-defined]
        make_invocation(SCHEDULE_TOOL, {"message": "x", "every_seconds": 60}), NoCancel()
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.code is ErrorCode.PERSISTENCE_READ_FAILED
