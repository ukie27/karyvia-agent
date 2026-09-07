"""Session 并发三策略的测试。

两条主线：

- **单写者不变量**：同一 session 的 `run` 任何时刻至多一个在跑，且 `QUEUE` 下执行顺序
  严格等于提交顺序。三种策略共用这条不变量，因此三组用例都断言它。
- **不静默丢弃**：队列满时必须给出明确的 `INPUT_SESSION_BUSY`，提交数恒等于
  「执行 + 合并 + 拒绝」之和。

**全程不用 `sleep` 制造时序**：用 `asyncio.Event` 卡住第一个
`run`，制造出确定的重叠窗口；用 `await asyncio.sleep(0)` 只是让已创建的任务跑到第一个
挂起点，不依赖任何时长。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from karyvia.contracts import (
    ErrorCode,
    InboundMessage,
    InstanceId,
    KaryviaError,
    Sender,
    SessionKey,
    TurnId,
)
from karyvia.kernel.routing import (
    DEFAULT_QUEUE_MAX_SIZE,
    ConcurrencyPolicy,
    SessionScheduler,
    SubmitOutcome,
    SubmitStatus,
)

KEY = SessionKey(channel_id="cli", conversation_id="c1")
OTHER_KEY = SessionKey(channel_id="cli", conversation_id="c2")


def message(index: int, *, conversation_id: str = "c1") -> InboundMessage:
    return InboundMessage(
        message_id=f"m{index}",
        instance_id=InstanceId("inst"),
        channel_id="cli",
        conversation_id=conversation_id,
        sender=Sender(user_id="u1"),
        content=str(index),
        timestamp=datetime.now(UTC),
    )


class Recorder:
    """记录执行批次的执行体，可选地被一道闸门卡住第一次调用。

    `concurrent_peak` 是单写者不变量的可观测形态：它只要超过 1，就说明两个 `run` 同时
    在跑，历史写入的顺序性也就无从谈起。
    """

    def __init__(self, gate: asyncio.Event | None = None) -> None:
        self.gate = gate
        self.batches: list[tuple[str, ...]] = []
        self.in_flight = 0
        self.concurrent_peak = 0

    async def __call__(self, batch: tuple[InboundMessage, ...]) -> tuple[str, ...]:
        self.in_flight += 1
        self.concurrent_peak = max(self.concurrent_peak, self.in_flight)
        try:
            if self.gate is not None and not self.gate.is_set():
                await self.gate.wait()
            contents = tuple(item.content for item in batch)
            self.batches.append(contents)
            return contents
        finally:
            self.in_flight -= 1

    @property
    def executed_contents(self) -> list[str]:
        """按执行顺序摊平的消息内容——「历史顺序」的可断言形态。"""
        return [content for batch in self.batches for content in batch]


async def submit_all(
    scheduler: SessionScheduler[tuple[str, ...]],
    recorder: Recorder,
    gate: asyncio.Event,
    count: int,
) -> list[SubmitOutcome[tuple[str, ...]]]:
    """并发提交 `count` 条消息，第一条会卡在闸门上直到其余全部入队。"""
    tasks = [
        asyncio.ensure_future(scheduler.submit(KEY, message(index), recorder))
        for index in range(count)
    ]
    # 让每个任务跑到第一个挂起点：第一条进入 `run` 并卡在闸门上，其余全部排好队。
    await asyncio.sleep(0)
    gate.set()
    return await asyncio.gather(*tasks)


# --------------------------------------------------------------------------- QUEUE


async def test_queue_preserves_strict_fifo_under_concurrency() -> None:
    """20 条并发消息，执行顺序必须严格等于提交顺序（「不得无序修改历史」）。"""
    scheduler: SessionScheduler[tuple[str, ...]] = SessionScheduler(
        policy=ConcurrencyPolicy.QUEUE, queue_max_size=64
    )
    gate = asyncio.Event()
    recorder = Recorder(gate)

    outcomes = await submit_all(scheduler, recorder, gate, 20)

    assert recorder.executed_contents == [str(index) for index in range(20)]
    assert all(outcome.status is SubmitStatus.EXECUTED for outcome in outcomes)


async def test_queue_order_is_fixed_before_awaitables_are_scheduled() -> None:
    """FIFO 取决于 `submit()` 调用顺序，不取决于协程任务先跑到哪一个。"""
    scheduler: SessionScheduler[tuple[str, ...]] = SessionScheduler(queue_max_size=64)
    recorder = Recorder()
    submissions = [scheduler.submit(KEY, message(index), recorder) for index in range(5)]

    await asyncio.gather(*(asyncio.ensure_future(item) for item in reversed(submissions)))

    assert recorder.executed_contents == [str(index) for index in range(5)]


async def test_queue_never_runs_two_writers_at_once() -> None:
    scheduler: SessionScheduler[tuple[str, ...]] = SessionScheduler(queue_max_size=64)
    gate = asyncio.Event()
    recorder = Recorder(gate)

    await submit_all(scheduler, recorder, gate, 20)

    assert recorder.concurrent_peak == 1


async def test_queue_hands_each_run_exactly_one_message() -> None:
    scheduler: SessionScheduler[tuple[str, ...]] = SessionScheduler(queue_max_size=64)
    gate = asyncio.Event()
    recorder = Recorder(gate)

    await submit_all(scheduler, recorder, gate, 5)

    assert all(len(batch) == 1 for batch in recorder.batches)


async def test_full_queue_degrades_to_reject_without_dropping_anything() -> None:
    """队列满按策略降级为拒绝，且**没有消息被静默丢弃**：提交数 == 执行数 + 拒绝数。"""
    scheduler: SessionScheduler[tuple[str, ...]] = SessionScheduler(queue_max_size=3)
    gate = asyncio.Event()
    recorder = Recorder(gate)

    outcomes = await submit_all(scheduler, recorder, gate, 10)

    executed = [item for item in outcomes if item.status is SubmitStatus.EXECUTED]
    rejected = [item for item in outcomes if item.status is SubmitStatus.REJECTED]
    assert len(executed) + len(rejected) == 10
    assert len(executed) == 4  # 1 个持有者 + 3 个排队名额
    assert len(recorder.executed_contents) == 4
    assert all(item.error is not None for item in rejected)
    assert all(item.error.code is ErrorCode.INPUT_SESSION_BUSY for item in rejected if item.error)


# --------------------------------------------------------------------------- REJECT


async def test_reject_policy_admits_one_and_refuses_the_rest() -> None:
    scheduler: SessionScheduler[tuple[str, ...]] = SessionScheduler(
        policy=ConcurrencyPolicy.REJECT
    )
    gate = asyncio.Event()
    recorder = Recorder(gate)

    outcomes = await submit_all(scheduler, recorder, gate, 6)

    statuses = [outcome.status for outcome in outcomes]
    assert statuses.count(SubmitStatus.EXECUTED) == 1
    assert statuses.count(SubmitStatus.REJECTED) == 5
    assert recorder.executed_contents == ["0"]


async def test_reject_error_is_diagnosable() -> None:
    scheduler: SessionScheduler[tuple[str, ...]] = SessionScheduler(
        policy=ConcurrencyPolicy.REJECT
    )
    gate = asyncio.Event()
    recorder = Recorder(gate)

    outcomes = await submit_all(scheduler, recorder, gate, 2)
    rejected = next(item for item in outcomes if item.status is SubmitStatus.REJECTED)

    assert rejected.error is not None
    assert rejected.error.code is ErrorCode.INPUT_SESSION_BUSY
    assert rejected.error.detail["policy"] == "reject"


async def test_reject_frees_the_slot_after_the_run_finishes() -> None:
    """拒绝是「此刻忙」，不是「这个会话废了」。"""
    scheduler: SessionScheduler[tuple[str, ...]] = SessionScheduler(
        policy=ConcurrencyPolicy.REJECT
    )
    gate = asyncio.Event()
    recorder = Recorder(gate)
    await submit_all(scheduler, recorder, gate, 3)

    later = await scheduler.submit(KEY, message(99), recorder)

    assert later.status is SubmitStatus.EXECUTED


# --------------------------------------------------------------------------- MERGE


async def test_merge_collapses_the_backlog_into_one_batch() -> None:
    """排队中的消息合并为一次后续输入，顺序仍是到达顺序。"""
    scheduler: SessionScheduler[tuple[str, ...]] = SessionScheduler(
        policy=ConcurrencyPolicy.MERGE
    )
    gate = asyncio.Event()
    recorder = Recorder(gate)

    outcomes = await submit_all(scheduler, recorder, gate, 8)

    assert recorder.batches == [("0", "1", "2", "3", "4", "5", "6", "7")]
    assert recorder.concurrent_peak == 1
    statuses = [outcome.status for outcome in outcomes]
    assert statuses.count(SubmitStatus.EXECUTED) == 1
    assert statuses.count(SubmitStatus.MERGED) == 7


async def test_merged_submitters_receive_the_absorbing_batch_result() -> None:
    """被合并的提交方也拿到结果——消息确实被处理了，只是和别人一起。"""
    scheduler: SessionScheduler[tuple[str, ...]] = SessionScheduler(
        policy=ConcurrencyPolicy.MERGE
    )
    gate = asyncio.Event()
    recorder = Recorder(gate)

    outcomes = await submit_all(scheduler, recorder, gate, 4)
    merged = [item for item in outcomes if item.status is SubmitStatus.MERGED]

    assert merged
    assert all(item.result == ("0", "1", "2", "3") for item in merged)
    assert all(len(item.batch) == 4 for item in merged)


async def test_merge_reports_failure_to_everyone_in_the_batch() -> None:
    """整批失败时，被合并的提交方不能收到「一切正常」。"""
    scheduler: SessionScheduler[tuple[str, ...]] = SessionScheduler(
        policy=ConcurrencyPolicy.MERGE
    )
    gate = asyncio.Event()
    started = asyncio.Event()

    async def run(batch: tuple[InboundMessage, ...]) -> tuple[str, ...]:
        if not started.is_set():
            started.set()
            await gate.wait()
            return ()
        raise RuntimeError("boom")

    first = asyncio.ensure_future(scheduler.submit(KEY, message(0), run))
    await started.wait()
    tasks = [
        first,
        asyncio.ensure_future(scheduler.submit(KEY, message(1), run)),
        asyncio.ensure_future(scheduler.submit(KEY, message(2), run)),
    ]
    gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert sum(1 for item in results if isinstance(item, RuntimeError)) == 2


# --------------------------------------------------------------------------- 通用不变量


async def test_a_failing_run_leaves_the_session_usable() -> None:
    """`run` 抛异常不得留下卡死的 slot——释放走 `finally`。"""
    scheduler: SessionScheduler[str] = SessionScheduler()

    async def boom(batch: tuple[InboundMessage, ...]) -> str:
        raise RuntimeError("boom")

    async def fine(batch: tuple[InboundMessage, ...]) -> str:
        return "ok"

    with pytest.raises(RuntimeError):
        await scheduler.submit(KEY, message(1), boom)

    assert (await scheduler.submit(KEY, message(2), fine)).result == "ok"


async def test_cancelling_a_waiter_does_not_poison_the_session() -> None:
    """同步登记后，等待方取消必须移除票据并保留后续可用性。"""
    scheduler: SessionScheduler[str] = SessionScheduler()
    gate = asyncio.Event()

    async def run(batch: tuple[InboundMessage, ...]) -> str:
        await gate.wait()
        return batch[0].content

    first = asyncio.ensure_future(scheduler.submit(KEY, message(1), run))
    waiting = asyncio.ensure_future(scheduler.submit(KEY, message(2), run))
    await asyncio.sleep(0)
    waiting.cancel()
    await asyncio.gather(waiting, return_exceptions=True)

    assert scheduler.waiting(KEY) == 0
    gate.set()
    assert (await first).result == "1"
    assert (await scheduler.submit(KEY, message(3), run)).result == "3"


async def test_different_sessions_do_not_block_each_other() -> None:
    scheduler: SessionScheduler[str] = SessionScheduler()
    gate = asyncio.Event()

    async def blocked(batch: tuple[InboundMessage, ...]) -> str:
        await gate.wait()
        return "blocked"

    async def free(batch: tuple[InboundMessage, ...]) -> str:
        return "free"

    first = asyncio.ensure_future(scheduler.submit(KEY, message(1), blocked))
    await asyncio.sleep(0)
    second = await scheduler.submit(OTHER_KEY, message(2, conversation_id="c2"), free)

    assert second.result == "free"
    gate.set()
    assert (await first).result == "blocked"


async def test_idle_slots_are_reclaimed() -> None:
    """槽位不能随历史会话数无界增长。"""
    scheduler: SessionScheduler[str] = SessionScheduler()

    async def run(batch: tuple[InboundMessage, ...]) -> str:
        return "ok"

    for index in range(5):
        await scheduler.submit(KEY, message(index), run)

    assert scheduler.active_sessions() == ()


async def test_running_turn_is_visible_while_the_run_is_in_flight() -> None:
    """诊断要能回答「这个 session 正在跑哪个 turn」。"""
    scheduler: SessionScheduler[str] = SessionScheduler()
    gate = asyncio.Event()
    observed: list[TurnId | None] = []

    async def run(batch: tuple[InboundMessage, ...]) -> str:
        observed.append(scheduler.running_turn(KEY))
        await gate.wait()
        return "ok"

    task = asyncio.ensure_future(
        scheduler.submit(KEY, message(1), run, turn_id=TurnId("turn-1"))
    )
    await asyncio.sleep(0)
    gate.set()
    await task

    assert observed == [TurnId("turn-1")]
    assert scheduler.running_turn(KEY) is None


def test_non_positive_queue_bound_is_rejected() -> None:
    with pytest.raises(KaryviaError) as exc:
        SessionScheduler(queue_max_size=0)

    assert exc.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


def test_outcome_requires_an_error_exactly_when_rejected() -> None:
    with pytest.raises(KaryviaError):
        SubmitOutcome(status=SubmitStatus.REJECTED)
    with pytest.raises(KaryviaError):
        SubmitOutcome(
            status=SubmitStatus.EXECUTED,
            error=KaryviaError(ErrorCode.INPUT_SESSION_BUSY, "忙"),
        )


def test_default_queue_bound_matches_the_documented_value() -> None:
    assert DEFAULT_QUEUE_MAX_SIZE == 32
