"""消息契约：统一的入站与出站消息。

职责：定义发送者、附件引用、流式状态，以及 Kernel 唯一认可的 `InboundMessage` 与
`OutboundMessage`，并在构造时完成字段形状、标识和元数据校验。
不负责：与任何平台 SDK 打交道、决定分段与格式降级策略、投递——归一化与投递都在
Channel 一侧。

两条设计约束值得单独说明：

- `OutboundMessage` 自带 `channel_id + conversation_id + turn_id`，
  Channel 不必维护自己的 Session 映射即可投递；构造时还会断言这些寻址字段与
  `session_key` 一致，否则「自带寻址」反而会静默投错地方。
- CLI 与其他 Channel 共用这一套契约，没有专用旁路。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from .errors import ErrorCode, KaryviaError
from .ids import InstanceId, SessionKey, TurnId, validate_identifier
from .metadata import EMPTY_METADATA, normalize_metadata

if TYPE_CHECKING:  # pragma: no cover - 仅为注解，运行时不导入，避免与包根成环。
    from . import JsonValue

__all__ = [
    "MAX_ATTACHMENTS",
    "MAX_CONTENT_LENGTH",
    "MAX_LOCATOR_LENGTH",
    "AttachmentRef",
    "AttachmentSource",
    "InboundMessage",
    "OutboundMessage",
    "Sender",
    "StreamState",
]

#: 单条消息的文本上限（字符）。 要求大文本产生可预期结果而不是拖垮进程。
MAX_CONTENT_LENGTH: Final = 256 * 1024

#: 单条消息的附件数量上限。
MAX_ATTACHMENTS: Final = 32

#: 附件 locator 的长度上限。URL 与平台 file_id 都远短于此。
MAX_LOCATOR_LENGTH: Final = 4096

#: MIME 形状：`type/subtype`，允许 `+`、`.`、`-` 与参数前的主体部分。
_MEDIA_TYPE_PATTERN: Final = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$"
)

#: 本地绝对路径的三种形态：POSIX `/x`、Windows 盘符 `C:\x`、UNC `\\host\share`。
_ABSOLUTE_PATH_PATTERN: Final = re.compile(r"^(/|[A-Za-z]:[\\/]|\\\\)")


class AttachmentSource(StrEnum):
    """附件的受控访问方式（§10.2「受控访问方式」）。

    没有「本地绝对路径」这一项是刻意的：§10.2 校验规则要求附件不能只依赖未经授权的
    本地绝对路径，把它排除在类型之外比事后检查更彻底。
    """

    URL = "url"
    """可由 Channel 或工具按 SSRF 策略拉取的 http(s) 地址。"""

    WORKSPACE = "workspace"
    """相对 Workspace 根的受控路径，实际解析仍要过的路径守卫。"""

    OPAQUE = "opaque"
    """平台侧不透明标识（如 Telegram `file_id`），需 Channel 用自己的凭据换取。"""

    INLINE = "inline"
    """随消息一并带入的内联数据引用，由 Channel 在边界物化后给出 locator。"""


class StreamState(StrEnum):
    """出站消息的流式状态（§10.3 `stream_state`）。

    `CANCELLED` 与 `FAILED` 必须与 `FINAL` 可区分，Channel 不得渲染为完整答案
    （§10.3 末段）。判定用 `OutboundMessage.is_complete_answer`。
    """

    STARTED = "started"
    DELTA = "delta"
    FINAL = "final"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Sender:
    """规范化后的发送者（§10.2 `sender`）。

    `is_operator` 是实例拥有者/管理员标记，命令权限判定要用它；由 Channel 依据平台
    角色与实例配置在边界判定，Kernel 不再猜。
    """

    user_id: str
    display_name: str | None = None
    is_operator: bool = False
    is_bot: bool = False

    def __post_init__(self) -> None:
        validate_identifier("sender.user_id", self.user_id)


@dataclass(frozen=True, slots=True)
class AttachmentRef:
    """附件引用（§10.2 `attachments`）：引用、媒体类型、大小与受控访问方式。

    契约层只存引用不存字节：附件可能是几十 MB 的媒体，塞进不可变消息对象既浪费内存
    也让事件与日志无从脱敏。
    """

    source: AttachmentSource
    locator: str
    media_type: str
    size_bytes: int | None = None
    filename: str | None = None

    def __post_init__(self) -> None:
        validate_identifier("attachment.locator", self.locator, max_length=MAX_LOCATOR_LENGTH)
        if not _MEDIA_TYPE_PATTERN.match(self.media_type):
            raise KaryviaError(
                ErrorCode.INPUT_UNSUPPORTED_MEDIA,
                "附件媒体类型不是合法的 MIME 形状。",
                detail={"media_type": self.media_type},
            )
        if self.size_bytes is not None and self.size_bytes < 0:
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                "附件大小不得为负。",
                detail={"size_bytes": self.size_bytes},
            )
        self._validate_locator()

    def _validate_locator(self) -> None:
        """`WORKSPACE` 与 `URL` 各有形状要求，其余来源只要求非空。"""
        if self.source is AttachmentSource.URL:
            if not self.locator.lower().startswith(("http://", "https://")):
                raise KaryviaError(
                    ErrorCode.INPUT_MALFORMED,
                    "URL 附件必须是 http/https 地址。",
                    detail={"locator": self.locator},
                )
            return
        if self.source is not AttachmentSource.WORKSPACE:
            return
        if _ABSOLUTE_PATH_PATTERN.match(self.locator):
            raise KaryviaError(
                ErrorCode.PERMISSION_PATH_OUTSIDE_WORKSPACE,
                "附件不得依赖未经授权的本地绝对路径。",
                detail={"locator": self.locator},
            )
        if ".." in self.locator.replace("\\", "/").split("/"):
            raise KaryviaError(
                ErrorCode.PERMISSION_PATH_OUTSIDE_WORKSPACE,
                "附件路径不得包含上跳段。",
                detail={"locator": self.locator},
            )


def _validate_content(content: str, attachments: Sequence[AttachmentRef], *, require: bool) -> None:
    """§10.2 校验规则：内容和附件至少存在一项；大小受限。"""
    if len(content) > MAX_CONTENT_LENGTH:
        raise KaryviaError(
            ErrorCode.INPUT_TOO_LARGE,
            "消息文本超过长度上限。",
            detail={"length": len(content), "limit": MAX_CONTENT_LENGTH},
        )
    if len(attachments) > MAX_ATTACHMENTS:
        raise KaryviaError(
            ErrorCode.INPUT_TOO_LARGE,
            "消息附件数量超限。",
            detail={"count": len(attachments), "limit": MAX_ATTACHMENTS},
        )
    if require and not content and not attachments:
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            "消息的内容与附件不能同时为空。",
        )


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """Kernel 唯一认可的入站消息（§10.2）。

    平台私有字段只能落在 `metadata` 的命名空间键下，Kernel 不解读其结构；
    原始 SDK 对象在 Channel 边界就必须归一化掉，`normalize_metadata`
    会对残留的非 JSON 值直接报错而不是放行。
    """

    message_id: str
    instance_id: InstanceId
    channel_id: str
    conversation_id: str
    sender: Sender
    content: str
    timestamp: datetime
    attachments: tuple[AttachmentRef, ...] = ()
    reply_to: str | None = None
    metadata: Mapping[str, JsonValue] = EMPTY_METADATA

    def __post_init__(self) -> None:
        validate_identifier("message_id", self.message_id)
        validate_identifier("instance_id", self.instance_id)
        validate_identifier("channel_id", self.channel_id)
        validate_identifier("conversation_id", self.conversation_id)
        if self.reply_to is not None:
            validate_identifier("reply_to", self.reply_to)
        if self.timestamp.tzinfo is None:
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                "消息时间必须带时区，否则跨渠道排序无意义。",
                detail={"message_id": self.message_id},
            )
        _validate_content(self.content, self.attachments, require=True)
        object.__setattr__(
            self, "metadata", normalize_metadata(self.metadata, field="inbound.metadata")
        )

    def session_key(self, scope: str = "default") -> SessionKey:
        """按本消息的寻址信息构造 `SessionKey`。

        `scope`（项目/工作区维度）由 Kernel 的路由决定，不在消息里，因此必须显式传入。
        """
        return SessionKey(
            channel_id=self.channel_id,
            conversation_id=self.conversation_id,
            scope=scope,
        )


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """Kernel 交给 Channel 投递的出站消息（§10.3）。

    `session_key` 对应 §10.3 的 `session_id`；`channel_id` 与 `conversation_id` 冗余
    保留，是为了让 Channel 不查任何缓存即可投递。冗余就必须校验一致，否则两份寻址
    信息打架时投递会静默走错目标。
    """

    session_key: SessionKey
    channel_id: str
    conversation_id: str
    turn_id: TurnId
    content: str
    attachments: tuple[AttachmentRef, ...] = ()
    reply_to: str | None = None
    stream_state: StreamState = StreamState.FINAL
    metadata: Mapping[str, JsonValue] = EMPTY_METADATA

    def __post_init__(self) -> None:
        validate_identifier("channel_id", self.channel_id)
        validate_identifier("conversation_id", self.conversation_id)
        validate_identifier("turn_id", self.turn_id)
        if self.reply_to is not None:
            validate_identifier("reply_to", self.reply_to)
        self._validate_addressing()
        # 只有「声称是完整答案」的状态才要求有内容；取消与失败允许空正文，
        # 由 Channel 按  附加标记后呈现。
        _validate_content(
            self.content,
            self.attachments,
            require=self.stream_state in (StreamState.FINAL, StreamState.DELTA),
        )
        object.__setattr__(
            self, "metadata", normalize_metadata(self.metadata, field="outbound.metadata")
        )

    def _validate_addressing(self) -> None:
        if (
            self.session_key.channel_id != self.channel_id
            or self.session_key.conversation_id != self.conversation_id
        ):
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "出站消息的寻址信息与 session_key 不一致。",
                detail={
                    "channel_id": self.channel_id,
                    "conversation_id": self.conversation_id,
                    "session_channel_id": self.session_key.channel_id,
                    "session_conversation_id": self.session_key.conversation_id,
                },
            )

    @property
    def is_complete_answer(self) -> bool:
        """是否可作为完整答案呈现。

        `CANCELLED` / `FAILED` 一律为 False：Channel 必须附加明确标记，
        不得让用户误以为这是模型给出的完整回答。
        """
        return self.stream_state is StreamState.FINAL
