"""`channel.py` 的用例：调度循环、注入消息的形状、对账与降级态。

**注入的 `sleep` 真的挂起**（`_cron_fakes.Sleeper`）：立即返回的替身会把
`while True` + `await sleep` 变成占满事件循环的忙等，而「循环有没有真的停下来等」
就再也断言不了了。等一条到期消息一律经 `due()`，它带超时——挂住的用例给不出信息。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path

import pytest
from _cron_fakes import (
    EPOCH,
    KEY,
    OTHER_KEY,
    Clock,
    Sleeper,
    fake_tz_resolver,
    make_job,
)
from karyvia_plugin_cron.channel import METADATA_KEY, SENDER_ID, CronChannel, CronScheduler
from karyvia_plugin_cron.job import RunStatus
from karyvia_plugin_cron.settings import resolve_settings
from karyvia_plugin_cron.store import JOBS_FILE, JobStore

from karyvia.contracts import ErrorCode, InboundMessage, InstanceId, KaryviaError

INSTANCE = InstanceId("inst-1")


def build(
    tmp_path: Path, *, config: dict[str, object] | None = None
) -> tuple[CronScheduler, Clock, Sleeper]:
    clock = Clock()
    sleeper = Sleeper()
    settings = resolve_settings(config or {}, tz_resolver=fake_tz_resolver)  # type: ignore[arg-type]
    store = JobStore(tmp_path / JOBS_FILE, now=clock)
    scheduler = CronScheduler(
        store,
        settings,
        INSTANCE,
        now=clock,
        sleep=sleeper,
        tz_resolver=fake_tz_resolver,
    )
    return scheduler, clock, sleeper


async def due(scheduler: CronScheduler, *, timeout: float = 2.0) -> InboundMessage | None:
    """等一条到期消息，**带超时**。

    直接 `await scheduler.next_due()` 在「本该到期却没到期」时会永远挂住，而一条挂住的
    用例给不出任何信息。超时让它变成一次失败。
    """
    return await asyncio.wait_for(scheduler.next_due(), timeout=timeout)


# ------------------------------------------------------------------------------ 派发


async def test_a_due_job_produces_a_message(tmp_path: Path) -> None:
    scheduler, clock, _ = build(tmp_path)
    await scheduler.load()
    job = await scheduler.add(make_job(every_seconds=60))
    clock.advance(seconds=61)

    message = await due(scheduler)

    assert message is not None
    assert message.content == job.message


async def test_the_message_is_addressed_at_the_origin_session(tmp_path: Path) -> None:
    """这是本插件的核心：turn 跑在**创建任务时那个会话**里，出站因此回到那条 Channel。"""
    scheduler, clock, _ = build(tmp_path)
    await scheduler.load()
    await scheduler.add(make_job(every_seconds=60, key=OTHER_KEY))
    clock.advance(seconds=61)

    message = await due(scheduler)

    assert message is not None
    assert message.channel_id == OTHER_KEY.channel_id
    assert message.conversation_id == OTHER_KEY.conversation_id


async def test_the_sender_is_not_an_operator(tmp_path: Path) -> None:
    """一条定时消息不该能执行 operator-only 命令。"""
    scheduler, clock, _ = build(tmp_path)
    await scheduler.load()
    await scheduler.add(make_job(every_seconds=60))
    clock.advance(seconds=61)

    message = await due(scheduler)

    assert message is not None
    assert message.sender.user_id == SENDER_ID
    assert message.sender.is_operator is False
    assert message.sender.is_bot is True


async def test_metadata_carries_the_job_under_a_namespace(tmp_path: Path) -> None:
    """平台私有字段只能落在命名空间键下。"""
    scheduler, clock, _ = build(tmp_path)
    await scheduler.load()
    job = await scheduler.add(make_job(every_seconds=60, name="构建检查"))
    clock.advance(seconds=61)

    message = await due(scheduler)

    assert message is not None
    payload = message.metadata[METADATA_KEY]
    assert isinstance(payload, Mapping)
    assert payload["job_id"] == job.job_id
    assert payload["name"] == "构建检查"


async def test_each_firing_gets_a_fresh_message_id(tmp_path: Path) -> None:
    """去重缓存按 `message_id` 索引：复用一个固定 id 会让第二次触发被当成重复投递丢掉。"""
    scheduler, clock, _ = build(tmp_path)
    await scheduler.load()
    await scheduler.add(make_job(every_seconds=60))

    ids: set[str] = set()
    for _ in range(3):
        clock.advance(seconds=61)
        message = await due(scheduler)
        assert message is not None
        ids.add(message.message_id)
    assert len(ids) == 3


async def test_nothing_due_means_the_loop_sleeps(tmp_path: Path) -> None:
    scheduler, _, sleeper = build(tmp_path)
    await scheduler.load()
    await scheduler.add(make_job(every_seconds=600))

    task = asyncio.ensure_future(scheduler.next_due())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not task.done()
    assert sleeper.calls  # 真的睡了，而不是空转
    await scheduler.stop()
    assert await task is None


async def test_sleep_is_bounded_by_the_tick_ceiling(tmp_path: Path) -> None:
    """上界兜的是系统时钟跳变：没有它，一次向前跳表会让循环睡过头。"""
    scheduler, _, sleeper = build(tmp_path, config={"tick_ceiling_ms": 1_000})
    await scheduler.load()
    await scheduler.add(make_job(every_seconds=86_400))

    task = asyncio.ensure_future(scheduler.next_due())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await scheduler.stop()
    await task
    assert sleeper.calls and max(sleeper.calls) <= 1.0


async def test_an_empty_table_still_sleeps_the_ceiling(tmp_path: Path) -> None:
    scheduler, _, sleeper = build(tmp_path, config={"tick_ceiling_ms": 2_000})
    await scheduler.load()

    task = asyncio.ensure_future(scheduler.next_due())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await scheduler.stop()
    await task
    assert sleeper.calls == [2.0]


async def test_disabled_jobs_never_fire(tmp_path: Path) -> None:
    scheduler, clock, _ = build(tmp_path)
    await scheduler.load()
    job = await scheduler.add(make_job(every_seconds=60))
    await scheduler.set_enabled(job.job_id, False)
    clock.advance(hours=1)

    task = asyncio.ensure_future(scheduler.next_due())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await scheduler.stop()
    assert await task is None


async def test_the_earliest_due_job_goes_first(tmp_path: Path) -> None:
    """两条同时到期时先派发早的那条。

    **必须给补跑窗口**：默认窗口是 0，而第二条在第一条被取走时已经「晚了 80 秒」，
    那正是 `due_decision` 要判成 `STALE` 的情形——这条用例验的是排序，不是补跑策略。
    """
    scheduler, clock, _ = build(tmp_path, config={"catch_up_window_ms": 600_000})
    await scheduler.load()
    later = await scheduler.add(make_job(every_seconds=120, name="晚", message="晚的那条"))
    sooner = await scheduler.add(make_job(every_seconds=60, name="早", message="早的那条"))
    clock.advance(seconds=200)

    first = await due(scheduler)
    second = await due(scheduler)

    assert first is not None and second is not None
    assert (first.content, second.content) == (sooner.message, later.message)


# ------------------------------------------------------------------------------ 状态推进


async def test_firing_records_the_dispatch_and_advances(tmp_path: Path) -> None:
    scheduler, clock, _ = build(tmp_path)
    await scheduler.load()
    job = await scheduler.add(make_job(every_seconds=60))
    clock.advance(seconds=61)
    await due(scheduler)

    updated = scheduler.get(job.job_id)
    assert updated is not None
    assert updated.history[-1].status is RunStatus.DISPATCHED
    assert updated.next_run_at == clock.now + timedelta(seconds=60)


async def test_a_one_shot_job_is_disabled_after_running(tmp_path: Path) -> None:
    """一次性任务跑完就停用而不是删掉——用户还要看得到它跑过。"""
    scheduler, clock, _ = build(tmp_path)
    await scheduler.load()
    job = await scheduler.add(make_job(at=EPOCH + timedelta(minutes=1), every_seconds=None))
    clock.advance(seconds=61)
    assert await due(scheduler) is not None

    updated = scheduler.get(job.job_id)
    assert updated is not None
    assert updated.enabled is False
    assert updated.next_run_at is None


async def test_state_survives_a_restart(tmp_path: Path) -> None:
    scheduler, clock, _ = build(tmp_path)
    await scheduler.load()
    job = await scheduler.add(make_job(every_seconds=60))
    clock.advance(seconds=61)
    await due(scheduler)

    revived, _, _ = build(tmp_path)
    await revived.load()
    restored = revived.get(job.job_id)
    assert restored is not None
    assert restored.history[-1].status is RunStatus.DISPATCHED


# ------------------------------------------------------------------------------ 对账


async def test_downtime_without_a_window_skips_and_reschedules(tmp_path: Path) -> None:
    """默认不补跑：停了三天再起来，不该炸出三天份的提醒。"""
    store = JobStore(tmp_path / JOBS_FILE, now=Clock())
    stale = make_job(every_seconds=60, next_run_at=EPOCH - timedelta(days=3))
    await store.save((stale,))

    scheduler, clock, _ = build(tmp_path)
    await scheduler.load()

    job = scheduler.get(stale.job_id)
    assert job is not None
    assert job.history[-1].status is RunStatus.SKIPPED
    assert job.next_run_at == clock.now + timedelta(seconds=60)


async def test_downtime_inside_the_window_catches_up_once(tmp_path: Path) -> None:
    store = JobStore(tmp_path / JOBS_FILE, now=Clock())
    stale = make_job(every_seconds=60, next_run_at=EPOCH - timedelta(minutes=5))
    await store.save((stale,))

    scheduler, _, _ = build(tmp_path, config={"catch_up_window_ms": 10 * 60 * 1000})
    await scheduler.load()

    message = await due(scheduler)
    assert message is not None


async def test_a_missed_one_shot_is_marked_not_silently_dropped(tmp_path: Path) -> None:
    """「这条本该在昨天 9 点跑」比一条永远排不上队的任务有用。"""
    store = JobStore(tmp_path / JOBS_FILE, now=Clock())
    stale = make_job(
        at=EPOCH - timedelta(days=1), every_seconds=None, next_run_at=EPOCH - timedelta(days=1)
    )
    await store.save((stale,))

    scheduler, _, _ = build(tmp_path)
    await scheduler.load()

    job = scheduler.get(stale.job_id)
    assert job is not None
    assert job.history[-1].status is RunStatus.MISSED
    assert job.enabled is False


async def test_a_job_without_a_next_run_gets_one(tmp_path: Path) -> None:
    store = JobStore(tmp_path / JOBS_FILE, now=Clock())
    fresh = make_job(every_seconds=60, next_run_at=None)
    await store.save((fresh,))

    scheduler, clock, _ = build(tmp_path)
    await scheduler.load()

    job = scheduler.get(fresh.job_id)
    assert job is not None
    assert job.next_run_at == clock.now + timedelta(seconds=60)


async def test_a_hand_edited_unschedulable_job_does_not_break_the_loop(tmp_path: Path) -> None:
    """表达式在创建时校验过，这里再失败只可能是有人手改了 jobs.json。
    折成「不再排期」，比让整个实例的调度停摆好。"""
    path = tmp_path / JOBS_FILE
    broken = make_job(expr="0 9 * * *", every_seconds=None)
    document = {
        "version": 1,
        "jobs": [
            {
                "id": broken.job_id,
                "name": broken.name,
                "message": broken.message,
                "enabled": True,
                "created_at": EPOCH.isoformat(),
                "origin": {"channel_id": "cli", "conversation_id": "local"},
                "schedule": {"kind": "cron", "expr": "0 0 30 2 *"},
                "history": [],
            }
        ],
    }
    path.write_text(json.dumps(document), encoding="utf-8")

    scheduler, _, _ = build(tmp_path)
    await scheduler.load()

    job = scheduler.get(broken.job_id)
    assert job is not None
    assert job.next_run_at is None


# ------------------------------------------------------------------------------ 任务表 API


async def test_add_computes_the_first_run(tmp_path: Path) -> None:
    scheduler, clock, _ = build(tmp_path)
    await scheduler.load()
    job = await scheduler.add(make_job(every_seconds=60, next_run_at=None))
    assert job.next_run_at == clock.now + timedelta(seconds=60)


async def test_add_respects_the_job_cap(tmp_path: Path) -> None:
    scheduler, _, _ = build(tmp_path, config={"max_jobs": 2})
    await scheduler.load()
    await scheduler.add(make_job())
    await scheduler.add(make_job())
    with pytest.raises(KaryviaError) as caught:
        await scheduler.add(make_job())
    assert caught.value.detail["maximum"] == 2


async def test_remove_reports_whether_it_existed(tmp_path: Path) -> None:
    scheduler, _, _ = build(tmp_path)
    await scheduler.load()
    job = await scheduler.add(make_job())
    assert await scheduler.remove(job.job_id) is True
    assert await scheduler.remove(job.job_id) is False


async def test_resume_recomputes_the_next_run(tmp_path: Path) -> None:
    """按暂停前的旧时刻恢复，会让一条停了一周的任务立刻补跑一次。"""
    scheduler, clock, _ = build(tmp_path)
    await scheduler.load()
    job = await scheduler.add(make_job(every_seconds=60))
    await scheduler.set_enabled(job.job_id, False)
    clock.advance(days=7)
    resumed = await scheduler.set_enabled(job.job_id, True)
    assert resumed.next_run_at == clock.now + timedelta(seconds=60)


async def test_pausing_clears_the_next_run(tmp_path: Path) -> None:
    scheduler, _, _ = build(tmp_path)
    await scheduler.load()
    job = await scheduler.add(make_job(every_seconds=60))
    paused = await scheduler.set_enabled(job.job_id, False)
    assert paused.next_run_at is None


async def test_run_now_makes_it_due(tmp_path: Path) -> None:
    """`run_now` 只把到期时刻挪到现在，**刻意不自己派发一次**——那会是第二条产出消息的
    路径，两条路径的记账迟早分叉。"""
    scheduler, _, _ = build(tmp_path)
    await scheduler.load()
    job = await scheduler.add(make_job(every_seconds=86_400))
    await scheduler.run_now(job.job_id)

    message = await due(scheduler)
    assert message is not None
    assert message.content == job.message


async def test_run_now_revives_a_paused_job(tmp_path: Path) -> None:
    scheduler, _, _ = build(tmp_path)
    await scheduler.load()
    job = await scheduler.add(make_job(every_seconds=86_400))
    await scheduler.set_enabled(job.job_id, False)
    revived = await scheduler.run_now(job.job_id)
    assert revived.enabled is True


async def test_missing_job_id_is_reported(tmp_path: Path) -> None:
    scheduler, _, _ = build(tmp_path)
    await scheduler.load()
    for action in (
        scheduler.set_enabled("cj-nope", True),
        scheduler.run_now("cj-nope"),
    ):
        with pytest.raises(KaryviaError) as caught:
            await action
        assert caught.value.code is ErrorCode.INPUT_MALFORMED


async def test_jobs_are_listed_in_creation_order(tmp_path: Path) -> None:
    """排序是输出契约的一部分——`/cron list` 的顺序不该随字典迭代顺序漂移。"""
    scheduler, _, _ = build(tmp_path)
    await scheduler.load()
    first = await scheduler.add(make_job(name="一", created_at=EPOCH))
    second = await scheduler.add(make_job(name="二", created_at=EPOCH + timedelta(minutes=1)))
    assert [job.job_id for job in scheduler.jobs()] == [first.job_id, second.job_id]


async def test_mutations_wake_the_loop(tmp_path: Path) -> None:
    """新任务不该等到下一次 `tick_ceiling_ms` 才被看见。"""
    scheduler, clock, _ = build(tmp_path, config={"tick_ceiling_ms": 3_600_000})
    await scheduler.load()

    task = asyncio.ensure_future(scheduler.next_due())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not task.done()

    job = await scheduler.add(make_job(every_seconds=60))
    clock.advance(seconds=61)
    message = await asyncio.wait_for(task, timeout=1)
    assert message is not None and message.content == job.message


# ------------------------------------------------------------------------------ 降级态


async def test_a_corrupt_table_degrades_instead_of_raising(tmp_path: Path) -> None:
    """`AgentInstance.start()` 里的 `await channel.start()` 没有 try/except，
    在那里抛异常会连 CLI 一起带走。"""
    (tmp_path / JOBS_FILE).write_text("{ not json", encoding="utf-8")
    scheduler, _, _ = build(tmp_path)

    await scheduler.load()  # 不抛

    assert scheduler.degraded is not None
    assert scheduler.jobs() == ()


async def test_a_degraded_scheduler_refuses_mutations(tmp_path: Path) -> None:
    """**不静默用空表覆盖**：那会把还能恢复的数据盖掉。"""
    (tmp_path / JOBS_FILE).write_text("{ not json", encoding="utf-8")
    scheduler, _, _ = build(tmp_path)
    await scheduler.load()

    with pytest.raises(KaryviaError) as caught:
        await scheduler.add(make_job())
    assert caught.value.code is ErrorCode.PERSISTENCE_READ_FAILED


async def test_a_degraded_scheduler_does_not_schedule(tmp_path: Path) -> None:
    (tmp_path / JOBS_FILE).write_text("{ not json", encoding="utf-8")
    scheduler, _, sleeper = build(tmp_path)
    await scheduler.load()

    assert await scheduler.next_due() is None
    assert sleeper.calls == []  # 不调度，也不假装在等什么


# ------------------------------------------------------------------------------ Channel 门面


async def test_channel_iterates_until_stopped(tmp_path: Path) -> None:
    scheduler, clock, _ = build(tmp_path)
    channel = CronChannel(scheduler)
    await channel.start()
    await scheduler.add(make_job(every_seconds=60))
    clock.advance(seconds=61)

    received = []
    stream = channel.receive()
    received.append(await anext(stream))
    await channel.stop()
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert len(received) == 1


async def test_channel_id_is_stable(tmp_path: Path) -> None:
    scheduler, _, _ = build(tmp_path)
    assert CronChannel(scheduler).channel_id == "cron"


async def test_deliver_is_a_no_op_and_never_raises(tmp_path: Path) -> None:
    """到期任务的出站回的是**原** Channel，不会回到这里。**不抛**。"""
    from karyvia.contracts import OutboundMessage, SessionKey, StreamState, TurnId

    scheduler, _, _ = build(tmp_path)
    channel = CronChannel(scheduler)
    # `OutboundMessage` 强制寻址信息与 `session_key` 一致，因此这里的 key 也得是 cron 的。
    key = SessionKey(channel_id="cron", conversation_id=KEY.conversation_id, scope=KEY.scope)
    await channel.deliver(
        OutboundMessage(
            session_key=key,
            channel_id=key.channel_id,
            conversation_id=key.conversation_id,
            turn_id=TurnId("turn-1"),
            content="whatever",
            stream_state=StreamState.FINAL,
        )
    )
