"""事件总线：序号分配、订阅与带隔离的扇出。

职责：分配单调 `sequence`、构造已脱敏的 `RuntimeEvent`、把它扇给全部订阅者，并把
订阅者的异常与耗时隔离在扇出循环内。
不负责：认识任何具体消费者（内建 sink 在 `sinks.py`，也只是普通订阅者）、写文件、
决定事件名的语义、启动或关闭实例。

设计上最要紧的一条：`publish()` **同步、绝不抛出、绝不 await**，理由见 `EventBus`。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from ...contracts import Correlation, EventName, InstanceId, KaryviaError, RuntimeEvent
from .redaction import prepare_payload

if TYPE_CHECKING:  # pragma: no cover - 仅为注解。
    from ...contracts import JsonValue

__all__ = [
    "DEFAULT_MAX_STRIKES",
    "DEFAULT_RETIRED_HISTORY",
    "DEFAULT_SLOW_AFTER_MS",
    "EventBus",
    "Subscriber",
    "SubscriberHealth",
    "Subscription",
]

#: 单次投递超过这个耗时就记一次 strike。50 ms 对一个同步回调已经很宽松——真要做慢活的
#: 订阅者应当自己转交给队列或线程，那正是 bus 不 await 的前提。
DEFAULT_SLOW_AFTER_MS: Final = 50.0

#: 连续 strike 达到这个数就自动退订。健康投递把连续计数清零。
DEFAULT_MAX_STRIKES: Final = 5

#: 保留多少个已退订者的健康快照。有界，且只留快照不留 handler 引用。
DEFAULT_RETIRED_HISTORY: Final = 64

#: 订阅者签名。同步、无返回值；异步消费者在回调里把事件塞进自己的有界队列。
Subscriber = Callable[[RuntimeEvent], None]


@dataclass(frozen=True, slots=True)
class SubscriberHealth:
    """一个订阅者的投递统计快照。诊断据此回答「谁被摘掉了、为什么」。"""

    name: str
    delivered: int
    failures: int
    slow_deliveries: int
    consecutive_strikes: int
    detached: bool
    last_error: str | None

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "delivered": self.delivered,
            "failures": self.failures,
            "slow_deliveries": self.slow_deliveries,
            "consecutive_strikes": self.consecutive_strikes,
            "detached": self.detached,
            "last_error": self.last_error,
        }


class Subscription:
    """一次订阅的句柄：持有 handler、自己的健康计数，以及「是否已退订」这一位。

    投递逻辑放在这里而不是 bus 里，是因为它改的全是本对象的状态。bus 只负责决定
    「投给谁、按什么阈值」，把计数散到调用方就会出现只改一半的路径。
    """

    __slots__ = (
        "_consecutive_strikes",
        "_delivered",
        "_detached",
        "_failures",
        "_handler",
        "_last_error",
        "_name",
        "_slow_deliveries",
    )

    def __init__(self, handler: Subscriber, name: str) -> None:
        self._handler = handler
        self._name = name
        self._delivered = 0
        self._failures = 0
        self._slow_deliveries = 0
        self._consecutive_strikes = 0
        self._detached = False
        self._last_error: str | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def detached(self) -> bool:
        """是否已退订：主动 `cancel()`，或连续 strike 触发的熔断。"""
        return self._detached

    @property
    def health(self) -> SubscriberHealth:
        return SubscriberHealth(
            name=self._name,
            delivered=self._delivered,
            failures=self._failures,
            slow_deliveries=self._slow_deliveries,
            consecutive_strikes=self._consecutive_strikes,
            detached=self._detached,
            last_error=self._last_error,
        )

    def cancel(self) -> None:
        """退订。幂等；扇出过程中调用也安全——bus 遍历的是快照，且每次投递前重查。"""
        self._detached = True

    def deliver(
        self,
        event: RuntimeEvent,
        *,
        clock: Callable[[], float],
        slow_after_ms: float,
        max_strikes: int,
    ) -> None:
        """投递一条事件并更新健康计数。**绝不抛出。**

        捕 `Exception` 不捕 `BaseException`：`CancelledError` / `KeyboardInterrupt` 是
        进程级信号，不是订阅者的失败，吞掉它们会让 Ctrl-C 停不下来。
        """
        started = clock()
        try:
            self._handler(event)
        except Exception as exc:  # noqa: BLE001 - 隔离订阅者故障正是本方法的职责。
            self._failures += 1
            self._consecutive_strikes += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
        else:
            self._delivered += 1
            if (clock() - started) * 1000.0 > slow_after_ms:
                self._slow_deliveries += 1
                self._consecutive_strikes += 1
            else:
                self._consecutive_strikes = 0
        if self._consecutive_strikes >= max_strikes:
            self.cancel()


class EventBus:
    """单一事件总线。只做扇出，不认识任何具体消费者。

    **`publish()` 同步、绝不抛出、绝不 await。** 三条理由：

    1.  要求观察者故障不中断 turn。bus 一旦 await 订阅者，一个慢订阅者就直接
       拉长 turn，那条要求在时间维度上已经不成立。
    2. publish 会在没有事件循环的路径上被调用：`instance.starting` 在启动第 1 步、
       `karyvia config show`、绝大多数测试都不在 loop 里。要求 bus 有 loop 等于要求每条诊断
       路径先起一个 loop。
    3. asyncio 抢占不了同步回调——即便 bus 是 async 的，`wait_for` 对一个 CPU 阻塞的
       订阅者也无能为力。

    既然抢占不了，「超时隔离」就只能是**测量 + 熔断**：超过 `slow_after_ms` 记一次
    strike，抛异常也记一次，健康投递清零；连续 strike 达到 `max_strikes` 即自动退订。
    一次性掉线比永久拖慢每一个 turn 诚实，而 `health()` 让这件事查得到。

    线程语义：锁只保证**序号分配与订阅列表**的原子性。`publish()` 的扇出本身假设单线程
    调用（Kernel 是 asyncio 单线程），这样 `subscribe()` 可以安全地在别的线程发生。
    """

    __slots__ = (
        "_clock",
        "_dispatching",
        "_instance_id",
        "_lock",
        "_max_strikes",
        "_now",
        "_pending",
        "_retired",
        "_sequence",
        "_slow_after_ms",
        "_subscriptions",
    )

    def __init__(
        self,
        instance_id: InstanceId,
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
        slow_after_ms: float = DEFAULT_SLOW_AFTER_MS,
        max_strikes: int = DEFAULT_MAX_STRIKES,
        retired_history: int = DEFAULT_RETIRED_HISTORY,
    ) -> None:
        """`clock` 与 `now` 可注入：慢订阅者的测试不该靠 `sleep` 制造时序。"""
        self._instance_id = instance_id
        self._clock = clock
        self._now = now if now is not None else _utc_now
        self._slow_after_ms = slow_after_ms
        self._max_strikes = max_strikes
        self._sequence = 0
        self._subscriptions: list[Subscription] = []
        self._retired: deque[SubscriberHealth] = deque(maxlen=retired_history)
        self._pending: deque[RuntimeEvent] = deque()
        self._dispatching = False
        self._lock = threading.Lock()

    @property
    def instance_id(self) -> InstanceId:
        return self._instance_id

    @property
    def next_sequence(self) -> int:
        """下一条事件将拿到的序号。诊断与测试用，不参与判定。"""
        with self._lock:
            return self._sequence

    def subscribe(self, handler: Subscriber, *, name: str | None = None) -> Subscription:
        """登记一个同步订阅者，返回可 `cancel()` 的句柄。

        `name` 只用于诊断展示；缺省取 handler 的限定名，取不到就用类型名——sink 是可调用
        对象而不是函数，`__qualname__` 未必存在。
        """
        label = name or getattr(handler, "__qualname__", None) or type(handler).__name__
        subscription = Subscription(handler, label)
        with self._lock:
            self._subscriptions.append(subscription)
        return subscription

    def subscribers(self) -> tuple[Subscription, ...]:
        """当前仍在册的订阅（不含已退订的）。"""
        with self._lock:
            return tuple(sub for sub in self._subscriptions if not sub.detached)

    def health(self) -> tuple[SubscriberHealth, ...]:
        """在册订阅 + 最近若干个已退订者的健康快照。

        退订者也报，是因为「事件没到 WebUI」最常见的原因就是它被熔断摘掉了，而摘掉的
        那一刻没人在看。快照有界（`retired_history`）且不持有 handler 引用。
        """
        with self._lock:
            active = tuple(sub.health for sub in self._subscriptions)
            retired = tuple(self._retired)
        return retired + active

    def publish(
        self,
        name: EventName,
        *,
        correlation: Correlation | None = None,
        payload: Mapping[str, object] | None = None,
        error: KaryviaError | None = None,
        occurred_at: datetime | None = None,
    ) -> RuntimeEvent:
        """构造并扇出一条事件，返回它。绝不抛出订阅者的异常。

        事件在**构造前**就已脱敏（`prepare_payload`），因此任何 sink 拿到的都是安全值；
        新增一个 sink 不会重新引入泄漏面。
        """
        event = RuntimeEvent(
            name=name,
            sequence=self._allocate(),
            occurred_at=occurred_at if occurred_at is not None else self._now(),
            instance_id=self._instance_id,
            correlation=correlation,
            payload=prepare_payload(payload or {}),
            error=error,
        )
        self._enqueue(event)
        return event

    def _allocate(self) -> int:
        with self._lock:
            sequence = self._sequence
            self._sequence = sequence + 1
        return sequence

    def _enqueue(self, event: RuntimeEvent) -> None:
        """扇出，或在重入时排队。

        订阅者在回调里再 `publish()` 是合法的（sink 记录自身失败、诊断插件派生事件）。
        朴素实现会递归扇出：深度不可控，投递顺序还会变成后序遍历。这里让最外层那次扇出
        在自己结束后按序 flush——序号仍严格单调，投递顺序 == 发布顺序。
        """
        self._pending.append(event)
        if self._dispatching:
            return
        self._dispatching = True
        try:
            while self._pending:
                self._dispatch(self._pending.popleft())
        finally:
            self._dispatching = False
            self._pending.clear()
            self._retire_detached()

    def _dispatch(self, event: RuntimeEvent) -> None:
        for subscription in self.subscribers():
            if subscription.detached:  # 上一位订阅者可能刚把它取消掉。
                continue
            subscription.deliver(
                event,
                clock=self._clock,
                slow_after_ms=self._slow_after_ms,
                max_strikes=self._max_strikes,
            )

    def _retire_detached(self) -> None:
        """把已退订者移出在册列表，只留健康快照。

        留快照是为了诊断，丢对象是为了不泄漏 handler 引用——每个 WebUI 连接订阅一次、
        断开时退订，长期运行下积累的就是这些回调闭包。
        """
        with self._lock:
            if not any(sub.detached for sub in self._subscriptions):
                return
            remaining: list[Subscription] = []
            for sub in self._subscriptions:
                if sub.detached:
                    self._retired.append(sub.health)
                else:
                    remaining.append(sub)
            self._subscriptions = remaining


def _utc_now() -> datetime:
    return datetime.now(UTC)
