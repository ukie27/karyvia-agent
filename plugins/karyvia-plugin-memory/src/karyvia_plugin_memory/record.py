"""记录：磁盘表示 ⇄ `ContextFragment`，以及一行 JSON 的编解码。

职责：一条记忆在**磁盘上长什么样**，以及它与契约类型之间的双向翻译。
不负责：任何文件 IO 与提交水位（`store.py`）、分区（`partition.py`）、打分（`scoring.py`）。

格式沿用 `builtins/session_jsonl/codec.py` 立下的两条规矩，因为它们已经被一份发布出去的
存储格式验证过：**可选字段缺席而不是写 `null`**（记忆文件是逐条追加的长文件），
**时间戳一律带时区的 ISO-8601**（`ContextFragment.expires_at` 与它一致）。

**两条与契约有关、必须写在这里的判定：**

- **一切写入都是 `trust=UNTRUSTED`。** 契约的 `MemoryProvider.remember` 只要求模型生成
  内容按 `UNTRUSTED` 写入，但 `/memory` 命令写的内容同样来自聊天窗口里的某个人。统一之后
  召回内容恒被 `contracts/context.py::as_model_text()` 包成带来源的数据块（`EDG-306`），
  没有一条「人手输入因此获得指令优先级」的路径。写入时传进来的 `trust` 因此被**忽略**
  而不是被信任——这是本模块唯一一处刻意不采纳调用方声明的地方，值得单独一条用例。
- **`Sensitivity.SECRET` 拒绝写入。** 组装器本来就不会把 SECRET 片段送进模型，
  存进去只是一条永远召不回来、却实实在在躺在明文文件里的记录。
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, NoReturn, cast

from karyvia.contracts import (
    ContextFragment,
    ErrorCode,
    FragmentKind,
    FragmentScope,
    JsonValue,
    KaryviaError,
    Sensitivity,
    TrustLevel,
)

__all__ = [
    "MAX_CONTENT_CHARS",
    "RECORD_FIELDS",
    "SOURCE",
    "MemoryRecord",
    "decode_record",
    "encode_record",
    "estimate_tokens",
    "from_fragment",
    "to_fragment",
]

#: 片段的来源标识。形状由 `contracts/context.py::_SOURCE_PATTERN` 定死。
SOURCE: Final = "plugin:memory"

#: 单条记忆的字符上限。它远低于 `MAX_FRAGMENT_LENGTH`（256 KiB）——记忆是「要点」，
#: 一条几万字的记忆会把召回预算一口气吃光，而它本该先被摘要。
MAX_CONTENT_CHARS: Final = 4_000

#: 记录行的字段清单，前五个必填。发布后新增字段只能是可选的。
RECORD_FIELDS: Final = (
    "id",
    "content",
    "scope",
    "created_at",
    "sequence",
    "tags",
    "expires_at",
    "origin",
)

_EMPTY_CONTENT: Final = "记忆内容不得为空。"
_CONTENT_TOO_LONG: Final = "记忆内容超过单条上限，请先自行摘要。"
_SECRET_REFUSED: Final = "拒绝把 secret 级内容写进长期记忆：它永远不会被召回，却会留在明文文件里。"
_CORRUPT: Final = "记忆记录与存储格式对不上。"


def estimate_tokens(text: str) -> int:
    """片段的 token 估算。

    **公式必须与组装器裁剪时用的那把尺同口径**：自报偏小会让请求真的超出模型窗口，
    偏大则白丢内容。那把尺是 `ceil(len/3)`，全项目现在有三份实现——
    `kernel/turn/context_builder.py`、`builtins/context_basic/instructions.py`，以及这里。
    前两份由一条逐字符对照测试钉住，而 `R4` 让插件（连同它的测试树）够不着 `kernel/`，
    因此本份只能由一条断言公式字面量的用例守着。**改那把尺时这里要跟着改。**
    """
    return math.ceil(len(text) / 3)


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """一条记忆在磁盘上的形态。

    `sequence` 是分区内单调递增的序号，与分区一起构成 `record_id`（`partition.py`）。
    **删除之后不复用**，因此一个记录标识一旦发出去就永不指向另一条记忆。
    """

    record_id: str
    content: str
    scope: FragmentScope
    created_at: datetime
    sequence: int
    tags: tuple[str, ...] = ()
    expires_at: datetime | None = None
    #: 写入者，只用于展示与审计（`MEM-004` 的「来源标记」）。`tool` / `command` / 插件自填。
    origin: str = ""

    def is_expired(self, now: datetime) -> bool:
        """过期即不召回。`expires_at` 因此不需要一条清理任务——过期记录只是查不到了，
        真正的删除仍由 `forget()` 或 `/memory forget` 完成。"""
        return self.expires_at is not None and self.expires_at <= now


def to_fragment(record: MemoryRecord, *, priority: int) -> ContextFragment:
    """把一条记忆变成可以直接进上下文的片段。

    `kind=MEMORY` 与 `trust=UNTRUSTED` 是固定的（见模块 docstring）；`priority` 由调用方
    按相关性排名给出，于是相关性最低的那条最先被组装器裁掉。
    """
    return ContextFragment(
        source=SOURCE,
        kind=FragmentKind.MEMORY,
        content=record.content,
        priority=priority,
        estimated_tokens=estimate_tokens(record.content),
        scope=record.scope,
        trust=TrustLevel.UNTRUSTED,
        expires_at=record.expires_at,
    )


def from_fragment(
    fragment: ContextFragment,
    *,
    record_id: str,
    sequence: int,
    created_at: datetime,
    origin: str = "",
    tags: tuple[str, ...] = (),
) -> MemoryRecord:
    """把调用方交来的片段变成一条待写入的记录。

    **异常约定**：空内容或超长抛 `INPUT_MALFORMED` / `INPUT_TOO_LARGE`，
    `sensitivity=SECRET` 抛 `INPUT_MALFORMED`。片段自带的 `trust` / `kind` / `priority`
    **不落盘**：前者见模块 docstring，后两者由召回侧统一决定。
    """
    content = fragment.content.strip()
    if not content:
        raise KaryviaError(ErrorCode.INPUT_MALFORMED, _EMPTY_CONTENT, detail={"source": fragment.source})
    if len(content) > MAX_CONTENT_CHARS:
        raise KaryviaError(
            ErrorCode.INPUT_TOO_LARGE,
            _CONTENT_TOO_LONG,
            detail={"length": len(content), "limit": MAX_CONTENT_CHARS},
        )
    if fragment.sensitivity is Sensitivity.SECRET:
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED, _SECRET_REFUSED, detail={"scope": fragment.scope.value}
        )
    return MemoryRecord(
        record_id=record_id,
        content=content,
        scope=fragment.scope,
        created_at=created_at,
        sequence=sequence,
        tags=tags,
        expires_at=fragment.expires_at,
        origin=origin,
    )


# ------------------------------------------------------------------------------ 编解码


def encode_record(record: MemoryRecord) -> str:
    """编码成**不含换行**的一行 JSON。

    `json.dumps` 把内容里的换行转义成 `\\n`，因此「一行一条记录」这个前提不可能被记忆
    内容破坏——这是 JSONL 作为存储格式成立的全部依据。
    """
    payload: dict[str, JsonValue] = {
        "id": record.record_id,
        "content": record.content,
        "scope": record.scope.value,
        "created_at": record.created_at.isoformat(),
        "sequence": record.sequence,
    }
    if record.tags:
        payload["tags"] = list(record.tags)
    if record.expires_at is not None:
        payload["expires_at"] = record.expires_at.isoformat()
    if record.origin:
        payload["origin"] = record.origin
    return json.dumps(payload, ensure_ascii=False)


def decode_record(raw: str, **detail: object) -> MemoryRecord:
    """解码一行文本。

    **异常约定**：形状不符一律 `PERSISTENCE_RECORD_CORRUPT`——与 `session_jsonl` 同一个码，
    理由也相同：读到的字节和格式对不上，读的一方没有别的判断依据。**不退化成「跳过这条」**，
    那是调用方（`store.py`）按 `SES` 系列的先例决定的事。
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        _corrupt(reason=error.msg, **detail)
    if not isinstance(parsed, dict):
        _corrupt(actual_type=type(parsed).__name__, **detail)
    # 边界窄化：`json.loads` 交出 `Any`，在这里定型成契约层的 `JsonValue`。
    fields = cast("Mapping[str, JsonValue]", parsed)

    scope_value = _require_str(fields, "scope", **detail)
    try:
        scope = FragmentScope(scope_value)
    except ValueError as error:
        raise _error(scope=scope_value, **detail) from error
    sequence = fields.get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        _corrupt(field="sequence", **detail)

    return MemoryRecord(
        record_id=_require_str(fields, "id", **detail),
        content=_require_str(fields, "content", **detail),
        scope=scope,
        created_at=_require_time(fields, "created_at", **detail),
        sequence=sequence,
        tags=_optional_tags(fields, **detail),
        expires_at=_optional_time(fields, "expires_at", **detail),
        origin=_optional_str(fields, "origin", **detail),
    )


