"""内建会话存储：每 session 一个 JSONL + 一个 `meta.json`。

职责：实现 `SessionStore` 的五个方法——追加、读取、压缩、删除与枚举，并保证整批追加的
顺序与原子性；同时提供内建注册入口 `setup(api)`。
不负责：决定何时压缩、保留多久、加锁排队（单写者由 Kernel 的 session 锁保证）、
决定存储目录在哪（由 `ctx` 交下来，见 `resolve_directory`）；也不解读记录内容。

格式与它的对外承诺在 `codec.py` 与 [`docs/session-storage.md`](../../../../docs/session-storage.md)。
本模块只负责「哪些字节算数」这一件事，它的答案是 `meta.json` 里的 `committed_bytes`：

- **追加**先把文件截断到 `committed_bytes`（丢弃上次崩溃留下的半批），写入整批并 `fsync`，
  最后原子替换 `meta.json`。因此崩在任何一步，下次读到的要么是「整批都在」要么是
  「整批都不在」，不会有半条记录，也不会有半批。
- **读取**只看 `[0, committed_bytes)`。超出的字节存在也不算数。

**为什么不用 `ctx.fs`**：`FileAccess` 只有 `read_text` / `write_text` / `list_dir`，没有追加、
没有 `fsync`、没有原子替换，用它实现本模块等于每次追加重写整个会话文件。因此这里直接用
`pathlib` + `os`；文件落盘是本存储实现的核心职责，路径与原子写约束由实现自身保证。

**IO 一律走 `asyncio.to_thread`**：会话历史可以长到几 MB，在事件循环里同步读写它会卡住
同一实例的其他 turn。契约说 `load()`/`append()` 不接受取消，那是「不许中途放弃」，不是
「可以阻塞事件循环」。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from karyvia.contracts import (
    ErrorCode,
    KaryviaError,
    SessionKey,
    SessionMessage,
    SessionSnapshot,
)
from karyvia.sdk import KaryviaAPI, PluginContext

from .codec import SessionMeta, decode_meta, decode_record, encode_meta, encode_record

__all__ = [
    "CAPABILITY_NAME",
    "CONFIG_DIRECTORY_KEY",
    "HISTORY_SUFFIX",
    "META_SUFFIX",
    "JsonlSessionStore",
    "resolve_directory",
    "setup",
]

#: 两个文件的后缀。**必须与 `kernel/config/layout.py::session_paths()` 一致**，
#: 由 `tests/builtins/test_session_jsonl.py` 的一条对照测试盯着：那里是实例布局的唯一
#: 来源，这里是唯一的写入方，两边对不上就意味着 `karyvia session` 找不到文件。
HISTORY_SUFFIX: Final = ".jsonl"
META_SUFFIX: Final = ".meta.json"

#: 临时文件后缀。原子替换用，落在同一目录以保证 `os.replace` 不跨设备。
_TEMP_SUFFIX: Final = ".tmp"

#: 存储目录的配置键。 在装配根上把 `InstanceLayout.sessions_dir` 经这个键交下来。
CONFIG_DIRECTORY_KEY: Final = "dir"

#: 本内建的能力名与插件 id，`manifest.py` 与注册调用共用。
CAPABILITY_NAME: Final = "jsonl"


def _write_failed(message: str, **detail: object) -> KaryviaError:
    return KaryviaError(ErrorCode.PERSISTENCE_WRITE_FAILED, message, detail=detail)


def _read_failed(message: str, **detail: object) -> KaryviaError:
    return KaryviaError(ErrorCode.PERSISTENCE_READ_FAILED, message, detail=detail)


def _fsync_dir(directory: Path) -> None:
    """让 `os.replace` 的结果落盘。

    Windows 上打开目录会抛 `PermissionError`（NTFS 同步写元数据，本就不需要这一步），
    因此吞掉它而不是分平台写两条路径。
    """
    with suppress(OSError):
        fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _atomic_write(path: Path, text: str) -> None:
    """临时文件 + `fsync` + `os.replace`：要么是旧内容，要么是新内容，没有中间态。"""
    temp = path.with_name(path.name + _TEMP_SUFFIX)
    try:
        with open(temp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        _fsync_dir(path.parent)
    except BaseException:
        with suppress(OSError):
            temp.unlink(missing_ok=True)
        raise


class JsonlSessionStore:
    """`SessionStore` 的内建实现。一个目录，每 session 两个文件。

    实例是无状态的（除了目录本身）：两个进程指向同一个目录也不会互相看花眼，因为每次
    读取都从磁盘取全量，而写入的可见性由 `meta.json` 的原子替换定义。真正的跨进程互斥
    由实例锁（`kernel/config/lock.py`）保证——一个实例目录同时只有一个进程。
    """

    __slots__ = ("_directory",)

    def __init__(self, directory: Path) -> None:
        self._directory = Path(directory)

    @property
    def directory(self) -> Path:
        """会话文件所在目录。诊断与测试用。"""
        return self._directory

    def paths_for(self, key: SessionKey) -> tuple[Path, Path]:
        """`(历史文件, 元数据文件)`。文件名只由 `SessionKey.storage_id()` 决定。

        绝不自己编码会话标识：那个编码已发布即为持久化契约，在这里另写一份
        等于给同一份历史造两个名字。
        """
        storage_id = key.storage_id()
        return (
            self._directory / f"{storage_id}{HISTORY_SUFFIX}",
            self._directory / f"{storage_id}{META_SUFFIX}",
        )

    # ------------------------------------------------------------------ SessionStore

    async def load(self, key: SessionKey) -> SessionSnapshot:
        """读取全量视图；会话不存在时返回空快照（**不抛**）。"""
        return await asyncio.to_thread(self._load_sync, key)

    async def append(self, key: SessionKey, messages: Sequence[SessionMessage]) -> None:
        """顺序追加，整批原子生效。空批次是无操作，不会凭空创建会话。"""
        if not messages:
            return
        await asyncio.to_thread(self._append_sync, key, tuple(messages))

    async def compact(self, key: SessionKey, through: int, summary: SessionMessage) -> None:
        """把前 `through` 条记录折成摘要并推进水位。

        **原始记录不物理删除**：摘要**插在水位处**（而不是替换掉前缀），`load()` 之后
        `compacted_through == through` 且摘要出现在 `live_messages` 的最前面。留着原文的
        理由是数据保留语义要可解释——用户删会话时数据真的消失，压缩时则只是不再送进模型。
        """
        await asyncio.to_thread(self._compact_sync, key, through, summary)

    async def delete(self, key: SessionKey) -> bool:
        """删除会话的两个文件，返回它是否真的存在过。删除是物理且不可撤销的。"""
        return await asyncio.to_thread(self._delete_sync, key)

    async def list_keys(self) -> tuple[SessionKey, ...]:
        """列出目录里的全部会话。单个文件名损坏时跳过它，不让整个列表不可用。"""
        return await asyncio.to_thread(self._list_keys_sync)

    # ------------------------------------------------------------------ 同步实现

    def _read_meta(self, meta_path: Path) -> SessionMeta | None:
        """读 `meta.json`。文件不存在返回 `None`，那等价于「这个会话不存在」。"""
        try:
            raw = meta_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _read_failed("无法读取会话元数据。", path=str(meta_path), errno=exc.errno) from exc
        except UnicodeDecodeError as exc:
            raise KaryviaError(
                ErrorCode.PERSISTENCE_RECORD_CORRUPT,
                "会话元数据不是合法的 UTF-8 文本。",
                detail={"path": str(meta_path), "reason": exc.reason},
            ) from exc
        return decode_meta(raw, path=str(meta_path))

    def _read_messages(self, history_path: Path, meta: SessionMeta) -> tuple[SessionMessage, ...]:
        """读 `[0, committed_bytes)` 内的全部记录。

        `committed_bytes` 之后的字节是上次崩溃留下的半批，不算数（下次 `append` 会截掉）。
        反过来，文件比水位还短说明历史被外部截断或删了一截——那是真的损坏，必须报出来而
        不是当成「少了几条」。
        """
        try:
            raw = history_path.read_bytes() if meta.committed_bytes else b""
        except FileNotFoundError:
            raw = b""
        except OSError as exc:
            raise _read_failed("无法读取会话历史。", path=str(history_path), errno=exc.errno) from exc

        if len(raw) < meta.committed_bytes:
            raise KaryviaError(
                ErrorCode.PERSISTENCE_RECORD_CORRUPT,
                "会话历史比已提交水位短，文件可能被外部截断。",
                detail={
                    "path": str(history_path),
                    "size": len(raw),
                    "committed_bytes": meta.committed_bytes,
                },
            )

        committed = raw[: meta.committed_bytes]
        try:
            text = committed.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise KaryviaError(
                ErrorCode.PERSISTENCE_RECORD_CORRUPT,
                "会话历史不是合法的 UTF-8 文本。",
                detail={"path": str(history_path), "reason": exc.reason},
            ) from exc

        return tuple(
            decode_record(line, path=str(history_path), line=number)
            for number, line in enumerate(text.splitlines(), start=1)
            if line.strip()
        )

    def _load_sync(self, key: SessionKey) -> SessionSnapshot:
        history_path, meta_path = self.paths_for(key)
        meta = self._read_meta(meta_path)
        if meta is None:
            return SessionSnapshot(session_key=key)
        messages = self._read_messages(history_path, meta)
        if meta.compacted_through > len(messages):
            raise KaryviaError(
                ErrorCode.PERSISTENCE_RECORD_CORRUPT,
                "压缩水位超出会话记录范围。",
                detail={
                    "path": str(meta_path),
                    "compacted_through": meta.compacted_through,
                    "messages": len(messages),
                },
            )
        return SessionSnapshot(
            session_key=key,
            messages=messages,
            created_at=meta.created_at,
            updated_at=meta.updated_at,
            compacted_through=meta.compacted_through,
            schema_version=meta.schema_version,
        )

    def _ensure_directory(self) -> None:
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise _write_failed(
                "无法创建会话目录。", path=str(self._directory), errno=exc.errno
            ) from exc

    def _append_sync(self, key: SessionKey, messages: tuple[SessionMessage, ...]) -> None:
        self._ensure_directory()
        history_path, meta_path = self.paths_for(key)
        meta = self._read_meta(meta_path)
        now = datetime.now(UTC)
        payload = "".join(f"{encode_record(message)}\n" for message in messages).encode("utf-8")
        committed = meta.committed_bytes if meta is not None else 0

        try:
            with open(history_path, "r+b" if history_path.exists() else "wb") as handle:
                # 先截断到已提交水位：上次崩溃留下的半批在这里被丢弃，而不是被当成历史的
                # 一部分留到下一次读取。
                handle.seek(committed)
                handle.truncate()
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise _write_failed(
                "无法写入会话历史。", path=str(history_path), errno=exc.errno
            ) from exc

        self._write_meta(
            meta_path,
            SessionMeta(
                session_key=key,
                created_at=meta.created_at if meta is not None else now,
                updated_at=now,
                compacted_through=meta.compacted_through if meta is not None else 0,
                committed_bytes=committed + len(payload),
            ),
        )

    def _compact_sync(self, key: SessionKey, through: int, summary: SessionMessage) -> None:
        snapshot = self._load_sync(key)
        if not 0 <= through <= len(snapshot.messages):
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                "压缩水位超出会话记录范围。",
                detail={"through": through, "messages": len(snapshot.messages)},
            )
        if through < snapshot.compacted_through:
            # 水位后退会让已被摘要覆盖的记录重新进入模型上下文，而摘要还留在原处——
            # 同一段历史因此被讲两遍。这是调用方的错，不该由存储层「尽力而为」。
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                "压缩水位不得后退。",
                detail={"through": through, "compacted_through": snapshot.compacted_through},
            )

        self._ensure_directory()
        history_path, meta_path = self.paths_for(key)
        messages = (*snapshot.messages[:through], summary, *snapshot.messages[through:])
        text = "".join(f"{encode_record(message)}\n" for message in messages)
        try:
            _atomic_write(history_path, text)
        except OSError as exc:
            raise _write_failed(
                "无法重写会话历史。", path=str(history_path), errno=exc.errno
            ) from exc

        now = datetime.now(UTC)
        self._write_meta(
            meta_path,
            SessionMeta(
                session_key=key,
                created_at=snapshot.created_at or now,
                updated_at=now,
                compacted_through=through,
                committed_bytes=len(text.encode("utf-8")),
            ),
        )

    def _write_meta(self, meta_path: Path, meta: SessionMeta) -> None:
        try:
            _atomic_write(meta_path, encode_meta(meta))
        except OSError as exc:
            raise _write_failed(
                "无法写入会话元数据。", path=str(meta_path), errno=exc.errno
            ) from exc

    def _delete_sync(self, key: SessionKey) -> bool:
        history_path, meta_path = self.paths_for(key)
        existed = meta_path.exists() or history_path.exists()
        for path in (history_path, meta_path):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                raise _write_failed("无法删除会话文件。", path=str(path), errno=exc.errno) from exc
        return existed

    def _list_keys_sync(self) -> tuple[SessionKey, ...]:
        if not self._directory.exists():
            return ()
        try:
            names = sorted(path.name for path in self._directory.glob(f"*{HISTORY_SUFFIX}"))
        except OSError as exc:
            raise _read_failed(
                "无法枚举会话目录。", path=str(self._directory), errno=exc.errno
            ) from exc

        keys: list[SessionKey] = []
        for name in names:
            # 一条坏文件名不能让 `/session` 与迁移工具整体失效（`SessionStore.list_keys`
            # 的异常约定），跳过它即可——文件还在，人可以自己去看。
            with suppress(KaryviaError):
                keys.append(SessionKey.from_storage_id(name[: -len(HISTORY_SUFFIX)]))
        return tuple(keys)


# ------------------------------------------------------------------------------ 注册入口


def resolve_directory(ctx: PluginContext) -> Path:
    """决定会话文件写在哪：配置里的 `dir`，否则退回插件私有状态目录。

    **本内建不知道实例布局**（`R4` 禁止 `builtins/` import `kernel/`），因此目录只能由
    装配根交下来： 会把 `InstanceLayout.sessions_dir` 放进本插件的配置块
    （`ctx.config` 按  只有自己那一块）。没配时退回 `ctx.state_dir`——那是每个
    插件都必然拥有的私有目录，比抛错更符合「插件在没有配置时也该能工作」。

    **异常约定**：`dir` 存在但不是非空字符串抛 `CONFIG_INVALID`——静默忽略一个写错类型
    的路径，会让会话安静地写到另一个地方去，而用户以为自己改了存储位置。
    """
    configured = ctx.config.get(CONFIG_DIRECTORY_KEY)
    if configured is None:
        return ctx.state_dir
    if not isinstance(configured, str) or not configured.strip():
        raise KaryviaError(
            ErrorCode.CONFIG_INVALID,
            "会话存储目录必须是非空字符串。",
            detail={"key": CONFIG_DIRECTORY_KEY, "actual_type": type(configured).__name__},
        )
    return Path(configured).expanduser()


def setup(api: KaryviaAPI) -> None:
    """内建注册入口，`BUILTIN_MANIFESTS` 里那条 manifest 的 `setup` 指向它。

    与外部插件的 `setup(api)` 完全同型：拿到 Host、调一次 `register_*`、返回。这里刻意
    不做任何 IO——目录在第一次写入时才创建，`karyvia capabilities` 这类只读命令因此不会因为
    一个从未使用过的会话目录而在磁盘上留下痕迹。
    """
    api.register_session_store(CAPABILITY_NAME, JsonlSessionStore(resolve_directory(api.ctx)))
