"""`kernel/observability/bus.py` 的行为测试。

四类验收点：序号单调且可按序重放、订阅者异常与「超时」被隔离且不影响 publish 返回、
连续 strike 触发熔断退订、重入 publish 不递归。

慢订阅者用**注入的假时钟**制造，不用 `sleep`：真实时序会让这条断言在慢机器上变成随机
失败，而它要证明的是判定逻辑，不是机器有多快。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Final

from karyvia.contracts import (
    Correlation,
    ErrorCode,
    EventName,
    InstanceId,
    KaryviaError,
    RuntimeEvent,
    SecretStr,
    SessionKey,
    TurnId,
)
from karyvia.contracts.errors import MASK
from karyvia.kernel.observability import DEFAULT_MAX_STRIKES, EventBus

INSTANCE: Final = InstanceId("inst-1")
CORRELATION: Final = Correlation(
    instance_id=INSTANCE,
    session_key=SessionKey("cli", "local"),
    turn_id=TurnId("turn-1"),
)
SENTINEL: Final = "karyvia-sentinel-1c77e3aa-do-not-leak"


class FakeClock:
    """可手动推进的单调时钟。`advance` 决定下一次读数比上一次多多少毫秒。"""

    def __init__(self) -> None:
        self.value = 0.0
        self.advance_ms = 0.0

    def __call__(self) -> float:
        now = self.value
        self.value += self.advance_ms / 1000.0
        return now


def _bus(**kwargs: object) -> EventBus:
    base: dict[str, object] = {"now": lambda: datetime(2026, 8, 11, tzinfo=UTC)}
    base.update(kwargs)
    return EventBus(INSTANCE, **base)  # pyright: ignore[reportArgumentType]


# ------------------------------------------------------------------ 序号与重放


def test_sequence_starts_at_zero_and_is_monotonic() -> None:
    bus = _bus()
    assert bus.next_sequence == 0
    events = [bus.publish(EventName.TURN_STARTED, correlation=CORRELATION) for _ in range(5)]
    assert [event.sequence for event in events] == [0, 1, 2, 3, 4]
    assert bus.next_sequence == 5


def test_delivery_order_equals_publication_order() -> None:
    bus = _bus()
    seen: list[int] = []
    bus.subscribe(lambda event: seen.append(event.sequence))
    for name in (EventName.TURN_STARTED, EventName.MODEL_REQUEST_STARTED, EventName.TURN_COMPLETED):
        bus.publish(name, correlation=CORRELATION)
    assert seen == [0, 1, 2]


def test_event_carries_instance_and_correlation() -> None:
    bus = _bus()
    event = bus.publish(EventName.TURN_STARTED, correlation=CORRELATION)
    assert event.instance_id == INSTANCE
    assert event.correlation is CORRELATION
    assert event.occurred_at.tzinfo is not None


def test_explicit_occurred_at_wins() -> None:
    stamp = datetime(2030, 1, 2, 3, 4, tzinfo=UTC)
    assert _bus().publish(EventName.INSTANCE_READY, occurred_at=stamp).occurred_at == stamp


# ------------------------------------------------------------------ 脱敏


def test_payload_is_redacted_before_the_event_exists() -> None:
    bus = _bus()
    delivered: list[RuntimeEvent] = []
    bus.subscribe(delivered.append)
    event = bus.publish(
        EventName.MODEL_REQUEST_STARTED,
        correlation=CORRELATION,
        payload={"api_key": SecretStr(SENTINEL), "model": "gpt-4o"},
    )
    assert event.payload == {"api_key": MASK, "model": "gpt-4o"}
    assert delivered[0].payload["api_key"] == MASK


# ------------------------------------------------------------------ 隔离


def test_a_raising_subscriber_does_not_affect_others_or_publish() -> None:
    bus = _bus()
    seen: list[int] = []

    def boom(event: RuntimeEvent) -> None:
        raise RuntimeError("订阅者炸了")

    failing = bus.subscribe(boom, name="boom")
    bus.subscribe(lambda event: seen.append(event.sequence), name="good")

    event = bus.publish(EventName.TURN_STARTED, correlation=CORRELATION)

    assert event.sequence == 0
    assert seen == [0]
    assert failing.health.failures == 1
    assert failing.health.last_error is not None
    assert "RuntimeError" in failing.health.last_error


def test_consecutive_failures_detach_the_subscriber() -> None:
    bus = _bus()

    def boom(event: RuntimeEvent) -> None:
        raise ValueError("坏了")

    subscription = bus.subscribe(boom, name="boom")
    for _ in range(DEFAULT_MAX_STRIKES):
        bus.publish(EventName.TURN_STARTED, correlation=CORRELATION)
    assert subscription.detached

    bus.publish(EventName.TURN_COMPLETED, correlation=CORRELATION)
    assert subscription.health.failures == DEFAULT_MAX_STRIKES  # 摘掉之后不再投递。


def test_a_healthy_delivery_resets_the_strike_counter() -> None:
    """偶尔打嗝的订阅者不该被摘掉——熔断针对的是持续故障。"""
    bus = _bus()
    calls = {"n": 0}

    def flaky(event: RuntimeEvent) -> None:
        calls["n"] += 1
        if calls["n"] % 2 == 1:
            raise RuntimeError("间歇失败")

    subscription = bus.subscribe(flaky, name="flaky")
    for _ in range(DEFAULT_MAX_STRIKES * 3):
        bus.publish(EventName.TURN_STARTED, correlation=CORRELATION)
    assert not subscription.detached
    assert subscription.health.failures > 0


def test_a_slow_subscriber_is_counted_and_eventually_detached() -> None:
    clock = FakeClock()
    bus = _bus(clock=clock, slow_after_ms=50.0)
    subscription = bus.subscribe(lambda event: None, name="slow")

    clock.advance_ms = 500.0
    for _ in range(DEFAULT_MAX_STRIKES):
        bus.publish(EventName.TURN_STARTED, correlation=CORRELATION)

    assert subscription.health.slow_deliveries == DEFAULT_MAX_STRIKES
    assert subscription.health.failures == 0
    assert subscription.detached


def test_a_fast_subscriber_is_never_slow() -> None:
    clock = FakeClock()
    bus = _bus(clock=clock, slow_after_ms=50.0)
    subscription = bus.subscribe(lambda event: None, name="fast")
    clock.advance_ms = 1.0
    for _ in range(20):
        bus.publish(EventName.TURN_STARTED, correlation=CORRELATION)
    assert subscription.health.slow_deliveries == 0
    assert subscription.health.delivered == 20


def test_base_exceptions_are_not_swallowed() -> None:
    """`CancelledError` / `KeyboardInterrupt` 是进程级信号，吞掉会让 Ctrl-C 停不下来。"""
    bus = _bus()

    def interrupt(event: RuntimeEvent) -> None:
        raise KeyboardInterrupt

    bus.subscribe(interrupt, name="interrupt")
    try:
        bus.publish(EventName.TURN_STARTED, correlation=CORRELATION)
    except KeyboardInterrupt:
        return
    raise AssertionError("KeyboardInterrupt 必须穿透 bus 的隔离层")


# ------------------------------------------------------------------ 订阅生命周期


def test_cancel_stops_delivery_and_keeps_the_health_snapshot() -> None:
    bus = _bus()
    seen: list[int] = []
    subscription = bus.subscribe(lambda event: seen.append(event.sequence), name="ui")

    bus.publish(EventName.TURN_STARTED, correlation=CORRELATION)
    subscription.cancel()
    bus.publish(EventName.TURN_COMPLETED, correlation=CORRELATION)

    assert seen == [0]
    assert bus.subscribers() == ()
    names = [health.name for health in bus.health()]
    assert names == ["ui"]
    assert bus.health()[0].detached is True


def test_cancel_is_idempotent() -> None:
    bus = _bus()
    subscription = bus.subscribe(lambda event: None)
    subscription.cancel()
    subscription.cancel()
    bus.publish(EventName.TURN_STARTED, correlation=CORRELATION)
    assert bus.subscribers() == ()


def test_retired_history_is_bounded() -> None:
    bus = _bus(retired_history=2)
    for index in range(5):
        subscription = bus.subscribe(lambda event: None, name=f"s{index}")
        bus.publish(EventName.TURN_STARTED, correlation=CORRELATION)
        subscription.cancel()
    bus.publish(EventName.TURN_STARTED, correlation=CORRELATION)
    assert len(bus.health()) == 2


def test_subscriber_name_defaults_to_something_useful() -> None:
    bus = _bus()

    class Sink:
        def __call__(self, event: RuntimeEvent) -> None:
            return None

    assert bus.subscribe(Sink()).name == "Sink"

    def named_handler(event: RuntimeEvent) -> None:
        return None

    assert "named_handler" in bus.subscribe(named_handler).name


# ------------------------------------------------------------------ 重入


def test_publishing_from_inside_a_subscriber_does_not_recurse() -> None:
    """重入的事件排队等外层扇出结束，序号仍单调、投递顺序 == 发布顺序。"""
    bus = _bus()
    seen: list[int] = []
    depth = {"max": 0, "current": 0}

    def echo(event: RuntimeEvent) -> None:
        depth["current"] += 1
        depth["max"] = max(depth["max"], depth["current"])
        seen.append(event.sequence)
        if event.name is EventName.TURN_STARTED:
            bus.publish(EventName.SESSION_STARTED, correlation=CORRELATION)
        depth["current"] -= 1

    bus.subscribe(echo)
    bus.publish(EventName.TURN_STARTED, correlation=CORRELATION)

    assert seen == [0, 1]
    assert depth["max"] == 1


def test_a_subscriber_may_cancel_another_mid_dispatch() -> None:
    """扇出遍历的是快照，因此每次投递前要重查 `detached`，否则会投给刚被摘掉的订阅者。"""
    bus = _bus()
    seen: list[str] = []

    def first(event: RuntimeEvent) -> None:
        seen.append("first")
        second.cancel()

    bus.subscribe(first, name="first")
    second = bus.subscribe(lambda event: seen.append("second"), name="second")

    bus.publish(EventName.TURN_STARTED, correlation=CORRELATION)
    assert seen == ["first"]


def test_errors_ride_along_with_the_event() -> None:
    bus = _bus()
    error = KaryviaError(ErrorCode.EXTERNAL_MODEL_PROVIDER, "上游挂了。")
    event = bus.publish(
        EventName.MODEL_REQUEST_FAILED, correlation=CORRELATION, error=error
    )
    assert event.error is error


def test_bus_exposes_its_instance_id() -> None:
    assert _bus().instance_id == INSTANCE


def test_subscriber_health_serializes_to_json() -> None:
    bus = _bus()
    subscription = bus.subscribe(lambda event: None, name="ring")
    bus.publish(EventName.TURN_STARTED, correlation=CORRELATION)
    payload = subscription.health.to_json()
    assert payload == {
        "name": "ring",
        "delivered": 1,
        "failures": 0,
        "slow_deliveries": 0,
        "consecutive_strikes": 0,
        "detached": False,
        "last_error": None,
    }
    assert json.loads(json.dumps(payload)) == payload
