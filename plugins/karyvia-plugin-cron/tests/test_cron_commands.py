"""`/cron` 命令的用例：子命令分流、会话隔离、`all` 的管理员判定与「约定不抛」。"""

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
    make_command,
    make_job,
)
from karyvia_plugin_cron.channel import CronScheduler
from karyvia_plugin_cron.commands import COMMAND_NAME, SUBCOMMANDS, CronCommand, cron_spec
from karyvia_plugin_cron.job import RunRecord, RunStatus
from karyvia_plugin_cron.settings import resolve_settings
from karyvia_plugin_cron.store import JOBS_FILE, JobStore

from karyvia.contracts import Disposition, ErrorCode, InstanceId, JsonValue

INSTANCE = InstanceId("inst-1")


async def build(
    tmp_path: Path, *, config: dict[str, JsonValue] | None = None
) -> tuple[CronScheduler, CronCommand, Clock]:
    clock = Clock()
    settings = resolve_settings(config or {}, tz_resolver=fake_tz_resolver)
    store = JobStore(tmp_path / JOBS_FILE, now=clock)
    scheduler = CronScheduler(
        store, settings, INSTANCE, now=clock, sleep=Sleeper(), tz_resolver=fake_tz_resolver
    )
    await scheduler.load()
    return scheduler, CronCommand(scheduler), clock


# ------------------------------------------------------------------------------ 声明


def test_spec_is_named_as_declared() -> None:
    assert cron_spec().name == COMMAND_NAME


def test_the_trailing_parameter_is_repeated() -> None:
    """`/cron list all` 是两个参数；不声明 `repeated` 会被 dispatcher 按「参数过多」拒掉。"""
    assert cron_spec().parameters[-1].repeated is True


def test_it_is_not_operator_only() -> None:
    """看自己排的任务不该要管理员——`all` 那一档才要（更细的判定，不是同一条）。"""
    assert cron_spec().operator_only is False


def test_usage_lists_every_subcommand() -> None:
    """用法只有一份来源，两处各写一遍必然分叉。"""
    assert {name for name, _ in SUBCOMMANDS} == {
        "list",
        "show",
        "pause",
        "resume",
        "run",
        "rm",
    }


# ------------------------------------------------------------------------------ 分流


async def test_no_arguments_prints_the_usage(tmp_path: Path) -> None:
    _, command, _ = await build(tmp_path)
    result = await command.handle(make_command([]), NoCancel())
    assert result.disposition is Disposition.COMMAND_HANDLED
    for name, _ in SUBCOMMANDS:
        assert name in result.content


async def test_an_unknown_subcommand_is_rejected(tmp_path: Path) -> None:
    _, command, _ = await build(tmp_path)
    result = await command.handle(make_command(["nonsense"]), NoCancel())
    assert result.disposition is Disposition.REJECTED
    assert result.error is not None
    assert result.error.detail["subcommand"] == "nonsense"


# ------------------------------------------------------------------------------ list


async def test_list_is_empty_without_jobs(tmp_path: Path) -> None:
    _, command, _ = await build(tmp_path)
    result = await command.handle(make_command(["list"]), NoCancel())
    assert result.disposition is Disposition.COMMAND_HANDLED
    assert "没有定时任务" in result.content


async def test_list_only_shows_this_session(tmp_path: Path) -> None:
    scheduler, command, _ = await build(tmp_path)
    await scheduler.add(make_job(name="我的", key=KEY))
    await scheduler.add(make_job(name="别人的", key=OTHER_KEY))

    result = await command.handle(make_command(["list"], key=KEY), NoCancel())

    assert "我的" in result.content
    assert "别人的" not in result.content


async def test_list_all_needs_an_operator(tmp_path: Path) -> None:
    scheduler, command, _ = await build(tmp_path)
    await scheduler.add(make_job(name="别人的", key=OTHER_KEY))

    result = await command.handle(make_command(["list", "all"], is_operator=False), NoCancel())

    assert result.disposition is Disposition.REJECTED
    assert result.error is not None
    assert result.error.code is ErrorCode.PERMISSION_DENIED


async def test_list_all_shows_every_session_to_an_operator(tmp_path: Path) -> None:
    scheduler, command, _ = await build(tmp_path)
    await scheduler.add(make_job(name="我的", key=KEY))
    await scheduler.add(make_job(name="别人的", key=OTHER_KEY))

    result = await command.handle(make_command(["list", "all"], is_operator=True), NoCancel())

    assert "我的" in result.content
    assert "别人的" in result.content


async def test_list_all_prints_the_delivery_target(tmp_path: Path) -> None:
    """这是「为什么我在这个群里没收到提醒」唯一看得见的线索：原 Channel 没加载时
    出站消息会被静默丢弃。"""
    scheduler, command, _ = await build(tmp_path)
    await scheduler.add(make_job(key=OTHER_KEY))

    result = await command.handle(make_command(["list", "all"], is_operator=True), NoCancel())

    assert f"{OTHER_KEY.channel_id}/{OTHER_KEY.conversation_id}" in result.content


async def test_list_says_so_when_the_table_is_degraded(tmp_path: Path) -> None:
    """一个空列表在降级态下是**误导**——用户会以为自己没排过任务。"""
    (tmp_path / JOBS_FILE).write_text("{ not json", encoding="utf-8")
    _, command, _ = await build(tmp_path)

    result = await command.handle(make_command(["list"]), NoCancel())

    assert result.disposition is Disposition.COMMAND_HANDLED
    # 印的是 store 给的那句原话：它点名了那份 `.corrupt-<时间戳>` 备份，
    # 比插件自己编一句「当前不可用」更能让人接着往下做。
    assert "损坏" in result.content
    assert ".corrupt-" in result.content


# ------------------------------------------------------------------------------ show


