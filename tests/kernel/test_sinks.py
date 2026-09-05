"""`kernel/observability/sinks.py` 的行为测试（`D12`：`NFR-404`、`OBS-002`、`OBS-003`、`EDG-501`）。

四类验收点：内存环的容量上限与 `dropped` 计数、JSONL 按天分片与写失败被计数而不是抛出、
哨兵扫描（埋入密钥后扫描两个 sink 的全部输出）、配置错误落盘。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import pytest

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
from karyvia.kernel.observability import (
    DEFAULT_RING_CAPACITY,
    EventBus,
    JsonlFileSink,
    MemoryRingSink,
    write_config_error,
)

INSTANCE: Final = InstanceId("inst-1")
SENTINEL: Final = "karyvia-sentinel-6b30fe95-do-not-leak"


def _correlation(turn: str = "turn-1") -> Correlation:
    return Correlation(
        instance_id=INSTANCE,
        session_key=SessionKey("cli", "local"),
        turn_id=TurnId(turn),
    )


def _event(sequence: int, *, turn: str = "turn-1", day: int = 11) -> RuntimeEvent:
    return RuntimeEvent(
        name=EventName.TURN_STARTED,
        sequence=sequence,
        occurred_at=datetime(2026, 8, day, tzinfo=UTC),
        instance_id=INSTANCE,
        correlation=_correlation(turn),
    )


# ------------------------------------------------------------------ 内存环（NFR-404）


def test_ring_default_capacity_is_bounded() -> None:
    assert MemoryRingSink().capacity == DEFAULT_RING_CAPACITY


def test_ring_drops_the_oldest_and_counts_it() -> None:
    ring = MemoryRingSink(capacity=3)
    for sequence in range(5):
        ring(_event(sequence))
    assert [event.sequence for event in ring.events()] == [2, 3, 4]
    assert ring.dropped == 2
    assert len(ring) == 3


def test_ring_never_grows_past_capacity() -> None:
    ring = MemoryRingSink(capacity=10)
    for sequence in range(1000):
        ring(_event(sequence))
    assert len(ring) == 10
    assert ring.dropped == 990


def test_ring_rejects_non_positive_capacity() -> None:
    with pytest.raises(ValueError):
        MemoryRingSink(capacity=0)


def test_ring_returns_events_sorted_by_sequence() -> None:
    ring = MemoryRingSink(capacity=8)
    for sequence in (3, 1, 2):
        ring(_event(sequence))
    assert [event.sequence for event in ring.events()] == [1, 2, 3]


def test_ring_filters_by_turn() -> None:
    ring = MemoryRingSink(capacity=8)
    ring(_event(0, turn="turn-a"))
    ring(_event(1, turn="turn-b"))
    ring(_event(2, turn="turn-a"))
    assert [event.sequence for event in ring.by_turn(TurnId("turn-a"))] == [0, 2]


def test_ring_ignores_events_without_correlation() -> None:
    ring = MemoryRingSink(capacity=4)
    ring(
        RuntimeEvent(
            name=EventName.INSTANCE_READY,
            sequence=0,
            occurred_at=datetime(2026, 8, 11, tzinfo=UTC),
            instance_id=INSTANCE,
        )
    )
    assert ring.by_turn(TurnId("turn-1")) == ()


def test_ring_clear_resets_everything() -> None:
    ring = MemoryRingSink(capacity=2)
    for sequence in range(5):
        ring(_event(sequence))
    ring.clear()
    assert len(ring) == 0
    assert ring.dropped == 0


# ------------------------------------------------------------------ JSONL sink


def _sink(root: Path) -> JsonlFileSink:
    return JsonlFileSink(lambda day: root / "logs" / f"events-{day.isoformat()}.jsonl")


def _bus() -> EventBus:
    """时钟注入固定值：JSONL 按 `occurred_at` 的日期分片，用真实时钟会让断言随日期漂移。"""
    return EventBus(INSTANCE, now=lambda: datetime(2026, 8, 11, tzinfo=UTC))


def test_jsonl_writes_one_json_object_per_line(tmp_path: Path) -> None:
    sink = _sink(tmp_path)
    sink(_event(0))
    sink(_event(1))
    sink.close()

    path = tmp_path / "logs" / "events-2026-08-11.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["sequence"] for line in lines] == [0, 1]
    assert sink.written == 2
    assert sink.write_failures == 0


def test_jsonl_rolls_over_at_midnight(tmp_path: Path) -> None:
    sink = _sink(tmp_path)
    sink(_event(0, day=11))
    sink(_event(1, day=12))
    sink.close()

    logs = sorted(path.name for path in (tmp_path / "logs").iterdir())
    assert logs == ["events-2026-08-11.jsonl", "events-2026-08-12.jsonl"]


def test_jsonl_reopens_after_close(tmp_path: Path) -> None:
    sink = _sink(tmp_path)
    sink(_event(0))
    assert sink.current_path is not None
    sink.close()
    assert sink.current_path is None
    sink(_event(1))
    sink.close()
    path = tmp_path / "logs" / "events-2026-08-11.jsonl"
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_jsonl_write_failures_are_counted_not_raised(tmp_path: Path) -> None:
    """sink 抛出只会被 bus 的隔离层吞掉；计数至少查得到。"""

    def explode(day: object) -> Path:
        raise OSError("磁盘满了")

    sink = JsonlFileSink(explode)  # pyright: ignore[reportArgumentType]
    sink(_event(0))
    assert sink.written == 0
    assert sink.write_failures == 1
    assert sink.last_error is not None
    assert "OSError" in sink.last_error


def test_a_failing_jsonl_sink_does_not_break_the_bus(tmp_path: Path) -> None:
    def explode(day: object) -> Path:
        raise OSError("磁盘满了")

    bus = _bus()
    ring = MemoryRingSink(capacity=4)
    bus.subscribe(JsonlFileSink(explode), name="jsonl")  # pyright: ignore[reportArgumentType]
    bus.subscribe(ring, name="ring")
    bus.publish(EventName.TURN_STARTED, correlation=_correlation())
    assert len(ring) == 1


# ------------------------------------------------------------------ 哨兵扫描（OBS-003）


def test_no_sentinel_reaches_any_sink(tmp_path: Path) -> None:
    """埋入密钥后扫描 JSONL 全文与内存环的全部渲染形式，哨兵不得出现。"""
    bus = _bus()
    ring = MemoryRingSink(capacity=16)
    jsonl = _sink(tmp_path)
    bus.subscribe(ring, name="ring")
    bus.subscribe(jsonl, name="jsonl")

    bus.publish(
        EventName.MODEL_REQUEST_STARTED,
        correlation=_correlation(),
        payload={
            "api_key": SENTINEL,
            "secret": SecretStr(SENTINEL),
            "nested": {"headers": {"authorization": f"Bearer {SENTINEL}"}},
            "items": [SecretStr(SENTINEL), "ok"],
        },
    )
    bus.publish(
        EventName.MODEL_REQUEST_FAILED,
        correlation=_correlation(),
        error=KaryviaError(
            ErrorCode.EXTERNAL_MODEL_PROVIDER,
            f"调用失败，用的是 {SENTINEL}",
            detail={"api_key": SENTINEL},
        ),
    )
    jsonl.close()

    text = (tmp_path / "logs" / "events-2026-08-11.jsonl").read_text(encoding="utf-8")
    assert SENTINEL not in text
    for event in ring.events():
        assert SENTINEL not in repr(event)
        assert SENTINEL not in json.dumps(dict(event.payload), ensure_ascii=False)
        if event.error is not None:
            assert SENTINEL not in repr(event.error)
            assert SENTINEL not in event.error.user_message


# ------------------------------------------------------------------ 配置错误落盘（EDG-501）


def test_config_error_is_appended_as_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "config-errors-2026-08-11.jsonl"
    error = KaryviaError(
        ErrorCode.CONFIG_INVALID, "未知字段 modl。", detail={"pointer": "/model/modl"}
    )
    stamp = datetime(2026, 8, 11, 9, tzinfo=UTC)

    assert write_config_error(path, error, occurred_at=stamp) is True
    assert write_config_error(path, error, occurred_at=stamp + timedelta(seconds=1)) is True

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    record = json.loads(lines[0])
    assert record["kind"] == "config_error"
    assert record["occurred_at"] == "2026-08-11T09:00:00+00:00"
    assert record["error"]["code"] == ErrorCode.CONFIG_INVALID.value
    assert record["error"]["detail"] == {"pointer": "/model/modl"}


def test_config_error_does_not_leak_secrets(tmp_path: Path) -> None:
    path = tmp_path / "config-errors.jsonl"
    error = KaryviaError(
        ErrorCode.CONFIG_SECRET_MISSING,
        f"解析失败：{SENTINEL}",
        detail={"api_key": SENTINEL},
    )
    assert write_config_error(path, error) is True
    assert SENTINEL not in path.read_text(encoding="utf-8")


def test_config_error_write_failure_returns_false(tmp_path: Path) -> None:
    """在一条已经失败的启动路径上再抛一次，只会把真正的原因盖掉。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("不是目录", encoding="utf-8")
    error = KaryviaError(ErrorCode.CONFIG_INVALID, "坏了。")
    assert write_config_error(blocker / "nested" / "errors.jsonl", error) is False
