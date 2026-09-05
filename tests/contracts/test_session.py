"""会话契约测试（`D03`，需求 §9.7、技术方案 §6.4）。

重点在两处一致性：`role=TOOL` 与 `tool_call_id` 必须同进同出，`TurnStatus` 与
`error` / `cancel_reason` 必须匹配——「状态说成功却带着错误」这种记录事后没人敢信。
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from karyvia.contracts import (
    AttachmentRef,
    AttachmentSource,
    CancelReason,
    Correlation,
    ErrorCode,
    InstanceId,
    KaryviaError,
    Role,
    SessionKey,
    SessionMessage,
    SessionSnapshot,
    TurnId,
    TurnOutcome,
    TurnStatus,
)
from karyvia.contracts.session import SESSION_SCHEMA_VERSION

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
CORRELATION = Correlation(InstanceId("default"), SessionKey("cli", "local"), TurnId("t-1"))


def message(**overrides: object) -> SessionMessage:
    base: dict[str, object] = {
        "message_id": "sm-1",
        "role": Role.USER,
        "content": "你好",
        "created_at": NOW,
    }
    base.update(overrides)
    return SessionMessage(**base)  # pyright: ignore[reportArgumentType]


def outcome(**overrides: object) -> TurnOutcome:
    base: dict[str, object] = {
        "correlation": CORRELATION,
        "status": TurnStatus.COMPLETED,
        "started_at": NOW,
        "finished_at": NOW + timedelta(seconds=3),
    }
    base.update(overrides)
    return TurnOutcome(**base)  # pyright: ignore[reportArgumentType]


def test_instances_are_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        message().content = "x"
    with pytest.raises(dataclasses.FrozenInstanceError):
        outcome().status = TurnStatus.FAILED


# ------------------------------------------------------------------ SessionMessage


def test_tool_call_id_only_on_tool_role() -> None:
    assert message(role=Role.TOOL, tool_call_id="c-1").tool_call_id == "c-1"
    with pytest.raises(KaryviaError) as exc:
        message(role=Role.ASSISTANT, tool_call_id="c-1")
    assert exc.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


def test_tool_role_requires_tool_call_id() -> None:
    with pytest.raises(KaryviaError) as exc:
        message(role=Role.TOOL)
    assert exc.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


def test_interrupted_defaults_to_false_and_is_recordable() -> None:
    """技术方案 §6.4 检查点 3：中断时已产生的文本要持久化并标记。"""
    assert message().interrupted is False
    assert message(interrupted=True).interrupted is True


def test_attachment_references_are_part_of_the_session_record() -> None:
    attachment = AttachmentRef(
        source=AttachmentSource.WORKSPACE,
        locator="reports/final.pdf",
        media_type="application/pdf",
        size_bytes=12,
        filename="final.pdf",
    )
    assert message(attachments=(attachment,)).attachments == (attachment,)


def test_naive_created_at_is_rejected() -> None:
    with pytest.raises(KaryviaError) as exc:
        message(created_at=datetime(2026, 8, 10))  # noqa: DTZ001
    assert exc.value.code is ErrorCode.INPUT_MALFORMED


# ------------------------------------------------------------------ SessionSnapshot


def test_snapshot_defaults_to_current_schema_version() -> None:
    assert SessionSnapshot(SessionKey("cli", "local")).schema_version == SESSION_SCHEMA_VERSION


def test_live_messages_skips_compacted_prefix() -> None:
    records = tuple(message(message_id=f"sm-{index}") for index in range(4))
    snapshot = SessionSnapshot(SessionKey("cli", "local"), records, compacted_through=2)
    assert snapshot.live_messages == records[2:]


@pytest.mark.parametrize("compacted_through", [-1, 3])
def test_compaction_watermark_must_stay_in_range(compacted_through: int) -> None:
    records = (message(),)
    with pytest.raises(KaryviaError) as exc:
        SessionSnapshot(SessionKey("cli", "local"), records, compacted_through=compacted_through)
    assert exc.value.code is ErrorCode.PERSISTENCE_RECORD_CORRUPT


@pytest.mark.parametrize("schema_version", [0, 1, SESSION_SCHEMA_VERSION + 1])
def test_snapshot_only_accepts_the_current_schema_version(schema_version: int) -> None:
    with pytest.raises(KaryviaError) as exc:
        SessionSnapshot(SessionKey("cli", "local"), schema_version=schema_version)
    assert exc.value.code is ErrorCode.PERSISTENCE_RECORD_CORRUPT


# ------------------------------------------------------------------ TurnOutcome


def test_turn_status_has_exactly_four_terminal_states() -> None:
    """技术方案 §6.4：终态只有四个，新增视为公开表面变化。"""
    assert {status.value for status in TurnStatus} == {
        "completed",
        "cancelled",
        "failed",
        "stopped_by_limit",
    }


def test_failed_turn_requires_error() -> None:
    with pytest.raises(KaryviaError) as exc:
        outcome(status=TurnStatus.FAILED)
    assert exc.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


def test_completed_turn_rejects_error() -> None:
    failure = KaryviaError(ErrorCode.KERNEL_UNEXPECTED, "boom")
    with pytest.raises(KaryviaError) as exc:
        outcome(error=failure)
    assert exc.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


def test_cancelled_turn_requires_reason() -> None:
    with pytest.raises(KaryviaError):
        outcome(status=TurnStatus.CANCELLED)
    assert outcome(
        status=TurnStatus.CANCELLED, cancel_reason=CancelReason.USER
    ).cancel_reason is CancelReason.USER


def test_non_cancelled_turn_rejects_reason() -> None:
    with pytest.raises(KaryviaError):
        outcome(cancel_reason=CancelReason.TIMEOUT)


def test_counters_and_clock_must_be_sane() -> None:
    with pytest.raises(KaryviaError):
        outcome(iterations=-1)
    with pytest.raises(KaryviaError):
        outcome(tool_calls=-1)
    with pytest.raises(KaryviaError):
        outcome(finished_at=NOW - timedelta(seconds=1))


@pytest.mark.parametrize(
    ("status", "complete"),
    [
        (TurnStatus.COMPLETED, True),
        (TurnStatus.STOPPED_BY_LIMIT, False),
    ],
)
def test_only_completed_is_a_complete_answer(status: TurnStatus, complete: bool) -> None:
    assert outcome(status=status).is_complete_answer is complete
