"""`ctx.fs` 的生产实现：限定在 workspace 内的文件门面。

职责：`GuardedFileAccess`——把路径过 `PathGuard`，再在线程里做实际 IO。
不负责：路径判定本身（`paths.py`）和给模型提供文件工具（`builtins/tools_fs/`）。

**IO 全部经 `asyncio.to_thread`**：插件读的可能是几 MB 的文件，在事件循环里同步读它会卡住
同一实例的其他 turn（`D17` 的同一条理由）。

**写是原子的**：同目录临时文件 → `fsync` → `os.replace`。契约写死「不得留下半份文件」，
而替换成功之后没有可失败的步骤。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from karyvia.contracts import ErrorCode, KaryviaError

from .paths import PathGuard

__all__ = ["GuardedFileAccess"]


class GuardedFileAccess:
    """`FileAccess` 的生产实现。结构化满足契约，不继承任何宿主基类。"""

    __slots__ = ("_guard", "_plugin_id")

    def __init__(self, root: Path, *, plugin_id: str) -> None:
        self._plugin_id = plugin_id
        self._guard = PathGuard(root)

    async def read_text(self, path: str) -> str:
        target = self._guard.resolve(path)
        return await asyncio.to_thread(self._read_sync, target, path)

    async def read_bytes(self, path: str) -> bytes:
        target = self._guard.resolve(path)
        return await asyncio.to_thread(self._read_bytes_sync, target, path)

    async def write_text(self, path: str, content: str) -> None:
        target = self._guard.resolve(path)
        await asyncio.to_thread(self._write_sync, target, path, content.encode("utf-8"))

    async def write_bytes(self, path: str, data: bytes) -> None:
        target = self._guard.resolve(path)
        await asyncio.to_thread(self._write_sync, target, path, data)

    async def list_dir(self, path: str) -> tuple[str, ...]:
        target = self._guard.resolve(path)
        return await asyncio.to_thread(self._list_sync, target, path)

    # ------------------------------------------------------------------ 同步体

    def _read_sync(self, target: Path, shown: str) -> str:
        try:
            return target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise self._read_failed(shown, exc) from exc

    def _read_bytes_sync(self, target: Path, shown: str) -> bytes:
        try:
            return target.read_bytes()
        except OSError as exc:
            raise self._read_failed(shown, exc) from exc

    def _write_sync(self, target: Path, shown: str, payload: bytes) -> None:
        """文本与二进制共用这一段。

        文本那条在调用点就 `encode("utf-8")` 了——写入的原子性、临时文件命名与失败清理
        对两者逐字相同，分成两份实现只会让「不得留下半份文件」有两处可以各自出错。
        """
        temp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(temp, "wb") as handle:  # noqa: PTH123
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
        except OSError as exc:
            temp.unlink(missing_ok=True)
            raise KaryviaError(
                ErrorCode.PERSISTENCE_WRITE_FAILED,
                "写入失败。",
                detail={"plugin": self._plugin_id, "path": shown, "os_error": type(exc).__name__},
            ) from exc

    def _list_sync(self, target: Path, shown: str) -> tuple[str, ...]:
        try:
            return tuple(sorted(entry.name for entry in target.iterdir()))
        except OSError as exc:
            raise self._read_failed(shown, exc) from exc

    def _read_failed(self, shown: str, exc: OSError) -> KaryviaError:
        """`detail` 里只放**插件自己给的**那个相对串与异常类型名。

        宿主机的绝对路径与操作系统的错误文本都不进去：前者是布局泄漏，后者在某些平台上
        会把完整路径拼进消息里。
        """
        return KaryviaError(
            ErrorCode.PERSISTENCE_READ_FAILED,
            "读取失败。",
            detail={"plugin": self._plugin_id, "path": shown, "os_error": type(exc).__name__},
        )
