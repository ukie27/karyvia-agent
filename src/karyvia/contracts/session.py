"""会话契约：持久化单元、快照与 turn 终态（需求 §9.7 `SES-001`–`SES-006`、技术方案 §6.4）。

职责：定义会话历史的最小持久化单元 `SessionMessage`、带版本号的 `SessionSnapshot`，
以及一次 turn 的四个终态 `TurnStatus`、取消原因与 `TurnOutcome`。
不负责：读写存储、加锁与排队、压缩策略、保留期判定——那些在 `kernel/session/`
（`D07`）与 `kernel/routing/`（`D10`）；本模块不含任何 IO。

`SessionSnapshot` 带 `schema_version` 是 `SES-006` 的落点：外部实现可以明确判断自己读到
的格式，而不是靠字段形状猜测。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from .errors import ErrorCode, KaryviaError
from .ids import Correlation, SessionKey, TurnId, validate_identifier
from .message import MAX_ATTACHMENTS, AttachmentRef
from .metadata import EMPTY_METADATA, normalize_metadata

if TYPE_CHECKING:  # pragma: no cover - 仅为注解，运行时不导入，避免与包根成环。
    from . import JsonValue

__all__ = [
    "SESSION_SCHEMA_VERSION",
    "CancelReason",
    "Role",
    "SessionMessage",
    "SessionSnapshot",
    "TurnOutcome",
    "TurnStatus",
]

#: 会话存储格式的版本号。字段语义变化必须递增（`SES-006`）。
SESSION_SCHEMA_VERSION: Final = 2


class Role(StrEnum):
    """会话与模型消息共用的角色。

    定义在会话模块而不是模型模块：历史是会话的资产，模型只是它的消费者之一，
    换 Provider 不应该牵动历史格式（`MOD-004`、`EDG-305`）。
    """

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class TurnStatus(StrEnum):
    """turn 的终态，恰好四个（技术方案 §6.4、`KER-003`、`KER-005`）。

    没有「部分成功」这种中间态：一次 turn 要么完成，要么被取消、失败或撞上预算上限，
    调用方据此决定是否可以把结果当完整答案呈现。
    """

    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    STOPPED_BY_LIMIT = "stopped_by_limit"


class CancelReason(StrEnum):
    """取消原因（`KER-007`、`EDG-206`）。用户重复中断时行为幂等，原因保持第一次的值。"""

    USER = "user"
    TIMEOUT = "timeout"
    BUDGET = "budget"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True, slots=True)
class SessionMessage:
    """会话历史的最小持久化单元（`SES-002`、`SES-004`）。

    `turn_id` 让每条记录可以回溯到产生它的 turn；`interrupted` 对应技术方案 §6.4 的
    检查点 3——流式中途被打断时，已产生的文本要持久化并标记，不能当作完整回答
    （`EDG-304`）。

    `tool_call_id` 只在 `role=TOOL` 时有值，把工具结果与它对应的调用绑起来；
    `attachments` 保存随消息流转的文件引用，不复制文件字节。
    """

    message_id: str
    role: Role
    content: str
    created_at: datetime
    turn_id: TurnId | None = None
    tool_call_id: str | None = None
    interrupted: bool = False
    attachments: tuple[AttachmentRef, ...] = ()
    metadata: Mapping[str, JsonValue] = EMPTY_METADATA

    def __post_init__(self) -> None:
        validate_identifier("session_message.message_id", self.message_id)
        if self.turn_id is not None:
            validate_identifier("session_message.turn_id", self.turn_id)
        if self.created_at.tzinfo is None:
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                "会话记录时间必须带时区。",
                detail={"message_id": self.message_id},
            )
        if (self.role is Role.TOOL) != (self.tool_call_id is not None):
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "tool_call_id 必须且只能出现在 role=TOOL 的记录上。",
                detail={"message_id": self.message_id, "role": self.role.value},
            )
        if len(self.attachments) > MAX_ATTACHMENTS:
            raise KaryviaError(
                ErrorCode.INPUT_TOO_LARGE,
                "会话记录的附件数量超限。",
                detail={"count": len(self.attachments), "limit": MAX_ATTACHMENTS},
            )
        object.__setattr__(
            self, "metadata", normalize_metadata(self.metadata, field="session_message.metadata")
        )


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """某一时刻的会话全量视图（`SES-004`、`SES-006`）。

    `compacted_through` 是已被压缩摘要覆盖的记录数：`messages[:compacted_through]` 的
    内容已由摘要代表，重放时不再逐条送入模型。用计数而不是标记位，是为了让「压缩到
    哪里」在快照里一眼可读，也便于外部实现迁移。
    """

    session_key: SessionKey
    messages: tuple[SessionMessage, ...] = ()
    created_at: datetime | None = None
    updated_at: datetime | None = None
    compacted_through: int = 0
    schema_version: int = SESSION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not 0 <= self.compacted_through <= len(self.messages):
            raise KaryviaError(
                ErrorCode.PERSISTENCE_RECORD_CORRUPT,
                "压缩水位超出会话记录范围。",
                detail={
                    "compacted_through": self.compacted_through,
                    "messages": len(self.messages),
                },
            )
        if self.schema_version != SESSION_SCHEMA_VERSION:
            raise KaryviaError(
                ErrorCode.PERSISTENCE_RECORD_CORRUPT,
                "会话快照的格式版本与当前契约不一致。",
                detail={
                    "schema_version": self.schema_version,
                    "current": SESSION_SCHEMA_VERSION,
                },
            )

    @property
    def live_messages(self) -> Sequence[SessionMessage]:
        """未被压缩摘要覆盖的记录，即需要逐条送入模型的部分。"""
        return self.messages[self.compacted_through :]


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    """一次 turn 的终态记录（`KER-003`、`KER-005`、`OBS-002`）。

    `error` 只在 `FAILED` 时有值，`cancel_reason` 只在 `CANCELLED` 时有值——两者与
    `status` 的一致性在构造时校验，避免出现「状态说成功但带着错误」这种事后没人敢信的
    记录。
    """

    correlation: Correlation
    status: TurnStatus
    started_at: datetime
    finished_at: datetime
    iterations: int = 0
    tool_calls: int = 0
    error: KaryviaError | None = None
    cancel_reason: CancelReason | None = None

    def __post_init__(self) -> None:
        if (self.status is TurnStatus.FAILED) != (self.error is not None):
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "error 必须且只能出现在 status=FAILED 的 turn 上。",
                detail={"status": self.status.value},
            )
        if (self.status is TurnStatus.CANCELLED) != (self.cancel_reason is not None):
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "cancel_reason 必须且只能出现在 status=CANCELLED 的 turn 上。",
                detail={"status": self.status.value},
            )
        if self.iterations < 0 or self.tool_calls < 0:
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "turn 的迭代与工具调用计数不得为负。",
                detail={"iterations": self.iterations, "tool_calls": self.tool_calls},
            )
        if self.finished_at < self.started_at:
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "turn 的结束时间早于开始时间。",
                detail={"turn_id": self.correlation.turn_id},
            )

    @property
    def is_complete_answer(self) -> bool:
        """只有 `COMPLETED` 才能作为完整答案呈现（`EDG-304`）。"""
        return self.status is TurnStatus.COMPLETED
