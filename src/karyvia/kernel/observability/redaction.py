"""事件载荷的脱敏、有界化与 JSON 序列化（技术方案 §6.8；`OBS-003`、`NFR-305`、`NFR-404`）。

职责：把任意载荷在**事件构造之前**收敛成既已脱敏又有界的 `JsonValue`；把
`RuntimeEvent` 与 `KaryviaError` 序列化成可直接写盘的 JSON 字典。
不负责：定义敏感键名规则（那是 `contracts.errors.redact` 的唯一职责，本模块只调用它，
不写第二份）、决定谁能看到事件（`bus.py`）、写文件（`sinks.py`）。

两件事放在同一个模块，是因为它们是同一条承诺的两个面：脱敏的意义在于「离开进程的
字节里没有密文」，而序列化正是那些字节的产生点。分开放，改了一边忘了另一边就是泄漏。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Final

from ...contracts import KaryviaError, RuntimeEvent, redact

if TYPE_CHECKING:  # pragma: no cover - 仅为注解。
    from ...contracts import JsonValue

__all__ = [
    "MAX_PAYLOAD_ENTRIES",
    "MAX_SEQUENCE_ITEMS",
    "error_to_json",
    "event_to_json",
    "prepare_payload",
]

#: 单个映射保留的最大条目数。超出部分整体丢弃，只留一条计数标记。
MAX_PAYLOAD_ENTRIES: Final = 64

#: 单个序列保留的最大元素数。同上。
MAX_SEQUENCE_ITEMS: Final = 128

#: 溢出标记的键名/元素形状。`<` 开头与 `contracts.errors` 的 `<truncated …>` 同一风格。
_OVERFLOW_KEY: Final = "<dropped-entries>"


def _cap_mapping(value: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """按条数上界收敛一个已脱敏的映射。"""
    items = list(value.items())
    capped: dict[str, JsonValue] = {str(key): _cap(item) for key, item in items[:MAX_PAYLOAD_ENTRIES]}
    if len(items) > MAX_PAYLOAD_ENTRIES:
        capped[_OVERFLOW_KEY] = len(items) - MAX_PAYLOAD_ENTRIES
    return capped


def _cap(value: JsonValue) -> JsonValue:
    """按条数上界收敛一棵已脱敏的 JSON 树。

    `redact()` 已经管了单串长度（512）与深度（6），唯独没有条数上界——一条带十万元素
    列表的事件在契约层是合法 JSON，却足以撑爆内存环与日志盘（`NFR-404`）。
    """
    if isinstance(value, Mapping):
        return _cap_mapping(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        items = list(value)
        capped: list[JsonValue] = [_cap(item) for item in items[:MAX_SEQUENCE_ITEMS]]
        if len(items) > MAX_SEQUENCE_ITEMS:
            capped.append(f"<dropped {len(items) - MAX_SEQUENCE_ITEMS} items>")
        return capped
    return value


def prepare_payload(payload: Mapping[str, object]) -> dict[str, JsonValue]:
    """脱敏并有界化一份事件载荷。

    **顺序是先脱敏、后截断，不能反**：先截断会把一个 40 字符的 `sk-…` 切成 20 字符的
    前缀，那既不再匹配 `contracts.errors` 的已知令牌形状，又仍然是一段明文密钥。

    返回的是普通 dict：`RuntimeEvent` 构造时会再 `redact()` 一次（幂等）并冻结为只读
    映射。那一次不是多余的——它保证绕过本模块直接构造事件的路径同样安全。
    """
    redacted, _ = redact(payload)
    return _cap_mapping(redacted)


def error_to_json(error: KaryviaError) -> dict[str, JsonValue]:
    """把 `KaryviaError` 序列化成 JSON。

    `user_message` 与 `detail` 在异常构造时就已脱敏，这里不再处理——重复脱敏一次
    不会更安全，只会让「脱敏发生在哪里」变成两个答案。
    """
    return {
        "code": error.code.value,
        "category": error.category.value,
        "user_message": error.user_message,
        "retryable": error.retryable,
        "detail": dict(error.detail),
        "capability": None if error.capability is None else error.capability.target,
    }


def event_to_json(event: RuntimeEvent) -> dict[str, JsonValue]:
    """把 `RuntimeEvent` 序列化成 JSON，可直接 `json.dumps`。

    `session_key` 用已发布的 `storage_id()` 编码而不是三个分量拆开：诊断要能拿这个串
    直接去 `sessions/` 里找文件，两种表示会让人对不上号。
    """
    correlation = event.correlation
    return {
        "name": event.name.value,
        "family": event.family.value,
        "sequence": event.sequence,
        "occurred_at": event.occurred_at.isoformat(),
        "instance_id": str(event.instance_id),
        "correlation": None
        if correlation is None
        else {
            "session_key": correlation.session_key.storage_id(),
            "turn_id": str(correlation.turn_id),
            "parent_turn_id": None
            if correlation.parent_turn_id is None
            else str(correlation.parent_turn_id),
        },
        "payload": dict(event.payload),
        "error": None if event.error is None else error_to_json(event.error),
    }
