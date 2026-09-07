"""元数据契约：受控扩展数据的上限与归一化。

职责：定义 `metadata` 的四项上限常量，并提供把任意映射校验、深拷贝、冻结为
`Mapping[str, JsonValue]` 的纯函数 `normalize_metadata()`。
不负责：决定各字段放什么、脱敏（那是 `errors.redact`）、序列化到磁盘或线缆。

`metadata` 在 `InboundMessage`、`OutboundMessage`、`ToolResult`、`ModelResponse` 四处
出现，四份等价校验没有意义，因此集中在此。非 JSON 值一律抛错而不是静默 `str()`：
SDK 对象混进 metadata 说明 Channel/Provider 的归一化没做完，静默通过
只会让问题推迟到持久化层才炸。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, cast

from .errors import ErrorCode, KaryviaError

if TYPE_CHECKING:  # pragma: no cover - 仅为注解，运行时不导入，避免与包根成环。
    from . import JsonValue

__all__ = [
    "EMPTY_METADATA",
    "MAX_METADATA_BYTES",
    "MAX_METADATA_DEPTH",
    "MAX_METADATA_ENTRIES",
    "MAX_METADATA_KEY_LENGTH",
    "normalize_metadata",
]

#: 顶层键数量上限。平台扩展数据按命名空间分组，几十个键足够。
MAX_METADATA_ENTRIES: Final = 64

#: 估算字节上限。按 UTF-8 编码长度累加，标量按固定成本计（见 `_measure`）。
MAX_METADATA_BYTES: Final = 16 * 1024

#: 嵌套深度上限。平台 payload 再深就该在 Channel 侧摊平，而不是整包塞进来。
MAX_METADATA_DEPTH: Final = 4

#: 单个键名长度上限。
MAX_METADATA_KEY_LENGTH: Final = 128

#: 共享的空只读默认值。`MappingProxyType` 不可变，可以安全地当 dataclass 默认值用。
EMPTY_METADATA: Mapping[str, "JsonValue"] = MappingProxyType({})

#: 标量的估算成本。不做精确序列化——上限的目的是拦住失控大小，不是精确计费。
_SCALAR_COST: Final = 8


def _fail(code: ErrorCode, message: str, **detail: object) -> KaryviaError:
    return KaryviaError(code, message, detail=detail)


def _normalize_key(key: object, *, field: str) -> str:
    if not isinstance(key, str):
        raise _fail(
            ErrorCode.INPUT_MALFORMED,
            "扩展元数据的键必须是字符串。",
            field=field,
            key_type=type(key).__name__,
        )
    if not key:
        raise _fail(ErrorCode.INPUT_MALFORMED, "扩展元数据的键不得为空。", field=field)
    if len(key) > MAX_METADATA_KEY_LENGTH:
        raise _fail(
            ErrorCode.INPUT_TOO_LARGE,
            "扩展元数据的键名超长。",
            field=field,
            length=len(key),
            limit=MAX_METADATA_KEY_LENGTH,
        )
    return key


def _measure(value: str | int | float | bool | None) -> int:
    return len(value.encode("utf-8")) if isinstance(value, str) else _SCALAR_COST


def _normalize_value(value: object, *, field: str, depth: int, size: list[int]) -> JsonValue:
    """递归校验并冻结。`size` 用单元素列表累加，避免在递归里线程一个可变计数器。"""
    if depth > MAX_METADATA_DEPTH:
        raise _fail(
            ErrorCode.INPUT_TOO_LARGE,
            "扩展元数据嵌套过深。",
            field=field,
            limit=MAX_METADATA_DEPTH,
        )

    # bool 必须先于 int 判断：`isinstance(True, int)` 为真。
    if value is None or isinstance(value, (bool, int, float, str)):
        size[0] += _measure(value)
        if size[0] > MAX_METADATA_BYTES:
            raise _fail(
                ErrorCode.INPUT_TOO_LARGE,
                "扩展元数据超过大小上限。",
                field=field,
                limit=MAX_METADATA_BYTES,
            )
        return value

    if isinstance(value, Mapping):
        # isinstance 就是这次 cast 的运行时支撑。
        items = cast("Mapping[object, object]", value).items()
        return MappingProxyType(
            {
                _normalize_key(key, field=field): _normalize_value(
                    item, field=field, depth=depth + 1, size=size
                )
                for key, item in items
            }
        )

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(
            _normalize_value(item, field=field, depth=depth + 1, size=size)
            for item in cast("Sequence[object]", value)
        )

    raise _fail(
        ErrorCode.INPUT_MALFORMED,
        "扩展元数据只接受 JSON 可表达的值；请在 Channel/Provider 边界完成归一化。",
        field=field,
        value_type=type(value).__name__,
    )


def normalize_metadata(
    metadata: Mapping[str, object] | None,
    *,
    field: str = "metadata",
) -> Mapping[str, JsonValue]:
    """校验上限、深拷贝并冻结为只读映射。

    深拷贝是必需的：调用方事后修改自己那份 dict 不得影响已经构造出的契约对象，
    这与 `RuntimeEvent.payload` 的快照语义一致。

    上限超出抛 `INPUT_TOO_LARGE`，非 JSON 值或非法键抛 `INPUT_MALFORMED`，
    `field` 会写进 `detail` 以便定位是哪个字段的元数据出了问题。
    """
    if metadata is None:
        return EMPTY_METADATA
    if len(metadata) > MAX_METADATA_ENTRIES:
        raise _fail(
            ErrorCode.INPUT_TOO_LARGE,
            "扩展元数据的顶层键数量超限。",
            field=field,
            entries=len(metadata),
            limit=MAX_METADATA_ENTRIES,
        )

    size = [0]
    normalized = {
        _normalize_key(key, field=field): _normalize_value(value, field=field, depth=1, size=size)
        for key, value in metadata.items()
    }
    return MappingProxyType(normalized)
