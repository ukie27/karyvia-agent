"""运行时事件契约测试（`D02`，需求 `OBS-001`–`OBS-003`）。"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from nucleamind.contracts import (
    Correlation,
    ErrorCategory,
    ErrorCode,
    EventFamily,
    EventName,
    NucleaError,
    RuntimeEvent,
    SessionKey,
)
from nucleamind.contracts.errors import MASK
from nucleamind.contracts.ids import InstanceId, TurnId

INSTANCE = InstanceId("default")
NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
CORRELATION = Correlation(
    instance_id=INSTANCE,
    session_key=SessionKey("cli", "local"),
    turn_id=TurnId("t-1"),
)


def _event(**overrides: object) -> RuntimeEvent:
    base: dict[str, object] = {
        "name": EventName.TURN_STARTED,
        "sequence": 1,
        "occurred_at": NOW,
        "instance_id": INSTANCE,
        "correlation": CORRELATION,
    }
    base.update(overrides)
    return RuntimeEvent(**base)  # pyright: ignore[reportArgumentType]


# ------------------------------------------------------------------ 事件名与事件族


def test_every_event_name_belongs_to_a_declared_family() -> None:
    for name in EventName:
        assert name.family in EventFamily
        assert name.value.startswith(f"{name.family.value}.")


def test_every_family_has_at_least_one_event() -> None:
    assert {name.family for name in EventName} == set(EventFamily)


def test_event_names_are_unique() -> None:
    values = [name.value for name in EventName]
    assert len(set(values)) == len(values)


#: `EventName` 的规范性快照（`NFR-104`）。以**字面量**写死：从实现反推的清单只能证明代码
#: 没改，证明不了它经过评审。增删事件名必须同时改这张表，那就是评审闸门。
#: `turn.stopped_by_limit` 由 `D12` 按 `NFR-104` 补入（`D09` 的 `TurnStoppedByLimit`
#: 原本没有落点，用 `turn.completed` 承载会让两种终态不可区分）。
#: Channel 泵可能在**进 orchestrator 之前**因总在途上界拒绝一条消息。它刻意不是
#: `turn.rejected`
#: ——那条消息从未进过 orchestrator，而 turn 事件只有那一个发布点。
#: `channel.delivery_failed` 由 `D43` 补入，用来消解 `Channel.deliver` 的 docstring 与
#: `EDG-204` 之间那条真实存在的矛盾（前者要求投递失败抛，后者要求 turn 仍走到终态，
#: 于是四个实现全都选了不抛）。它刻意不是 `turn.failed`：「答案没算出来」与「答案没送
#: 出去」的处置不同（重跑 vs 重发），也不是 `plugin.failed`：内建 CLI 的投递失败与插件无关。
EVENT_NAME_SNAPSHOT = (
    "instance.starting",
    "instance.ready",
    "instance.stopping",
    "instance.stopped",
    "instance.input_dropped",
    "plugin.discovered",
    "plugin.loaded",
    "plugin.load_failed",
    "plugin.activated",
    "plugin.deactivated",
    "plugin.failed",
    "capability.registered",
    "capability.shadowed",
    "capability.disabled",
    "capability.resolved",
    "session.started",
    "session.loaded",
    "session.compacted",
    "session.closed",
    "turn.started",
    "turn.rejected",
    "turn.completed",
    "turn.failed",
    "turn.cancelled",
    "turn.stopped_by_limit",
    "model.request_started",
    "model.response_received",
    "model.request_failed",
    "tool.call_started",
    "tool.call_completed",
    "tool.call_blocked",
    "tool.call_failed",
    "channel.delivery_failed",
)


def test_event_name_snapshot() -> None:
    assert tuple(name.value for name in EventName) == EVENT_NAME_SNAPSHOT


def test_every_turn_terminal_status_has_its_own_event_name() -> None:
    """四个终态在事件流里必须可区分（`EDG-304`）——`D09` 留下的缺口由 `D12` 补上。"""
    turn_names = {name.value for name in EventName if name.family is EventFamily.TURN}
    assert {
        "turn.completed",
        "turn.failed",
        "turn.cancelled",
        "turn.stopped_by_limit",
    } <= turn_names


def test_event_exposes_family() -> None:
    assert _event(name=EventName.TOOL_CALL_FAILED).family is EventFamily.TOOL


# ------------------------------------------------------------------ 不变量


def test_sequence_must_be_non_negative() -> None:
    with pytest.raises(NucleaError) as excinfo:
        _event(sequence=-1)
    assert excinfo.value.category is ErrorCategory.KERNEL_INTERNAL


def test_timestamp_must_be_timezone_aware() -> None:
    with pytest.raises(NucleaError) as excinfo:
        _event(occurred_at=datetime(2026, 8, 10, 12, 0))  # noqa: DTZ001
    assert excinfo.value.category is ErrorCategory.KERNEL_INTERNAL


def test_correlation_instance_must_match_event_instance() -> None:
    with pytest.raises(NucleaError) as excinfo:
        _event(instance_id=InstanceId("other"))
    assert excinfo.value.category is ErrorCategory.KERNEL_INTERNAL


def test_instance_level_events_may_omit_correlation() -> None:
    """实例启动阶段还没有会话与 turn，此时 correlation 必须允许为空（`OBS-001`）。"""
    event = _event(name=EventName.INSTANCE_STARTING, correlation=None)
    assert event.correlation is None
    assert event.instance_id == INSTANCE


def test_event_is_frozen() -> None:
    event = _event()
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.sequence = 2  # pyright: ignore[reportAttributeAccessIssue]


# ------------------------------------------------------------------ payload


def test_payload_defaults_to_empty_and_is_read_only() -> None:
    event = _event()
    assert dict(event.payload) == {}
    with pytest.raises(TypeError):
        event.payload["x"] = 1  # pyright: ignore[reportIndexIssue]


def test_payload_is_redacted_at_construction() -> None:
    """脱敏不依赖 sink：事件一旦存在就已经是安全的（`OBS-003`）。"""
    event = _event(payload={"api_key": "S3NT1NEL-value", "model": "claude-fable-5"})
    assert event.payload["api_key"] == MASK
    assert event.payload["model"] == "claude-fable-5"
    assert "S3NT1NEL-value" not in repr(event.payload)


def test_payload_snapshot_is_detached_from_caller() -> None:
    """调用方事后改自己的 dict 不能反向影响已发布的事件。"""
    payload: dict[str, object] = {"tokens": 10}
    event = _event(payload=payload)
    payload["tokens"] = 999
    assert event.payload["tokens"] == 10


def test_event_can_carry_an_error() -> None:
    error = NucleaError(ErrorCode.TIMEOUT_MODEL_REQUEST, "模型请求超时", retryable=True)
    event = _event(name=EventName.MODEL_REQUEST_FAILED, error=error)
    assert event.error is error
    assert event.family is EventFamily.MODEL