def _error(**detail: object) -> KaryviaError:
    return KaryviaError(ErrorCode.PERSISTENCE_RECORD_CORRUPT, _CORRUPT, detail=detail)


def _corrupt(**detail: object) -> NoReturn:
    """抛出「记录损坏」。抛出动作放在函数里，`raise` 处因此没有字符串字面量（`TRY003`）。"""
    raise _error(**detail)


def _require_str(fields: Mapping[str, JsonValue], key: str, **detail: object) -> str:
    value = fields.get(key)
    if not isinstance(value, str) or not value:
        _corrupt(field=key, **detail)
    return value


def _optional_str(fields: Mapping[str, JsonValue], key: str, **detail: object) -> str:
    value = fields.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        _corrupt(field=key, **detail)
    return value


def _optional_tags(fields: Mapping[str, JsonValue], **detail: object) -> tuple[str, ...]:
    value = fields.get("tags")
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        _corrupt(field="tags", **detail)
    return tuple(cast("list[str]", value))


def _parse_time(raw: str, key: str, **detail: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as error:
        raise _error(field=key, **detail) from error
    if parsed.tzinfo is None:
        _corrupt(field=key, reason="naive", **detail)
    return parsed


def _require_time(fields: Mapping[str, JsonValue], key: str, **detail: object) -> datetime:
    return _parse_time(_require_str(fields, key, **detail), key, **detail)


def _optional_time(fields: Mapping[str, JsonValue], key: str, **detail: object) -> datetime | None:
    value = fields.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        _corrupt(field=key, **detail)
    return _parse_time(value, key, **detail)
