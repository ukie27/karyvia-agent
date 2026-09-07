"""消息契约测试。

重点是 §10.2 的三条校验规则与 `OutboundMessage` 的寻址自洽——后者是「Channel 不查缓存
即可投递」这个承诺唯一的保障。
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from karyvia.contracts import (
    AttachmentRef,
    AttachmentSource,
    ErrorCode,
    InboundMessage,
    InstanceId,
    KaryviaError,
    OutboundMessage,
    Sender,
    SessionKey,
    StreamState,
    TurnId,
)
from karyvia.contracts.message import MAX_ATTACHMENTS, MAX_CONTENT_LENGTH

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
IMAGE = AttachmentRef(AttachmentSource.OPAQUE, "file-123", "image/png")


def inbound(**overrides: object) -> InboundMessage:
    base: dict[str, object] = {
        "message_id": "m-1",
        "instance_id": InstanceId("default"),
        "channel_id": "cli",
        "conversation_id": "local",
        "sender": Sender("u-1"),
        "content": "你好",
        "timestamp": NOW,
    }
    base.update(overrides)
    return InboundMessage(**base)  # pyright: ignore[reportArgumentType]


def outbound(**overrides: object) -> OutboundMessage:
    base: dict[str, object] = {
        "session_key": SessionKey("cli", "local"),
        "channel_id": "cli",
        "conversation_id": "local",
        "turn_id": TurnId("t-1"),
        "content": "回复",
    }
    base.update(overrides)
    return OutboundMessage(**base)  # pyright: ignore[reportArgumentType]


# ------------------------------------------------------------------ 不可变性


@pytest.mark.parametrize(
    ("label", "instance", "field"),
    [
        ("inbound", inbound(), "content"),
        ("outbound", outbound(), "content"),
        ("sender", Sender("u-1"), "user_id"),
        ("attachment", IMAGE, "locator"),
    ],
)
def test_instances_are_frozen(label: str, instance: object, field: str) -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(instance, field, "x")


# ------------------------------------------------------------------ §10.2 校验规则


def test_content_and_attachments_cannot_both_be_empty() -> None:
    with pytest.raises(KaryviaError) as exc:
        inbound(content="")
    assert exc.value.code is ErrorCode.INPUT_MALFORMED


def test_attachment_alone_is_enough() -> None:
    assert inbound(content="", attachments=(IMAGE,)).attachments == (IMAGE,)


@pytest.mark.parametrize(
    "locator",
    ["/etc/passwd", "C:\\Users\\me\\secret.txt", "C:/Users/me/secret.txt", "\\\\host\\share\\x"],
)
def test_workspace_attachment_rejects_absolute_paths(locator: str) -> None:
    """§10.2：附件不能只依赖未经授权的本地绝对路径。"""
    with pytest.raises(KaryviaError) as exc:
        AttachmentRef(AttachmentSource.WORKSPACE, locator, "text/plain")
    assert exc.value.code is ErrorCode.PERMISSION_PATH_OUTSIDE_WORKSPACE


@pytest.mark.parametrize("locator", ["../../etc/passwd", "docs/../../x", "a\\..\\b"])
def test_workspace_attachment_rejects_parent_segments(locator: str) -> None:
    with pytest.raises(KaryviaError) as exc:
        AttachmentRef(AttachmentSource.WORKSPACE, locator, "text/plain")
    assert exc.value.code is ErrorCode.PERMISSION_PATH_OUTSIDE_WORKSPACE


def test_workspace_attachment_accepts_relative_path() -> None:
    assert AttachmentRef(AttachmentSource.WORKSPACE, "notes/a.md", "text/markdown").locator


@pytest.mark.parametrize("locator", ["ftp://x/y", "file:///etc/passwd", "/tmp/x"])
def test_url_attachment_must_be_http(locator: str) -> None:
    with pytest.raises(KaryviaError) as exc:
        AttachmentRef(AttachmentSource.URL, locator, "image/png")
    assert exc.value.code is ErrorCode.INPUT_MALFORMED


@pytest.mark.parametrize("media_type", ["png", "image/", "/png", "image png", ""])
def test_media_type_must_be_mime_shaped(media_type: str) -> None:
    with pytest.raises(KaryviaError) as exc:
        AttachmentRef(AttachmentSource.OPAQUE, "f-1", media_type)
    assert exc.value.code is ErrorCode.INPUT_UNSUPPORTED_MEDIA


def test_metadata_rejects_sdk_objects() -> None:
    """原始 SDK 对象不得越过 Channel 边界。"""
    with pytest.raises(KaryviaError):
        inbound(metadata={"telegram": object()})


def test_metadata_is_frozen_snapshot() -> None:
    source = {"telegram": {"chat_id": 1}}
    message = inbound(metadata=source)
    source["telegram"] = {"chat_id": 2}
    assert message.metadata["telegram"] == {"chat_id": 1}


  # ------------------------------------------------------------------  大小



def test_oversized_text_is_rejected() -> None:
    with pytest.raises(KaryviaError) as exc:
        inbound(content="x" * (MAX_CONTENT_LENGTH + 1))
    assert exc.value.code is ErrorCode.INPUT_TOO_LARGE


def test_too_many_attachments_is_rejected() -> None:
    with pytest.raises(KaryviaError) as exc:
        inbound(attachments=tuple(IMAGE for _ in range(MAX_ATTACHMENTS + 1)))
    assert exc.value.code is ErrorCode.INPUT_TOO_LARGE


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(KaryviaError) as exc:
        inbound(timestamp=datetime(2026, 8, 10, 12, 0))  # noqa: DTZ001
    assert exc.value.code is ErrorCode.INPUT_MALFORMED


@pytest.mark.parametrize("field", ["message_id", "channel_id", "conversation_id"])
def test_identifiers_must_be_non_empty(field: str) -> None:
    with pytest.raises(KaryviaError) as exc:
        inbound(**{field: ""})
    assert exc.value.code is ErrorCode.INPUT_MALFORMED


def test_session_key_requires_explicit_scope() -> None:
    """scope 是路由决策，不在消息里；默认值必须与 `SessionKey` 的默认一致。"""
    assert inbound().session_key() == SessionKey("cli", "local", "default")
    assert inbound().session_key("proj-a").scope == "proj-a"


  # ------------------------------------------------------------------ §10.3 /



def test_outbound_addressing_must_match_session_key() -> None:
    """寻址字段与 session_key 打架时必须失败，否则投递会静默走错目标。"""
    with pytest.raises(KaryviaError) as exc:
        outbound(channel_id="telegram")
    assert exc.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


def test_outbound_carries_full_addressing() -> None:
    """Channel 不查任何缓存即可投递。"""
    message = outbound()
    assert (message.channel_id, message.conversation_id, message.turn_id) == (
        "cli",
        "local",
        "t-1",
    )


@pytest.mark.parametrize(
    ("state", "complete"),
    [
        (StreamState.FINAL, True),
        (StreamState.STARTED, False),
        (StreamState.DELTA, False),
        (StreamState.CANCELLED, False),
        (StreamState.FAILED, False),
    ],
)
def test_only_final_is_a_complete_answer(state: StreamState, complete: bool) -> None:
    """取消与失败不得被渲染为完整答案。"""
    assert outbound(stream_state=state).is_complete_answer is complete


@pytest.mark.parametrize("state", [StreamState.CANCELLED, StreamState.FAILED, StreamState.STARTED])
def test_non_answer_states_allow_empty_body(state: StreamState) -> None:
    """取消/失败/开始允许空正文，标记由 Channel 附加。"""
    assert outbound(content="", stream_state=state).content == ""


@pytest.mark.parametrize("state", [StreamState.FINAL, StreamState.DELTA])
def test_answer_states_require_a_body(state: StreamState) -> None:
    with pytest.raises(KaryviaError) as exc:
        outbound(content="", stream_state=state)
    assert exc.value.code is ErrorCode.INPUT_MALFORMED
