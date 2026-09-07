"""标识与关联契约。

职责：定义实例/turn/插件标识，以及结构化的 `SessionKey` 与贯穿一次 turn 的 `Correlation`；
提供 `SessionKey` 的稳定、可逆、无碰撞编码 `storage_id()`。
不负责：生成标识（`turn_id` 由 Kernel 分配）、决定会话如何存储、访问文件系统。

`SessionKey` 是结构化对象而不是拼接字符串，这是的根：不同用户、群组、
Channel 和项目不能因为「拼出来的串恰好一样」而共享同一份会话历史。`storage_id()`
一旦发布即为持久化契约——已有实例的目录名和记录键都依赖它，改编码等于让历史会话失联。
"""

from __future__ import annotations

import string
from dataclasses import dataclass
from typing import Final, NewType

from .errors import ErrorCode, KaryviaError

__all__ = [
    "MAX_COMPONENT_LENGTH",
    "Correlation",
    "InstanceId",
    "PluginId",
    "SessionKey",
    "TurnId",
    "validate_identifier",
]

#: 实例标识：一台机器上可并存多个实例，各自有独立的配置与状态目录。
InstanceId = NewType("InstanceId", str)

#: turn 标识：每次 turn 新建，贯穿命令、模型、工具、持久化与事件。
TurnId = NewType("TurnId", str)

#: 插件标识：manifest 中声明的稳定 id，如 "memory-sqlite"。
PluginId = NewType("PluginId", str)


# --------------------------------------------------------------------- SessionKey 编码

#: 编码后直接保留的字符集。刻意取「所有主流文件系统都安全」的最小集合，
#: 使 `storage_id()` 可以直接当目录名或文件名用，不需要再套一层转义。
_SAFE_CHARS: Final[frozenset[str]] = frozenset(string.ascii_letters + string.digits + "._-")

#: 分量分隔符。它不在 `_SAFE_CHARS` 内，因此编码结果中绝不会出现未转义的它，
#: `split` 还原分量时不可能切错位置——这正是「不同输入不可能撞同一个 id」的依据。
_SEPARATOR: Final = "~"

#: 单个分量的长度上限。防止平台传来的畸形标识撑爆路径长度限制。
MAX_COMPONENT_LENGTH: Final = 256


def _encode_component(value: str) -> str:
    """按 UTF-8 逐字节百分号编码，安全字符原样保留。

    编码是单射：安全字符只能来自自身，`%XX` 只能来自对应字节，因此不同分量
    编码后必然不同。
    """
    parts: list[str] = []
    for byte in value.encode("utf-8"):
        char = chr(byte)
        parts.append(char if char in _SAFE_CHARS else f"%{byte:02X}")
    return "".join(parts)


def _decode_component(value: str) -> str:
    """`_encode_component` 的逆运算；遇到非法转义抛 `INPUT_MALFORMED`。"""
    raw = bytearray()
    index = 0
    while index < len(value):
        char = value[index]
        if char != "%":
            raw.extend(char.encode("utf-8"))
            index += 1
            continue
        hex_digits = value[index + 1 : index + 3]
        if len(hex_digits) != 2 or any(digit not in string.hexdigits for digit in hex_digits):
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                "会话标识编码损坏：百分号转义不完整。",
                detail={"component": value, "offset": index},
            )
        raw.append(int(hex_digits, 16))
        index += 3
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            "会话标识编码损坏：还原出的字节不是合法 UTF-8。",
            detail={"component": value},
        ) from exc


def validate_identifier(field: str, value: str, *, max_length: int = MAX_COMPONENT_LENGTH) -> None:
    """标识字段的通用校验：非空、不超长、不含控制字符。

    `message.py` 等模块的 `message_id`、`channel_id`、`call_id` 与会话分量遵循同一条
    规则，因此共用这一个实现——三处平台标识各写一份校验只会慢慢长歪。
    """
    if not value:
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            "标识字段必须非空。",
            detail={"field": field},
        )
    if len(value) > max_length:
        raise KaryviaError(
            ErrorCode.INPUT_TOO_LARGE,
            "标识字段超长。",
            detail={"field": field, "length": len(value), "limit": max_length},
        )
    if any(char < " " or char == "\x7f" for char in value):
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            "标识字段不得含控制字符。",
            detail={"field": field},
        )


@dataclass(frozen=True, slots=True)
class SessionKey:
    """会话的结构化标识。三个分量共同决定一份会话历史的归属。

    - `channel_id`：来源渠道，如 ``"cli"``、``"telegram:main"``。
    - `conversation_id`：平台侧会话标识，如群组 id 或私聊 id。
    - `scope`：项目/工作区维度，让同一个人在不同项目下互不串味。
    """

    channel_id: str
    conversation_id: str
    scope: str = "default"

    def __post_init__(self) -> None:
        validate_identifier("channel_id", self.channel_id)
        validate_identifier("conversation_id", self.conversation_id)
        validate_identifier("scope", self.scope)

    def storage_id(self) -> str:
        """稳定、可逆、无碰撞的编码，可直接用作目录名或记录键。

        ``SessionKey("a", "b:c")`` -> ``"a~b%3Ac~default"``
        ``SessionKey("a:b", "c")`` -> ``"a%3Ab~c~default"``

        两者不同——这正是  要求的性质。发布后不得更改编码方式。
        """
        return _SEPARATOR.join(
            (
                _encode_component(self.channel_id),
                _encode_component(self.conversation_id),
                _encode_component(self.scope),
            )
        )

    @classmethod
    def from_storage_id(cls, value: str) -> SessionKey:
        """`storage_id()` 的逆运算。分量数不为 3 或转义损坏时抛 `INPUT_MALFORMED`。"""
        parts = value.split(_SEPARATOR)
        if len(parts) != 3:
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                "会话标识编码损坏：分量数不是 3。",
                detail={"storage_id": value, "components": len(parts)},
            )
        channel_id, conversation_id, scope = (_decode_component(part) for part in parts)
        return cls(channel_id=channel_id, conversation_id=conversation_id, scope=scope)


@dataclass(frozen=True, slots=True)
class Correlation:
    """一次 turn 的关联标识。

    命令处理、模型调用、工具调用、持久化与事件发布共用同一个实例，因此单个 turn
    的执行过程可以按 `turn_id` 完整还原。subagent / 派生 turn 用 `derive()` 生成，
    通过 `parent_turn_id` 保留父子关系。
    """

    instance_id: InstanceId
    session_key: SessionKey
    turn_id: TurnId
    parent_turn_id: TurnId | None = None

    def __post_init__(self) -> None:
        validate_identifier("instance_id", self.instance_id)
        validate_identifier("turn_id", self.turn_id)
        if self.parent_turn_id is not None:
            validate_identifier("parent_turn_id", self.parent_turn_id)
        if self.parent_turn_id == self.turn_id:
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "派生 turn 的 parent_turn_id 不能等于自身 turn_id。",
                detail={"turn_id": self.turn_id},
            )

    def derive(self, turn_id: TurnId, *, session_key: SessionKey | None = None) -> Correlation:
        """派生子 turn 的关联标识，父子链只记一层父节点。"""
        return Correlation(
            instance_id=self.instance_id,
            session_key=session_key or self.session_key,
            turn_id=turn_id,
            parent_turn_id=self.turn_id,
        )
