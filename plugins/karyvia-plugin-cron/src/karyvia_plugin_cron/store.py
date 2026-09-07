"""任务存储：`jobs.json` 的读、写与损坏保全。**本包唯一碰文件的模块。**

职责：把任务列表整份读进来、整份原子写回去，并在文件损坏时保住它。
不负责：任务形状与编解码（`job.py`）、调度判定（`schedule.py`）、什么时候写
（`channel.py` / `tools.py` / `commands.py`）。

**整份重写而不是 JSONL 追加。** 与 `plugins/…-memory` 的选择相反，理由也相反：那里的
记录只增不改、量级到 10³，这里的每条任务**每次运行都要改**（`next_run_at`、运行历史），
追加日志加提交水位反而要多一层压缩。任务是 10¹–10² 的量级，整份重写的代价可以忽略。

**损坏的文件不被覆盖。** 解析失败时把它改名成 `jobs.json.corrupt-<时间戳>` 并**拒绝
以空表继续**（抛 `PERSISTENCE_READ_FAILED`）——参考实现在这条上栽过一次，注释里写着
「silently treating a parse error as an empty store would overwrite the recoverable data」。
一个装了二十条提醒的用户宁愿看到实例起不来，也不愿意开机后发现提醒全没了。

**不用 `ctx.fs`**：`sdk.api.FileAccess` 没有 `fsync`、没有原子替换、也没有改名，因此实现
直接用 `pathlib`——与 `builtins/session_jsonl` 和 `plugins/…-memory` 是相同原则。

**IO 全部经 `asyncio.to_thread`**：保存发生在调度循环与工具调用路径上，在事件循环里同步
写盘会卡住同一实例的其他 turn。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

from karyvia.contracts import ErrorCode, JsonValue, KaryviaError

from .job import CronJob, decode_job, encode_job

__all__ = [
    "JOBS_FILE",
    "SCHEMA_VERSION",
    "JobStore",
    "utc_now",
]

#: 存储格式版本。高于本实现所知即拒绝读取（不猜、不降级）。
SCHEMA_VERSION: Final = 1

#: 任务文件名。它是发布出去的契约（README 里有示例），改名要同时改文档。
JOBS_FILE: Final = "jobs.json"

_READ_FAILED: Final = "读取定时任务文件失败。"
_WRITE_FAILED: Final = "写入定时任务文件失败。"
_CORRUPT: Final = "定时任务文件损坏，已保全为 .corrupt-<时间戳>；请人工检查后再启动。"
_FUTURE_VERSION: Final = "定时任务文件的格式版本高于当前实现，请升级 Karyvia 后再读取。"


def utc_now() -> datetime:
    """默认时钟。注入点：用例不依赖真实墙钟。"""
    return datetime.now(UTC)


class JobStore:
    """`jobs.json` 的读写。**它自己不缓存**——缓存在 `channel.py` 的调度器里。

    不缓存是刻意的：这个类只要保证「写进去的能读出来、坏了的不被盖掉」，把「当前任务
    列表是什么」也放进来，就会出现两个真相来源，而 `/cron` 与调度循环恰好会同时读它。
    """

    __slots__ = ("_now", "_path")

    def __init__(self, path: Path, *, now: Callable[[], datetime] = utc_now) -> None:
        self._path = path
        self._now = now

    @property
    def path(self) -> Path:
        return self._path

    async def load(self) -> tuple[CronJob, ...]:
        """读出全部任务。文件不存在时返回空元组（那是首次运行，不是错误）。

        **异常约定**：文件存在但读不懂抛 `PERSISTENCE_READ_FAILED`，且原文件已被改名
        保全。**单条任务记录坏掉也保全整份**：解析在同一次 `json.loads` 里完成，一条坏
        记录说明写入方有 bug，此时猜「哪几条还能要」比整份交给人工检查更危险。
        """
        return await asyncio.to_thread(self._load_sync)

    async def save(self, jobs: Iterable[CronJob]) -> None:
        """整份写回。**异常约定**：失败抛 `PERSISTENCE_WRITE_FAILED`。"""
        payload: dict[str, JsonValue] = {
            "version": SCHEMA_VERSION,
            "updated_at": self._now().isoformat(),
            "jobs": [encode_job(job) for job in jobs],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        await asyncio.to_thread(self._atomic_write, self._path, text.encode("utf-8"))

    # ------------------------------------------------------------------ 内部

    def _load_sync(self) -> tuple[CronJob, ...]:
        if not self._path.exists():
            return ()
        try:
            raw = self._path.read_bytes()
        except OSError as error:
            raise _failed(_READ_FAILED, ErrorCode.PERSISTENCE_READ_FAILED, error) from error
        try:
            # `json.loads` 交回 `Any`。**在这里就收成 `object`**，剩下的全靠 isinstance
            # 收窄——`Any` 会顺着 `document.get(...)` 一路漏进 `decode_job` 的实参，
            # 于是「边界上把动态数据类型化」（原则 6）就只是句口号。
            parsed: object = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise self._preserve(error) from error
        if not isinstance(parsed, Mapping):
            raise self._preserve(None)
        document = cast(Mapping[str, JsonValue], parsed)
        version = document.get("version")
        if isinstance(version, int) and version > SCHEMA_VERSION:
            raise KaryviaError(
                ErrorCode.PERSISTENCE_READ_FAILED,
                _FUTURE_VERSION,
                detail={"found": version, "supported": SCHEMA_VERSION},
            )
        jobs = document.get("jobs")
        if not isinstance(jobs, Sequence) or isinstance(jobs, str):
            raise self._preserve(None)
        try:
            return tuple(decode_job(item) for item in jobs)
        except KaryviaError as error:
            raise self._preserve(error) from error

    def _preserve(self, cause: BaseException | None) -> KaryviaError:
        """把损坏的文件改名保全，返回要抛的错误。

        **改名失败不掩盖原因**：那种情况下文件仍在原地，下次启动还会报同样的错，
        这比吞掉一个 `OSError` 之后让用户以为文件被处理过要好。
        """
        stamp = self._now().strftime("%Y%m%d-%H%M%S")
        backup = self._path.with_name(f"{self._path.name}.corrupt-{stamp}")
        detail: dict[str, JsonValue] = {"backup": backup.name}
        try:
            os.replace(self._path, backup)
        except OSError:
            detail["preserved"] = False
        else:
            detail["preserved"] = True
        if isinstance(cause, KaryviaError):
            detail["cause"] = cause.user_message
        return KaryviaError(ErrorCode.PERSISTENCE_READ_FAILED, _CORRUPT, detail=detail)

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        """同目录临时文件 → `fsync` → `os.replace`（`plugins/…-memory` 的同一段）。

        `os.replace` 在两个平台上都是原子的，因此读的一方永远看到完整的旧内容或完整的
        新内容——**绝不会读到半份任务表**。
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


def _failed(message: str, code: ErrorCode, error: OSError) -> KaryviaError:
    """把一个 `OSError` 折成契约错误。**只放异常类型名与 errno**，不放路径——
    宿主机绝对路径会经错误进到模型可见的文本里（`builtins/tools_fs` 采用的相同规则判定）。"""
    return KaryviaError(
        code, message, detail={"cause": type(error).__name__, "errno": error.errno or 0}
    )
