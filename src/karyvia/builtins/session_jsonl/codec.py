"""JSONL 存储格式的编解码：记录行与 `meta.json`。

职责：把 `SessionMessage` 与会话元数据在**磁盘表示**和契约类型之间双向翻译，并把一切
形状问题折成 `PERSISTENCE_RECORD_CORRUPT`。
不负责：任何文件 IO、决定读到哪一字节为止、原子性与顺序（那些在 `store.py`）、
决定压缩语义。

**这是本项目对外发布的持久化格式的唯一实现**（格式必须文档化且可被外部实现
读取）。格式说明在 [`docs/session-storage.md`](../../../../docs/session-storage.md)，
那份文档里的示例由 `tests/builtins/test_session_jsonl.py` 直接喂给本模块解析——文档漂移
会让测试失败，而不是等到某个外部实现读不懂我们的文件时才被发现。

两条格式设计上的取舍：

- **可选字段缺席而不是写 `null`**。`turn_id` / `tool_call_id` / `interrupted` /
  `attachments` / `metadata` 只在有值时出现。会话文件是逐条追加的长文件，恒为空的键会
  让它明显变大，而
  「缺席即默认值」对外部实现同样是一条好写的规则（解码侧一律 `get()`）。
- **时间戳一律带时区的 ISO-8601**。`SessionMessage` 拒绝 naive 时间，格式层因此不需要
  「没有时区怎么办」这条分支——`datetime.fromisoformat` 解出 naive 就是记录坏了。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, NoReturn, cast

from karyvia.contracts import (
    SESSION_SCHEMA_VERSION,
    AttachmentRef,
    AttachmentSource,
    ErrorCode,
    JsonValue,
    KaryviaError,
    Role,
    SessionKey,
    SessionMessage,
    TurnId,
)

__all__ = [
    "META_FIELDS",
    "RECORD_FIELDS",
    "SessionMeta",
    "decode_meta",
    "decode_record",
    "encode_meta",
    "encode_record",
]

#: 记录行的字段清单，前四个必填。发布后新增字段只能是可选的。
RECORD_FIELDS: Final = (
    "message_id",
    "role",
    "content",
    "created_at",
    "turn_id",
    "tool_call_id",
    "interrupted",
    "attachments",
    "metadata",
)

#: `meta.json` 的字段清单。`session_key` 冗余存一份是刻意的：文件名是
#: `SessionKey.storage_id()` 的编码结果，人读不方便，而迁移工具需要一眼看出这份历史属于谁。
META_FIELDS: Final = (
    "schema_version",
    "session_key",
    "created_at",
    "updated_at",
    "compacted_through",
    "committed_bytes",
)


#: `json.dumps` 兜底转换失败时的信息（`TRY003`：消息不写在 raise 处）。
_UNSERIALISABLE: Final = "会话记录里出现了不可序列化的值：{}"


def _corrupt(message: str, **detail: object) -> KaryviaError:
    """记录损坏一律用同一个码。

    错误码写在里面而不是当参数——本模块只可能抛这一个码（读到的字节和格式对不上），
    在每个调用点重复它只是噪音。这与 `contracts.metadata._fail` 的「码作为第一个参数」
    不冲突：那里的调用方确实会抛好几种码。

    **不退化成空历史**：`SessionStore.load()` 明确写着「读取失败或记录损坏抛
    `PERSISTENCE_RECORD_CORRUPT`，不得返回空快照冒充没有历史」——一次读盘故障静默清空
    用户的上下文，比直接报错糟糕得多。
    """
    return KaryviaError(ErrorCode.PERSISTENCE_RECORD_CORRUPT, message, detail=detail)


def _raise_corrupt(message: str, cause: Exception | None = None, /, **detail: object) -> NoReturn:
    """抛出「记录损坏」。第二个参数保留异常链（等价于 `raise ... from cause`）。

    抛出动作放在函数里而不是写在每个调用点，是为了让 `raise` 语句里不出现字符串字面量：
    ruff 的 `TRY003` 只看 `raise f("……")` 的第一个实参，而仓库既有的写法
    （`_fail(code, message, …)`）恰好把错误码放在那个位置。本模块只可能抛这一个码，
    在十几个调用点重复它只是噪音，于是改成「抛出也在函数里完成」。

    前两个参数**位置限定**：`**detail` 是从调用方原样透传下来的，形参只要能被关键字填上，
    类型检查器就没法排除「某个 detail 键恰好叫 cause」。
    """
    error = _corrupt(message, **detail)
    if cause is not None:
        raise error from cause
    raise error


def _parse_object(raw: str, **detail: object) -> Mapping[str, JsonValue]:
    """解析一行/一份 JSON 并断言它是对象。"""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        _raise_corrupt(
            "会话记录不是合法的 JSON。", exc, reason=exc.msg, column=exc.colno, **detail
        )
    if not isinstance(parsed, dict):
        _raise_corrupt("会话记录的顶层必须是 JSON 对象。", actual_type=type(parsed).__name__, **detail)
    # 边界窄化：`json.loads` 交出 `Any`，在这里定型成契约层的 `JsonValue`。
    return cast("Mapping[str, JsonValue]", parsed)


def _require_str(record: Mapping[str, JsonValue], field: str, **detail: object) -> str:
    value = record.get(field)
    if not isinstance(value, str):
        _raise_corrupt("会话记录缺少必填字符串字段。", field=field, **detail)
    return value


def _optional_str(record: Mapping[str, JsonValue], field: str, **detail: object) -> str | None:
    value = record.get(field)
    if value is None or isinstance(value, str):
        return value
    _raise_corrupt("会话记录的可选字段类型不符。", field=field, expected="string", **detail)


def _require_int(record: Mapping[str, JsonValue], field: str, **detail: object) -> int:
    value = record.get(field)
    # `bool` 是 `int` 的子类，放行它等于让 `"compacted_through": true` 变成 1。
    if not isinstance(value, int) or isinstance(value, bool):
        _raise_corrupt("会话元数据缺少必填整数字段。", field=field, **detail)
    return value


def _parse_time(raw: str, field: str, **detail: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        _raise_corrupt(
            "会话记录的时间戳不是合法的 ISO-8601。", exc, field=field, value=raw, **detail
        )
    if parsed.tzinfo is None:
        _raise_corrupt("会话记录的时间戳必须带时区。", field=field, value=raw, **detail)
    return parsed


def _encode_attachment(item: AttachmentRef) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "source": item.source.value,
        "locator": item.locator,
        "media_type": item.media_type,
    }
    if item.size_bytes is not None:
        payload["size_bytes"] = item.size_bytes
    if item.filename is not None:
        payload["filename"] = item.filename
    return payload


def _decode_attachments(
    record: Mapping[str, JsonValue], **detail: object
) -> tuple[AttachmentRef, ...]:
    value = record.get("attachments", [])
    if not isinstance(value, list):
        _raise_corrupt("会话记录的 attachments 必须是数组。", **detail)
    attachments: list[AttachmentRef] = []
    for index, raw_item in enumerate(value):
        if not isinstance(raw_item, dict):
            _raise_corrupt("会话附件必须是 JSON 对象。", attachment=index, **detail)
        item = cast("Mapping[str, JsonValue]", raw_item)
        source_value = _require_str(item, "source", attachment=index, **detail)
        size_bytes = item.get("size_bytes")
        if size_bytes is not None and (
            not isinstance(size_bytes, int) or isinstance(size_bytes, bool)
        ):
            _raise_corrupt(
                "会话附件的 size_bytes 必须是整数。", attachment=index, **detail
            )
        try:
            attachments.append(
                AttachmentRef(
                    source=AttachmentSource(source_value),
                    locator=_require_str(item, "locator", attachment=index, **detail),
                    media_type=_require_str(item, "media_type", attachment=index, **detail),
                    size_bytes=size_bytes,
                    filename=_optional_str(item, "filename", attachment=index, **detail),
                )
            )
        except (KaryviaError, ValueError) as exc:
            _raise_corrupt(
                "会话附件违反契约不变量。",
                exc,
                attachment=index,
                source=source_value,
                **detail,
            )
    return tuple(attachments)


# ------------------------------------------------------------------------------ 记录行


def _unfreeze(value: object) -> Mapping[str, JsonValue]:
    """`json.dumps` 的兜底转换：把契约层冻结过的容器还原成可序列化的形状。

    `normalize_metadata()` 把 `metadata` 深冻结成 `MappingProxyType`（那是它「快照语义」
    的实现方式），而 `json` 不认识这个类型。**只在这里解冻，不去改契约层**：冻结是
    `metadata` 不可变承诺的机制本身，为了迁就一个序列化器而放弃它是本末倒置。
    """
    if not isinstance(value, Mapping):
        raise TypeError(_UNSERIALISABLE.format(type(value).__name__))
    # 边界窄化：能走到这里的只有 `metadata`，而它已由 `normalize_metadata()` 校验过键是
    # 字符串、值是 `JsonValue`。
    return dict(cast("Mapping[str, JsonValue]", value))


def encode_record(message: SessionMessage) -> str:
    """把一条会话记录编码成**不含换行**的 JSON 文本。

    `json.dumps` 会把内容里的换行转义成 `\\n`，因此「一行一条记录」这个前提不可能被
    消息内容破坏——这是 JSONL 作为存储格式成立的全部依据。
    """
    payload: dict[str, JsonValue] = {
        "message_id": message.message_id,
        "role": message.role.value,
        "content": message.content,
        "created_at": message.created_at.isoformat(),
    }
    if message.turn_id is not None:
        payload["turn_id"] = message.turn_id
    if message.tool_call_id is not None:
        payload["tool_call_id"] = message.tool_call_id
    if message.interrupted:
        payload["interrupted"] = True
    if message.attachments:
        payload["attachments"] = [_encode_attachment(item) for item in message.attachments]
    if message.metadata:
        payload["metadata"] = dict(message.metadata)
    return json.dumps(payload, ensure_ascii=False, default=_unfreeze)


def decode_record(raw: str, **detail: object) -> SessionMessage:
    """把一行文本解码回 `SessionMessage`。

    **异常约定**：形状不符一律 `PERSISTENCE_RECORD_CORRUPT`；`detail` 由调用方补上
    文件与行号（本模块看不见文件）。`SessionMessage` 自己的不变量（`tool_call_id` 只在
    `role=TOOL` 时出现、时间戳带时区）在构造时复查，违反同样折成损坏——那说明写它的
    实现和契约对不上，读的一方没有别的判断依据。
    """
    record = _parse_object(raw, **detail)
    role_value = _require_str(record, "role", **detail)
    try:
        role = Role(role_value)
    except ValueError as exc:
        _raise_corrupt("会话记录的 role 不是已知取值。", exc, role=role_value, **detail)

    metadata = record.get("metadata") or {}
    if not isinstance(metadata, dict):
        _raise_corrupt("会话记录的 metadata 必须是 JSON 对象。", **detail)
    interrupted = record.get("interrupted", False)
    if not isinstance(interrupted, bool):
        _raise_corrupt("会话记录的 interrupted 必须是布尔值。", **detail)

    turn_id = _optional_str(record, "turn_id", **detail)
    try:
        return SessionMessage(
            message_id=_require_str(record, "message_id", **detail),
            role=role,
            content=_require_str(record, "content", **detail),
            created_at=_parse_time(_require_str(record, "created_at", **detail), "created_at", **detail),
            turn_id=None if turn_id is None else TurnId(turn_id),
            tool_call_id=_optional_str(record, "tool_call_id", **detail),
            interrupted=interrupted,
            attachments=_decode_attachments(record, **detail),
            metadata=metadata,
        )
    except KaryviaError as exc:
        if exc.code is ErrorCode.PERSISTENCE_RECORD_CORRUPT:
            raise
        _raise_corrupt("会话记录违反契约不变量。", exc, reason=exc.user_message, **detail)


# ---------------------------------------------------------------------------- meta.json


@dataclass(frozen=True, slots=True)
class SessionMeta:
    """`<storage_id>.meta.json` 的内容。

    `committed_bytes` 是本格式的**提交水位**，也是整批追加原子性的全部机制：JSONL 里
    超出这个长度的字节一律视为未提交（上次写到一半就崩了），读取时忽略、下次追加时截断。
    没有它，「整批原子生效」在只能追加的文件上就无从谈起——除非每次追加都
    重写整个文件。
    """

    session_key: SessionKey
    created_at: datetime
    updated_at: datetime
    compacted_through: int = 0
    committed_bytes: int = 0
    schema_version: int = SESSION_SCHEMA_VERSION


def encode_meta(meta: SessionMeta) -> str:
    """把元数据编码成 `meta.json` 的文本（带尾随换行，便于人读与 diff）。"""
    payload: dict[str, JsonValue] = {
        "schema_version": meta.schema_version,
        "session_key": {
            "channel_id": meta.session_key.channel_id,
            "conversation_id": meta.session_key.conversation_id,
            "scope": meta.session_key.scope,
        },
        "created_at": meta.created_at.isoformat(),
        "updated_at": meta.updated_at.isoformat(),
        "compacted_through": meta.compacted_through,
        "committed_bytes": meta.committed_bytes,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def decode_meta(raw: str, **detail: object) -> SessionMeta:
    """解析 `meta.json`。

    **异常约定**：形状不符或版本不等于当前版本，一律
    `PERSISTENCE_RECORD_CORRUPT`。运行时只有当前格式这一条读写路径。
    """
    record = _parse_object(raw, **detail)
    version = _require_int(record, "schema_version", **detail)
    if version != SESSION_SCHEMA_VERSION:
        _raise_corrupt(
            "会话存储格式版本与当前实现不匹配。",
            schema_version=version,
            supported=SESSION_SCHEMA_VERSION,
            **detail,
        )

    key_record = record.get("session_key")
    if not isinstance(key_record, dict):
        _raise_corrupt("会话元数据缺少 session_key 对象。", **detail)
    # 边界窄化同上：嵌套对象在这里定型成三个必填字符串。
    key_fields = cast("Mapping[str, JsonValue]", key_record)
    try:
        session_key = SessionKey(
            channel_id=_require_str(key_fields, "channel_id", **detail),
            conversation_id=_require_str(key_fields, "conversation_id", **detail),
            scope=_require_str(key_fields, "scope", **detail),
        )
    except KaryviaError as exc:
        if exc.code is ErrorCode.PERSISTENCE_RECORD_CORRUPT:
            raise
        _raise_corrupt("会话元数据里的 session_key 非法。", exc, reason=exc.user_message, **detail)

    compacted_through = _require_int(record, "compacted_through", **detail)
    committed_bytes = _require_int(record, "committed_bytes", **detail)
    if compacted_through < 0 or committed_bytes < 0:
        _raise_corrupt(
            "会话元数据的水位不得为负。",
            compacted_through=compacted_through,
            committed_bytes=committed_bytes,
            **detail,
        )
    return SessionMeta(
        session_key=session_key,
        created_at=_parse_time(_require_str(record, "created_at", **detail), "created_at", **detail),
        updated_at=_parse_time(_require_str(record, "updated_at", **detail), "updated_at", **detail),
        compacted_through=compacted_through,
        committed_bytes=committed_bytes,
        schema_version=version,
    )
