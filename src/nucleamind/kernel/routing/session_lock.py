"""Session 并发：三种策略与单写者不变量（技术方案 §6.5，需求 `KER-008`、`EDG-202`）。

职责：为每个 `SessionKey` 维护一个槽位，按 `queue` / `merge` / `reject` 三种策略决定一条
入站消息是排队、被合并进下一次执行，还是立刻被拒绝；并保证同一 session 的执行体
（`run`）任何时刻至多有一个在跑。
不负责：执行 turn（`run` 由调用方给）、写会话历史（写者是 `run` 里的编排层）、去重
（`dedup.py`）、发布事件（`D14`）。

**不变量只有一条，三种策略共用同一段代码**：`run` 只在持有槽位时被调用，且同一 session
同一时刻至多一个持有者。历史因此不可能乱序或并发写——这正是 `KER-008` 要的东西。三种
策略的差别只在「拿不到槽位时怎么办」：排队、并进下一批、还是拒绝。

**显式 FIFO 票据，不用 `asyncio.Lock`。** `Lock` 的唤醒顺序是 CPython 的实现细节而不是
文档保证，而 `EDG-202` 要断言的恰好是严格 FIFO；顺带地，票据让「队列多长」「谁在跑」
变成可读的状态，而 `Lock` 的等待者是私有的。
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Generic, TypeVar

from nucleamind.contracts import ErrorCode, InboundMessage, NucleaError, SessionKey, TurnId

__all__ = [
    "DEFAULT_QUEUE_MAX_SIZE",
    "ConcurrencyPolicy",
    "SessionScheduler",
    "SessionSlot",
    "SubmitOutcome",
    "SubmitStatus",
]

#: 单个 session 的等待上限。超出即降级为拒绝，不静默丢弃（技术方案 §6.5 末段）。
#: 与 `kernel/config/schema.py` 的同名常量必须相等。
DEFAULT_QUEUE_MAX_SIZE: Final = 32

#: `run` 的返回类型。调度器不解释它，只负责搬运给提交方与被合并的提交方。
_T = TypeVar("_T")


class ConcurrencyPolicy(StrEnum):
    """同一 session 收到多条消息时的处置策略（`KER-008`）。取值与配置字面量同名。"""

    QUEUE = "queue"
    """默认：串行排队，严格 FIFO。"""

    MERGE = "merge"
    """排队中的消息合并为一次后续输入，只跑一次。"""

    REJECT = "reject"
    """槽位被占即返回明确的忙碌错误，不排队。"""


class SubmitStatus(StrEnum):
    """一次提交的三种归宿。"""

    EXECUTED = "executed"
    """本次提交自己拿到了槽位并执行了 `run`。"""

    MERGED = "merged"
    """消息被并进了别人的那一批（只可能出现在 `MERGE` 策略下）。"""

    REJECTED = "rejected"
    """未执行也未被合并，`error` 说明原因。"""


@dataclass(frozen=True, slots=True)
class SubmitOutcome(Generic[_T]):
    """一次提交的结果。

    `MERGED` 也带 `result` 与 `batch`：消息确实被处理了，只是和别人一起。让被合并的提交方
    拿到那一批的返回值，调用方就不需要再为「我的消息去哪了」维护一张映射表。
    """

    status: SubmitStatus
    result: _T | None = None
    #: 实际交给 `run` 的那一批消息；`REJECTED` 时为空。
    batch: tuple[InboundMessage, ...] = ()
    error: NucleaError | None = None

    def __post_init__(self) -> None:
        if (self.status is SubmitStatus.REJECTED) != (self.error is not None):
            raise NucleaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "error 必须且只能出现在 REJECTED 的提交结果上。",
                detail={"status": self.status.value},
            )


@dataclass(slots=True)
class _Merged(Generic[_T]):
    """「你的消息被并进这一批了」——由持有者塞进被合并票据的 future。"""

    result: _T
    batch: tuple[InboundMessage, ...]


class _Ticket(Generic[_T]):
    """一次排队。`future` 在真的需要等待时才创建，队头直接拿槽位不必绕一圈事件循环。"""

    __slots__ = ("future", "message")

    def __init__(self, message: InboundMessage) -> None:
        self.message = message
        self.future: asyncio.Future[_Merged[_T] | None] | None = None

    def wait_handle(self) -> asyncio.Future[_Merged[_T] | None]:
        future = asyncio.get_running_loop().create_future()
        self.future = future
        return future


@dataclass(slots=True)
class SessionSlot(Generic[_T]):
    """一个 session 的槽位（技术方案 §6.5 的同名 dataclass）。

    `pending` 只在 `MERGE` 策略下非空：其它两种策略里每张票据只代表它自己那条消息。
    """

    waiters: deque[_Ticket[_T]] = field(default_factory=deque)
    pending: list[InboundMessage] = field(default_factory=list)
    holder: _Ticket[_T] | None = None
    running_turn: TurnId | None = None

    @property
    def busy(self) -> bool:
        """是否有人正持有槽位或正在排队。"""
        return self.holder is not None or bool(self.waiters)


class SessionScheduler(Generic[_T]):
    """按 `SessionKey` 串行化执行体。

    泛型参数是 `run` 的返回类型：编排层一次只会有一种 turn 结果类型，让它跟着调度器走，
    被合并的提交方就能拿到原类型的结果而不需要在调用点强转。

    非线程安全，只在事件循环内使用——`submit` 的判定与入队之间没有 `await`，这是「不加锁
    也不会漏判」的前提；从别的线程投递消息应当先把消息送进事件循环。
    """

    __slots__ = ("_policy", "_queue_max_size", "_slots")

    def __init__(
        self,
        *,
        policy: ConcurrencyPolicy = ConcurrencyPolicy.QUEUE,
        queue_max_size: int = DEFAULT_QUEUE_MAX_SIZE,
    ) -> None:
        """**异常约定**：`queue_max_size` 非正抛 `KERNEL_INVARIANT_VIOLATED`——上限为 0
        会让 `QUEUE` 静默退化成 `REJECT`。"""
        if queue_max_size <= 0:
            raise NucleaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "队列上限必须为正。",
                detail={"queue_max_size": queue_max_size},
            )
        self._policy = policy
        self._queue_max_size = queue_max_size
        self._slots: dict[SessionKey, SessionSlot[_T]] = {}

    @property
    def policy(self) -> ConcurrencyPolicy:
        return self._policy

    def active_sessions(self) -> tuple[SessionKey, ...]:
        """当前有槽位的 session。空闲槽位会被回收，因此这也是「谁在忙」。"""
        return tuple(self._slots)

    def running_turn(self, key: SessionKey) -> TurnId | None:
        """某个 session 正在跑的 turn；没有则 `None`。诊断用。"""
        slot = self._slots.get(key)
        return slot.running_turn if slot is not None else None

    def waiting(self, key: SessionKey) -> int:
        """某个 session 的等待数（不含正在跑的那一个）。"""
        slot = self._slots.get(key)
        return len(slot.waiters) if slot is not None else 0

    def submit(
        self,
        key: SessionKey,
        message: InboundMessage,
        run: Callable[[tuple[InboundMessage, ...]], Awaitable[_T]],
        *,
        turn_id: TurnId | None = None,
    ) -> Awaitable[SubmitOutcome[_T]]:
        """同步登记一条消息，返回它的异步处理结果。

        `run` 拿到的是**一批**消息：`QUEUE` / `REJECT` 下恒为一条，`MERGE` 下可能是多条，
        顺序即到达顺序。给它元组而不是单条，是为了让编排层的签名不随策略变化。

        登记阶段没有 `await`：调用顺序就是 FIFO 票据顺序。Channel 因此可以连续登记消息后
        再并发等待结果，无需在外层再建一套 per-session lane。

        **异常约定**：`run` 抛出的异常原样上抛给提交方（以及被合并进这一批的提交方），
        调度器只保证槽位一定被释放。拒绝不是异常，走 `SubmitOutcome.REJECTED`。
        """
        slot = self._slots.get(key)
        if slot is None:
            slot = SessionSlot[_T]()
            self._slots[key] = slot

        rejection = self._rejection_for(slot)
        if rejection is not None:
            self._discard_if_idle(key, slot)
            future: asyncio.Future[SubmitOutcome[_T]] = asyncio.get_running_loop().create_future()
            future.set_result(SubmitOutcome(status=SubmitStatus.REJECTED, error=rejection))
            return future

        ticket: _Ticket[_T] = _Ticket(message)
        slot.waiters.append(ticket)
        if self._policy is ConcurrencyPolicy.MERGE:
            slot.pending.append(message)

        if slot.holder is None and slot.waiters[0] is ticket:
            slot.waiters.popleft()
            slot.holder = ticket
            wait_handle = None
        else:
            wait_handle = ticket.wait_handle()

        return self._complete_submission(
            key,
            slot,
            ticket,
            run,
            turn_id=turn_id,
            wait_handle=wait_handle,
        )

    async def _complete_submission(
        self,
        key: SessionKey,
        slot: SessionSlot[_T],
        ticket: _Ticket[_T],
        run: Callable[[tuple[InboundMessage, ...]], Awaitable[_T]],
        *,
        turn_id: TurnId | None,
        wait_handle: asyncio.Future[_Merged[_T] | None] | None,
    ) -> SubmitOutcome[_T]:
        """等待票据取得槽位，并在调用方任务内执行这一批。"""
        if wait_handle is not None:
            try:
                merged = await wait_handle
            except BaseException:
                self._abandon(key, slot, ticket)
                raise
            if merged is not None:
                return SubmitOutcome(
                    status=SubmitStatus.MERGED, result=merged.result, batch=merged.batch
                )
            # 唤醒者已经把槽位交给了我们（`_hand_over` 在 resolve 之前设置 holder）。

        slot.running_turn = turn_id
        batch, absorbed = self._take_batch(slot, ticket)
        try:
            result = await run(batch)
        except BaseException as exc:
            self._settle_absorbed(absorbed, exc=exc)
            raise
        finally:
            slot.running_turn = None
            self._release(key, slot)
        self._settle_absorbed(absorbed, merged=_Merged(result=result, batch=batch))
        return SubmitOutcome(status=SubmitStatus.EXECUTED, result=result, batch=batch)

    # ------------------------------------------------------------------ 内部

    def _rejection_for(self, slot: SessionSlot[_T]) -> NucleaError | None:
        """按策略判断这条消息是否当场被拒。返回 `None` 表示可以入队。"""
        if self._policy is ConcurrencyPolicy.REJECT and slot.busy:
            return NucleaError(
                ErrorCode.INPUT_SESSION_BUSY,
                "该会话正在处理上一条消息，当前策略不排队，请稍后重试。",
                detail={"policy": self._policy.value},
            )
        # `MERGE` 不受队列上限约束：它的等待者不会各自跑一次，全部并成一批，
        # 队列长度不代表待执行的工作量。
        if self._policy is ConcurrencyPolicy.QUEUE and len(slot.waiters) >= self._queue_max_size:
            return NucleaError(
                ErrorCode.INPUT_SESSION_BUSY,
                "该会话的等待队列已满，请稍后重试。",
                detail={"policy": self._policy.value, "queue_max_size": self._queue_max_size},
            )
        return None

    def _take_batch(
        self, slot: SessionSlot[_T], ticket: _Ticket[_T]
    ) -> tuple[tuple[InboundMessage, ...], tuple[_Ticket[_T], ...]]:
        """确定这次执行的消息批次，以及被它吸收掉的票据。

        `MERGE` 下持有者把 `pending` 一次取空，此刻仍在 `waiters` 里的票据全部被吸收——
        它们的消息就在这一批里，再让它们各跑一次就等于没有合并。
        """
        if self._policy is not ConcurrencyPolicy.MERGE:
            return ((ticket.message,), ())
        batch = tuple(slot.pending)
        slot.pending.clear()
        absorbed = tuple(slot.waiters)
        slot.waiters.clear()
        return (batch, absorbed)

    def _settle_absorbed(
        self,
        absorbed: Sequence[_Ticket[_T]],
        *,
        merged: _Merged[_T] | None = None,
        exc: BaseException | None = None,
    ) -> None:
        """把结果或异常交给被吸收的票据。

        失败也要如实转达：它们的消息确实进了那一批，把失败说成「已合并、一切正常」会让
        用户以为消息被处理了。
        """
        for absorbed_ticket in absorbed:
            future = absorbed_ticket.future
            if future is None or future.done():
                continue
            if exc is not None:
                future.set_exception(exc)
            else:
                future.set_result(merged)

    def _release(self, key: SessionKey, slot: SessionSlot[_T]) -> None:
        """释放槽位并唤醒下一位。走 `finally`，因此 `run` 抛异常不会卡死 session。"""
        slot.holder = None
        if slot.waiters:
            self._hand_over(slot)
            return
        self._discard_if_idle(key, slot)

    def _abandon(
        self, key: SessionKey, slot: SessionSlot[_T], ticket: _Ticket[_T]
    ) -> None:
        """等待方被取消时移除票据，避免留下永远不会继续执行的 holder。"""
        if slot.holder is ticket:
            self._release(key, slot)
            return
        try:
            slot.waiters.remove(ticket)
        except ValueError:
            return  # 票据已被某个 MERGE 批次吸收，消息会随该批正常处理。
        if self._policy is ConcurrencyPolicy.MERGE:
            slot.pending = [item for item in slot.pending if item is not ticket.message]
        self._discard_if_idle(key, slot)

    def _hand_over(self, slot: SessionSlot[_T]) -> None:
        """把槽位交给队头。**先设置 holder 再 resolve**，否则被唤醒者与新到达的提交方
        会同时认为槽位空着。"""
        nxt = slot.waiters.popleft()
        slot.holder = nxt
        future = nxt.future
        if future is not None and not future.done():
            future.set_result(None)

    def _discard_if_idle(self, key: SessionKey, slot: SessionSlot[_T]) -> None:
        """回收空闲槽位，否则 `dict[SessionKey, SessionSlot]` 会随历史会话数无界增长。"""
        if not slot.busy and not slot.pending:
            self._slots.pop(key, None)
