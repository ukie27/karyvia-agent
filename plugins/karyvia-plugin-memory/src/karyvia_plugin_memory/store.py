"""存储：JSONL + `meta.json` 提交水位，以及契约形状的 `MemoryProvider` 门面。

职责：本包唯一碰文件的模块。分区文件的读、追加、原子重写与提交水位。
不负责：分区映射（`partition.py`）、打分（`scoring.py`）、记录格式（`record.py`）。

**提交水位照搬 `builtins/session_jsonl`**，理由一个字没变：JSONL 是追加写的，不引入水位
就只有两条路——每次追加重写整个文件，或者承认崩溃时可能留下半条记录。水位把它变成两条
规则：**读只认 `[0, committed_bytes)`；写先截断到水位、追加、`fsync`，最后才原子替换
`meta.json`**。于是崩在任何一步，下次读到的要么整条都在、要么整条都不在。
**文件比水位短是损坏，不是「就这些了」**——那说明文件被外部截断，返回一个短列表等于
静默丢用户的记忆。

**`forget()` 真的删**：重写整个分区文件（临时文件 → `fsync` → `os.replace`），不留墓碑。
`MEM-005` 要的是删除，而一条留在明文文件里的墓碑不是删除。分区是 10²–10³ 条的量级，
重写的代价可以接受；真到了需要墓碑 + 定期压缩的规模，那也该是换一个后端而不是改这里。

**不用 `ctx.fs`**：`sdk.api.FileAccess` 只有 `read_text` / `write_text` / `list_dir`，
没有追加、`fsync` 与原子替换，因此实现直接用 `pathlib`——与 `builtins/session_jsonl`
是同一条先例。插件私有状态的持久化由拥有数据的插件负责。

**IO 全部经 `asyncio.to_thread`**：召回发生在每一轮 turn 的组装路径上，在事件循环里同步
读几个文件会卡住同一实例的其他 turn。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, NoReturn, cast

from karyvia.contracts import (
    CancelSignal,
    ContextFragment,
    ErrorCode,
    FragmentScope,
    JsonValue,
    KaryviaError,
    SessionKey,
)

from .partition import Partition, parse_record_id, partition_for, partitions_for, record_id
from .record import MemoryRecord, decode_record, encode_record, from_fragment, to_fragment
from .scoring import rank

__all__ = [
    "META_FIELDS",
    "SCHEMA_VERSION",
    "ContractMemoryProvider",
    "Hit",
    "MemoryStore",
    "utc_now",
]

#: 分区元数据的格式版本。高于本实现所知即拒绝读取（不猜、不降级）。
SCHEMA_VERSION: Final = 1

#: `meta.json` 的字段清单。`scope` / `token` 冗余存一份是刻意的：文件名是编码结果，
#: 人读不方便，而迁移工具需要一眼看出这份记忆属于谁（`session_jsonl` 的同一条理由）。
META_FIELDS: Final = (
    "schema_version",
    "scope",
    "token",
    "created_at",
    "updated_at",
    "next_sequence",
    "committed_bytes",
)

_READ_FAILED: Final = "读取记忆文件失败。"
_WRITE_FAILED: Final = "写入记忆文件失败。"
_TRUNCATED: Final = "记忆文件短于已提交水位，文件可能被外部截断。"
_BAD_META: Final = "记忆元数据与存储格式对不上。"
_FUTURE_VERSION: Final = "记忆存储格式版本高于当前实现，请升级 Karyvia 后再读取。"
_CONTRACT_SCOPE_ONLY_AGENT: Final = (
    "经 MemoryProvider 接口只能读写 agent 范围：它的签名不带 SessionKey，"
    "session / workspace 范围因此无从定位。插件自己的工具与命令不受此限。"
)


def utc_now() -> datetime:
    """默认时钟。注入点：用例不依赖真实墙钟，也不需要 `sleep` 制造时间差。"""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Hit:
    """一次检索命中：记录本身加上它的得分。得分只用于展示与调试，不进上下文。"""

    record: MemoryRecord
    score: float


@dataclass(frozen=True, slots=True)
class _Meta:
    """一个分区的元数据。"""

    scope: FragmentScope
    token: str
    created_at: datetime
    updated_at: datetime
    next_sequence: int = 0
    committed_bytes: int = 0
    schema_version: int = SCHEMA_VERSION


class MemoryStore:
    """本插件的记忆存储。**它是 session 感知的**，与契约的 `MemoryProvider` 形状不同。

    这个差别是硬的，也是本插件形状的由来：契约的
    `remember(fragment, cancel)` / `recall(query, *, scope, limit, cancel)`
    **一个 `SessionKey` 都不带**，因此经那条接口无法表达「这个会话的记忆」。而插件自己的
    四条通路（Context Provider / 三条工具 / 一条命令）全都拿得到 `SessionKey`
    （分别来自 `SessionSnapshot`、`ToolInvocation.correlation` 与 `CommandInvocation`），
    于是它们走这个类，契约门面（`ContractMemoryProvider`）走它的一个受限子集。
    """

    __slots__ = ("_now", "_root")

    def __init__(self, root: Path, *, now: Callable[[], datetime] = utc_now) -> None:
        self._root = root
        self._now = now

    @property
    def root(self) -> Path:
        return self._root

    # ---------------------------------------------------------------------- 写

    async def add(
        self,
        key: SessionKey,
        fragment: ContextFragment,
        *,
        origin: str = "",
        tags: tuple[str, ...] = (),
    ) -> str:
        """写入一条记忆，返回它的记录标识。

        **异常约定**：`USER` 范围与内容问题抛 `INVALID_INPUT` 类（`partition.py` /
        `record.py`），落盘失败抛 `PERSISTENCE_WRITE_FAILED`。**不接受取消**（契约原文）。
        """
        partition = partition_for(fragment.scope, key)
        return await asyncio.to_thread(self._add_sync, partition, fragment, origin, tags)

    def _add_sync(
        self,
        partition: Partition,
        fragment: ContextFragment,
        origin: str,
        tags: tuple[str, ...],
    ) -> str:
        meta = self._read_meta(partition)
        now = self._now()
        sequence = meta.next_sequence
        record = from_fragment(
            fragment,
            record_id=record_id(partition, sequence),
            sequence=sequence,
            created_at=now,
            origin=origin,
            tags=tags,
        )
        line = (encode_record(record) + "\n").encode("utf-8")

        path = self._jsonl_path(partition)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a+b") as handle:
                # 截断到水位：上次写到一半留下的字节在这里被丢掉，而不是被当成记录读。
                handle.truncate(meta.committed_bytes)
                handle.seek(meta.committed_bytes)
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as error:
            raise _failed(_WRITE_FAILED, ErrorCode.PERSISTENCE_WRITE_FAILED, error) from error

        # 水位最后才推进：到这一行为止，那条记录在磁盘上但「不算数」。
        self._write_meta(
            partition,
            _Meta(
                scope=partition.scope,
                token=partition.token,
                created_at=meta.created_at if meta.committed_bytes else now,
                updated_at=now,
                next_sequence=sequence + 1,
                committed_bytes=meta.committed_bytes + len(line),
            ),
        )
        return record.record_id

    async def remove(self, value: str) -> bool:
        """删除一条记忆，返回它是否真的存在过。**不接受取消**（契约原文）。

        **异常约定**：标识形状非法抛 `INPUT_MALFORMED`；分区不存在返回 `False` 而不是抛
        ——「删一条不存在的记忆」不是错误，这是契约写死的。
        """
        partition, sequence = parse_record_id(value)
        return await asyncio.to_thread(self._remove_sync, partition, sequence)

    def _remove_sync(self, partition: Partition, sequence: int) -> bool:
        meta = self._read_meta(partition)
        records = self._read_records(partition, meta)
        kept = [record for record in records if record.sequence != sequence]
        if len(kept) == len(records):
            return False

        payload = "".join(encode_record(record) + "\n" for record in kept).encode("utf-8")
        path = self._jsonl_path(partition)
        self._atomic_write(path, payload)
        # `next_sequence` 不回退：一个已经发出去的记录标识永不指向另一条记忆。
        self._write_meta(
            partition,
            _Meta(
                scope=partition.scope,
                token=partition.token,
                created_at=meta.created_at,
                updated_at=self._now(),
                next_sequence=meta.next_sequence,
                committed_bytes=len(payload),
            ),
        )
        return True

    # ---------------------------------------------------------------------- 读

    async def entries(
        self, key: SessionKey, *, scopes: tuple[FragmentScope, ...]
    ) -> tuple[MemoryRecord, ...]:
        """列出若干分区里**未过期**的全部记忆，新的在前。

        **异常约定**：读失败抛 `PERSISTENCE_READ_FAILED`，记录损坏抛
        `PERSISTENCE_RECORD_CORRUPT`。
        """
        parts = partitions_for(scopes, key)
        return await asyncio.to_thread(self._entries_sync, parts)

    def _entries_sync(self, parts: tuple[Partition, ...]) -> tuple[MemoryRecord, ...]:
        now = self._now()
        collected: list[MemoryRecord] = []
        for partition in parts:
            meta = self._read_meta(partition)
            collected.extend(
                record
                for record in self._read_records(partition, meta)
                if not record.is_expired(now)
            )
        collected.sort(key=lambda record: record.created_at, reverse=True)
        return tuple(collected)

    async def search(
        self,
        key: SessionKey,
        query: str,
        *,
        scopes: tuple[FragmentScope, ...],
        limit: int,
        min_score: float = 0.0,
        cancel: CancelSignal | None = None,
    ) -> tuple[Hit, ...]:
        """按相关性检索。返回按得分降序、同分按分区由窄到宽的命中。

        **取消语义**：读盘之前检查一次。整个检索只有一次外部往返（一次 `to_thread`），
        中途没有可插检查点的位置——加一个必然为假的检查只会让人以为这里能被打断。
        """
        if cancel is not None:
            cancel.raise_if_requested()
        parts = partitions_for(scopes, key)
        return await asyncio.to_thread(self._search_sync, parts, query, limit, min_score)

    def _search_sync(
        self, parts: tuple[Partition, ...], query: str, limit: int, min_score: float
    ) -> tuple[Hit, ...]:
        now = self._now()
        candidates: list[MemoryRecord] = []
        # 按 `partitions_for` 给的顺序（由窄到宽）收集，`rank()` 的稳定排序因此让同分
        # 的会话级记忆排在实例级之前。
        for partition in parts:
            meta = self._read_meta(partition)
            candidates.extend(
                record
                for record in self._read_records(partition, meta)
                if not record.is_expired(now)
            )
        scored = rank(query, [record.content for record in candidates], limit=limit, min_score=min_score)
        return tuple(Hit(candidates[item.index], item.value) for item in scored)

    async def get(self, value: str) -> MemoryRecord | None:
        """按记录标识取一条。不存在返回 `None`。"""
        partition, sequence = parse_record_id(value)
        return await asyncio.to_thread(self._get_sync, partition, sequence)

    def _get_sync(self, partition: Partition, sequence: int) -> MemoryRecord | None:
        meta = self._read_meta(partition)
        for record in self._read_records(partition, meta):
            if record.sequence == sequence:
                return record
        return None

    # ------------------------------------------------------------------ 文件层

    def _jsonl_path(self, partition: Partition) -> Path:
        return self._root / f"{partition.filename}.jsonl"

    def _meta_path(self, partition: Partition) -> Path:
        return self._root / f"{partition.filename}.meta.json"

    def _read_records(self, partition: Partition, meta: _Meta) -> tuple[MemoryRecord, ...]:
        """读一个分区里已提交的全部记录。

        分区文件不存在时水位必然为 0（`_read_meta` 保证），因此「还没写过」与「读不出来」
        不会混淆。
        """
        if meta.committed_bytes == 0:
            return ()
        path = self._jsonl_path(partition)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            # 有 meta、没有 jsonl：水位声称有内容而文件整个不见了，与「短于水位」同一类。
            _fail(_TRUNCATED, ErrorCode.PERSISTENCE_RECORD_CORRUPT, file=path.name)
        except OSError as error:
            raise _failed(_READ_FAILED, ErrorCode.PERSISTENCE_READ_FAILED, error) from error
        if len(raw) < meta.committed_bytes:
            _fail(
                _TRUNCATED,
                ErrorCode.PERSISTENCE_RECORD_CORRUPT,
                file=path.name,
                size=len(raw),
                committed=meta.committed_bytes,
            )
        text = raw[: meta.committed_bytes].decode("utf-8", errors="strict")
        return tuple(
            decode_record(line, file=path.name, line=number)
            for number, line in enumerate(text.splitlines(), start=1)
            if line.strip()
        )

    def _read_meta(self, partition: Partition) -> _Meta:
        """读元数据。文件不存在即视为空分区——那是「还没写过」，不是错误。"""
        now = self._now()
        empty = _Meta(scope=partition.scope, token=partition.token, created_at=now, updated_at=now)
        path = self._meta_path(partition)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return empty
        except OSError as error:
            raise _failed(_READ_FAILED, ErrorCode.PERSISTENCE_READ_FAILED, error) from error
        return _decode_meta(raw, partition, file=path.name)

    def _write_meta(self, partition: Partition, meta: _Meta) -> None:
        payload: dict[str, JsonValue] = {
            "schema_version": meta.schema_version,
            "scope": meta.scope.value,
            "token": meta.token,
            "created_at": meta.created_at.isoformat(),
            "updated_at": meta.updated_at.isoformat(),
            "next_sequence": meta.next_sequence,
            "committed_bytes": meta.committed_bytes,
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        self._atomic_write(self._meta_path(partition), text.encode("utf-8"))

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        """同目录临时文件 → `fsync` → `os.replace`。

        `os.replace` 在两个平台上都是原子的（Windows 上是 `MoveFileEx` +
        `REPLACE_EXISTING`），因此读的一方永远看到完整的旧内容或完整的新内容。
        """
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError as error:
            # 清理失败不覆盖真正的原因：留下一个 .tmp 比丢掉错误信息好。
            temporary.unlink(missing_ok=True)
            raise _failed(_WRITE_FAILED, ErrorCode.PERSISTENCE_WRITE_FAILED, error) from error


class ContractMemoryProvider:
    """`contracts.MemoryProvider` 的实现，注册为 `MEMORY:jsonl`。

    **它只服务 `AGENT` 范围。** 契约的三个方法一个 `SessionKey` 都不带，因此
    `SESSION` / `WORKSPACE` 经这条接口无从定位——静默落到某个「默认」分区会让写入方以为
    自己存进了当前会话，而读的时候什么都找不到。拒绝它并说明原因，是这里唯一诚实的做法
    （`AGENTS.md` 原则 7）。

    **本插件自己不经它工作**：Context Provider、三条工具与 `/memory` 命令直接用
    `MemoryStore`，因为它们都拿得到 `SessionKey`。它仍然有两个消费者——第三方换后端时的
    契约形状（可被 `sdk.testing.MemoryProviderContract` 驱动，`MEM-001`），以及 `D44` 起
    kernel 的召回路径（`memory.provider = "jsonl"` 时装配根就取这个对象）。**后者默认不开**，
    而两边同时开会重复召回，处置写在 README 的「已知边界」第 1 条。
    """

    __slots__ = ("_key", "_store")

    def __init__(self, store: MemoryStore) -> None:
        self._store = store
        # 一个只用于 AGENT 分区的合成 key：`partition_for(AGENT, key)` 不看 key 的任何
        # 分量，这个对象因此不会影响落点，它的存在只是为了满足 `MemoryStore` 的签名。
        self._key = SessionKey(channel_id="memory", conversation_id="instance")

    async def remember(self, fragment: ContextFragment, cancel: CancelSignal) -> str:
        """**异常约定**：写失败抛 `PERSISTENCE_WRITE_FAILED`。**不接受取消**（契约原文）。"""
        del cancel
        _require_agent_scope(fragment.scope)
        return await self._store.add(self._key, fragment, origin="contract")

    async def recall(
        self,
        query: str,
        *,
        scope: FragmentScope,
        limit: int,
        cancel: CancelSignal,
    ) -> Mapping[str, ContextFragment]:
        """按相关性召回，返回 `记录标识 -> 片段` 的**有序**映射（顺序即相关性）。

        **异常约定**：不为了「保证可用」而用空结果掩盖故障——读盘故障原样抛出。
        **取消语义**：检索前检查一次。
        """
        _require_agent_scope(scope)
        hits = await self._store.search(
            self._key, query, scopes=(scope,), limit=limit, cancel=cancel
        )
        # dict 保持插入顺序，因此「顺序即相关性排序」不需要第二种类型来表达。
        return {
            hit.record.record_id: to_fragment(hit.record, priority=index)
            for index, hit in enumerate(hits)
        }

    async def forget(self, record_id: str) -> bool:
        """**异常约定**：不存在返回 `False` 不抛；删除失败抛 `PERSISTENCE_WRITE_FAILED`。"""
        return await self._store.remove(record_id)


def _require_agent_scope(scope: FragmentScope) -> None:
    if scope is not FragmentScope.AGENT:
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            _CONTRACT_SCOPE_ONLY_AGENT,
            detail={"scope": scope.value, "supported": [FragmentScope.AGENT.value]},
        )


# ------------------------------------------------------------------------ 错误与元数据


def _failed(message: str, code: ErrorCode, error: OSError) -> KaryviaError:
    """把一个 `OSError` 折成契约错误。

    **只放 `errno` 与异常类型名，不放 `strerror` 与路径**：那两样会把宿主机的绝对路径写进
    一条可能被模型看到的错误里（`builtins/tools_fs` 的同一条判定）。
    """
    return KaryviaError(
        code,
        message,
        detail={"errno": error.errno, "cause": type(error).__name__},
    )


def _fail(message: str, code: ErrorCode, **detail: object) -> NoReturn:
    """抛出。动作放在函数里，`raise` 处因此没有字符串字面量（`TRY003`）。"""
    raise KaryviaError(code, message, detail=detail)


def _decode_meta(raw: str, partition: Partition, **detail: object) -> _Meta:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        _fail(_BAD_META, ErrorCode.PERSISTENCE_RECORD_CORRUPT, reason=error.msg, **detail)
    if not isinstance(parsed, dict):
        _fail(_BAD_META, ErrorCode.PERSISTENCE_RECORD_CORRUPT, **detail)
    # 边界窄化：`json.loads` 交出 `Any`，在这里定型成契约层的 `JsonValue`。
    fields = cast("Mapping[str, JsonValue]", parsed)

    version = _meta_int(fields, "schema_version", **detail)
    if version > SCHEMA_VERSION:
        _fail(
            _FUTURE_VERSION,
            ErrorCode.PERSISTENCE_RECORD_CORRUPT,
            schema_version=version,
            supported=SCHEMA_VERSION,
            **detail,
        )
    committed = _meta_int(fields, "committed_bytes", **detail)
    next_sequence = _meta_int(fields, "next_sequence", **detail)
    if committed < 0 or next_sequence < 0:
        _fail(_BAD_META, ErrorCode.PERSISTENCE_RECORD_CORRUPT, **detail)
    return _Meta(
        scope=partition.scope,
        token=partition.token,
        created_at=_meta_time(fields, "created_at", **detail),
        updated_at=_meta_time(fields, "updated_at", **detail),
        next_sequence=next_sequence,
        committed_bytes=committed,
        schema_version=version,
    )


def _meta_int(fields: Mapping[str, JsonValue], key: str, **detail: object) -> int:
    value = fields.get(key)
    # `bool` 是 `int` 的子类，放行它等于让 `"committed_bytes": true` 变成 1。
    if not isinstance(value, int) or isinstance(value, bool):
        _fail(_BAD_META, ErrorCode.PERSISTENCE_RECORD_CORRUPT, field=key, **detail)
    return value


def _meta_time(fields: Mapping[str, JsonValue], key: str, **detail: object) -> datetime:
    value = fields.get(key)
    if not isinstance(value, str):
        _fail(_BAD_META, ErrorCode.PERSISTENCE_RECORD_CORRUPT, field=key, **detail)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise KaryviaError(
            ErrorCode.PERSISTENCE_RECORD_CORRUPT, _BAD_META, detail={"field": key, **detail}
        ) from error
    if parsed.tzinfo is None:
        _fail(_BAD_META, ErrorCode.PERSISTENCE_RECORD_CORRUPT, field=key, reason="naive", **detail)
    return parsed
