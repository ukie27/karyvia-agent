"""Turn 压缩结果到 Session 持久化水位的映射。"""

from __future__ import annotations

from datetime import UTC, datetime

from karyvia.contracts import ModelMessage, Role, SessionKey, SessionMessage, SessionSnapshot
from karyvia.kernel.turn.compaction import SessionCompactionTracker
from karyvia.kernel.turn.context_builder import replay_history

NOW = datetime(2026, 8, 18, tzinfo=UTC)
KEY = SessionKey("cli", "local")


def record(
    role: Role, content: str, index: int, *, tool_call_id: str | None = None
) -> SessionMessage:
    return SessionMessage(
        message_id=f"m{index}",
        role=role,
        content=content,
        created_at=NOW,
        tool_call_id=tool_call_id,
    )


def snapshot(*messages: SessionMessage, compacted_through: int = 0) -> SessionSnapshot:
    return SessionSnapshot(KEY, messages, compacted_through=compacted_through)


def summary(content: str = "摘要") -> ModelMessage:
    return ModelMessage(Role.USER, content)


def test_session_prefix_maps_back_to_absolute_record_watermark() -> None:
    snap = snapshot(
        record(Role.USER, "问题", 1),
        record(Role.TOOL, "工具结果", 2, tool_call_id="call-1"),
        record(Role.ASSISTANT, "回答", 3),
    )
    history = replay_history(snap)
    tracker = SessionCompactionTracker(history)

    tracker.record((history[0].message,), summary())

    assert tracker.pending is not None
    assert tracker.pending.through == 1


def test_transient_context_is_not_mistaken_for_session_history() -> None:
    snap = snapshot(record(Role.USER, "旧问题", 1))
    tracker = SessionCompactionTracker(replay_history(snap))

    tracker.record((ModelMessage(Role.USER, "临时上下文"),), summary())

    assert tracker.pending is None

def test_equal_but_rebuilt_message_has_no_session_provenance() -> None:
    snap = snapshot(record(Role.USER, "同样的正文", 1))
    tracker = SessionCompactionTracker(replay_history(snap))

    tracker.record((ModelMessage(Role.USER, "同样的正文"),), summary())

    assert tracker.pending is None


def test_a_mixed_summary_is_not_persisted() -> None:
    snap = snapshot(record(Role.USER, "旧问题", 1))
    tracker = SessionCompactionTracker(replay_history(snap))

    tracker.record(
        (
            ModelMessage(Role.USER, "旧问题"),
            ModelMessage(Role.USER, "临时上下文"),
        ),
        summary(),
    )

    assert tracker.pending is None