async def test_show_prints_the_details(tmp_path: Path) -> None:
    scheduler, command, _ = await build(tmp_path)
    job = await scheduler.add(make_job(name="构建检查", message="看一眼构建。"))

    result = await command.handle(make_command(["show", job.job_id]), NoCancel())

    assert "构建检查" in result.content
    assert "看一眼构建。" in result.content
    assert f"{KEY.channel_id} / {KEY.conversation_id}" in result.content


async def test_show_labels_history_as_dispatch_not_outcome(tmp_path: Path) -> None:
    """插件看不到 turn 的结局（泵吞掉 `TurnReceipt`），因此不能让人以为它看得到。"""
    scheduler, command, _ = await build(tmp_path)
    job = await scheduler.add(make_job())
    updated = job.with_run(
        RunRecord(fired_at=EPOCH, status=RunStatus.DISPATCHED), next_run_at=EPOCH
    )
    await scheduler.remove(job.job_id)
    await scheduler.add(updated)

    result = await command.handle(make_command(["show", updated.job_id]), NoCancel())

    assert "派发" in result.content
    assert RunStatus.DISPATCHED.value in result.content


@pytest.mark.parametrize("subcommand", ["show", "pause", "resume", "run", "rm"])
async def test_subcommands_need_a_job_id(tmp_path: Path, subcommand: str) -> None:
    _, command, _ = await build(tmp_path)
    result = await command.handle(make_command([subcommand]), NoCancel())
    assert result.disposition is Disposition.REJECTED


@pytest.mark.parametrize("subcommand", ["show", "pause", "resume", "run", "rm"])
async def test_subcommands_refuse_another_sessions_job(
    tmp_path: Path, subcommand: str
) -> None:
    """「不存在」与「是别人的」给同一个回答（`tools.py` 采用的相同规则判定）。"""
    scheduler, command, _ = await build(tmp_path)
    other = await scheduler.add(make_job(key=OTHER_KEY))

    result = await command.handle(make_command([subcommand, other.job_id], key=KEY), NoCancel())
    missing = await command.handle(make_command([subcommand, "cj-nope"], key=KEY), NoCancel())

    assert result.disposition is Disposition.REJECTED
    assert result.error is not None and missing.error is not None
    assert result.error.user_message == missing.error.user_message
    assert len(scheduler.jobs()) == 1


# ------------------------------------------------------------------------------ 生命周期


async def test_pause_then_resume(tmp_path: Path) -> None:
    scheduler, command, clock = await build(tmp_path)
    job = await scheduler.add(make_job(every_seconds=60))

    paused = await command.handle(make_command(["pause", job.job_id]), NoCancel())
    assert "已暂停" in paused.content
    stored = scheduler.get(job.job_id)
    assert stored is not None and stored.enabled is False

    clock.advance(days=7)
    resumed = await command.handle(make_command(["resume", job.job_id]), NoCancel())
    assert "已恢复" in resumed.content
    stored = scheduler.get(job.job_id)
    assert stored is not None
    assert stored.next_run_at == clock.now + timedelta(seconds=60)


async def test_run_says_scheduled_not_ran(tmp_path: Path) -> None:
    """这条命令只把到期时刻挪到现在，真正的派发由调度循环做——说「已运行」是不实的。"""
    scheduler, command, _ = await build(tmp_path)
    job = await scheduler.add(make_job(every_seconds=86_400))

    result = await command.handle(make_command(["run", job.job_id]), NoCancel())

    assert "已排到最近一次运行" in result.content
    stored = scheduler.get(job.job_id)
    assert stored is not None and stored.next_run_at is not None


async def test_rm_removes_the_job(tmp_path: Path) -> None:
    scheduler, command, _ = await build(tmp_path)
    job = await scheduler.add(make_job())

    result = await command.handle(make_command(["rm", job.job_id]), NoCancel())

    assert result.disposition is Disposition.COMMAND_HANDLED
    assert scheduler.jobs() == ()


# ------------------------------------------------------------------------------ 约定不抛


async def test_cancellation_is_folded_into_a_rejection(tmp_path: Path) -> None:
    _, command, _ = await build(tmp_path)
    result = await command.handle(make_command(["list"]), Cancelled())
    assert result.disposition is Disposition.REJECTED
    assert result.error is not None
    assert result.error.code is ErrorCode.CANCELLED_BY_USER


async def test_an_unexpected_error_carries_only_the_type_name(tmp_path: Path) -> None:
    """第三方栈里的异常文本可能带着凭据或宿主机路径，因此只放类型名。"""

    class Exploding:
        degraded = None

        def jobs(self) -> tuple[object, ...]:
            raise RuntimeError("secret-token-abc123")

    command = CronCommand(Exploding())  # type: ignore[arg-type]
    result = await command.handle(make_command(["list"]), NoCancel())

    assert result.disposition is Disposition.REJECTED
    assert result.error is not None
    assert result.error.code is ErrorCode.KERNEL_INVARIANT_VIOLATED
    assert result.error.detail == {"cause": "RuntimeError"}
    assert "secret-token-abc123" not in result.error.user_message


async def test_handle_never_raises_for_any_subcommand(tmp_path: Path) -> None:
    """一切失败折成 `REJECTED`，会话保持可用。"""
    _, command, _ = await build(tmp_path)
    for args in (
        [],
        ["list"],
        ["list", "all"],
        ["show"],
        ["show", "cj-nope"],
        ["pause", "cj-nope"],
        ["resume", "cj-nope"],
        ["run", "cj-nope"],
        ["rm", "cj-nope"],
        ["nonsense"],
        [""],
    ):
        result = await command.handle(make_command(args), NoCancel())
        assert result.disposition in {Disposition.COMMAND_HANDLED, Disposition.REJECTED}
