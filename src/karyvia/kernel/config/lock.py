"""实例排他锁。

职责：用 `O_EXCL` 获取 `instance.lock`，判定并回收陈旧锁，释放时不误删别人的锁。
不负责：判定 PID 是否存活（`process.py`）、发布事件或写日志、注册退出钩子
（生命周期归 `runtime/bootstrap.py`——`kernel/` 不留全局副作用）。

互斥机制只有两样：`O_EXCL` 创建 + 对占用者的存活探测。**不引入** `fcntl.flock` /
`LockFileEx`：规范要的就是这两样，而 flock 在 NFS、fork 继承、以及 Windows 上字节范围锁
是强制而非建议这几点上语义差异大，够自成一个风险项。

**fd 保持打开到 `release()`。** POSIX 上这对互斥没有额外作用，但 Windows 上是真正的
安全网：CPython 的 `os.open` 用 `_wopen`，共享模式 `_SH_DENYNO` 共享读写但**不共享删除**，
所以别的进程即使把陈旧性算错了也 `unlink` 不掉一把活锁，只会拿到 `PermissionError`。
回收路径因此必须把那个错误降级成「持有者存活 → 拒绝」，而不是让它冒出去。
反过来，第二个进程必须仍能**读**被持有的锁文件，否则  报不出 PID——
`_SH_DENYNO` 允许这件事，而这正是有人「用 `msvcrt.locking` 改进一下」就会破坏的不变量。

`O_EXCL` 在 NFS 上不可靠。 把范围限定在同一主机，锁里记的 `hostname` 让共享
文件系统的情形失败关闭：主机名不同就一律当成占用中，绝不去探测另一台机器上的 PID。
"""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Callable, Mapping, cast

from ...contracts import ErrorCode, KaryviaError
from .process import Liveness, process_is_alive, process_started_at

if TYPE_CHECKING:
    from ...contracts import JsonValue

__all__ = ["LockInfo", "StaleLockReclaimed", "InstanceLock"]

#: 记进 `StaleLockReclaimed.raw` 的原始内容上限。锁文件本该是一行 JSON；截断是为了
#: 防止一个被写坏成几 MB 的文件把诊断输出淹掉。
_RAW_EXCERPT_LIMIT = 512

#: 判定 PID 复用时允许的时钟抖动（秒）。`created_at` 取自本进程的 `time.time()`，而
#: `process_started_at` 来自内核，两者不同源；没有余量的话正常启动也会被误判成复用。
_START_TIME_SLACK_SECONDS = 2.0

_REASON_DEAD_PID = "dead_pid"
_REASON_PID_REUSED = "pid_reused"
_REASON_UNREADABLE = "unreadable"

_OPEN_FLAGS = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)


@dataclass(frozen=True, slots=True)
class LockInfo:
    """锁文件的内容。"""

    pid: int
    #: 获取锁的时刻（`time.time()`）。§11 要的「启动时间」，进人可读的错误消息。
    created_at: float
    #: 持有者进程的创建时间；取不到时为 `0.0`。可机器比较的 PID 复用护栏。
    process_started_at: float
    hostname: str
    instance_dir: str

    @classmethod
    def from_json(cls, data: object) -> LockInfo | None:
        """解码锁文件。**不可解读时返回 `None`**，绝不抛异常。

        调用方要的是「这把锁能不能给出一个可探测的 PID」，不是「这个 JSON 合不合法」。
        """
        if not isinstance(data, Mapping):
            return None
        # 边界窄化：`json.loads` 交出的是 `Any`，这里定型成「键值都还未知」的映射，
        # 与 `contracts/errors.py` 处理外来 JSON 的写法一致。
        fields = cast("Mapping[str, object]", data)
        pid = fields.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return None
        return cls(
            pid=pid,
            created_at=_as_float(fields.get("created_at")),
            process_started_at=_as_float(fields.get("process_started_at")),
            hostname=str(fields.get("hostname") or ""),
            instance_dir=str(fields.get("instance_dir") or ""),
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "pid": self.pid,
            "created_at": self.created_at,
            "process_started_at": self.process_started_at,
            "hostname": self.hostname,
            "instance_dir": self.instance_dir,
        }

    def describe(self) -> str:
        """给人看的一句话。PID 必须出现在里面。"""
        when = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.created_at))
            if self.created_at
            else "未知时间"
        )
        where = f"主机 {self.hostname} 上的 " if self.hostname else ""
        return f"{where}进程 {self.pid}（于 {when} 获取该实例锁）"


def _as_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


@dataclass(frozen=True, slots=True)
class StaleLockReclaimed:
    """回收一把陈旧锁的记录。调用方应当把它大声报出来。"""

    #: `dead_pid` / `pid_reused` / `unreadable`。
    reason: str
    holder: LockInfo | None
    #: 被回收锁文件的原始内容（已截断）。`unreadable` 时这是唯一的线索。
    raw: str


class InstanceLock:
    """`instance.lock` 的持有者。

    `now` / `liveness` / `started_at` 可注入，测试因此不需要真去制造一个僵死进程。
    """

    __slots__ = ("_fd", "_info", "_liveness", "_now", "_path", "_reclaimed", "_started_at")

    def __init__(
        self,
        path: Path,
        *,
        now: Callable[[], float] = time.time,
        liveness: Callable[[int], Liveness] = process_is_alive,
        started_at: Callable[[int], float | None] = process_started_at,
    ) -> None:
        self._path = path
        self._now = now
        self._liveness = liveness
        self._started_at = started_at
        self._fd: int | None = None
        self._info: LockInfo | None = None
        self._reclaimed: StaleLockReclaimed | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def info(self) -> LockInfo | None:
        """本进程写进锁里的内容；未持有时为 `None`。"""
        return self._info

    @property
    def held(self) -> bool:
        return self._fd is not None

    @property
    def reclaimed(self) -> StaleLockReclaimed | None:
        """本次 `acquire()` 回收了一把陈旧锁时的记录。"""
        return self._reclaimed

    def acquire(self) -> InstanceLock:
        """获取锁。已被别的活进程持有时抛 `CONFIG_INSTANCE_LOCKED`。

        回收陈旧锁后只重试**一次**：再失败说明别人赢了这场竞争，报新持有者。有界重试
        而非循环，两个同时启动的进程因此不会来回抢。
        """
        if self._fd is not None:
            raise KaryviaError(
                ErrorCode.CONFIG_INSTANCE_LOCKED,
                "本进程已持有该实例锁。",
                detail={"path": str(self._path), "holder_pid": os.getpid()},
            )
        try:
            self._create()
        except FileExistsError:
            self._reclaim_or_fail()
            try:
                self._create()
            except FileExistsError as exc:
                raise self._occupied(self._read_holder()[0], "另一个进程刚刚抢到该实例锁。") from exc
        return self

    def release(self) -> None:
        """释放锁。幂等。

        unlink 前重读 PID：如果文件已经被别人回收并重建，那把锁不是我们的，删掉它会让
        两个进程同时认为自己持有实例。
        """
        fd, self._fd = self._fd, None
        info, self._info = self._info, None
        if fd is None:
            return
        try:
            os.close(fd)
        except OSError:
            pass
        holder, _ = self._read_holder()
        if info is not None and holder is not None and holder.pid != info.pid:
            return
        try:
            self._path.unlink()
        except OSError:
            # 已经不在了，或没权限删——两种情况下都无事可做。
            pass

    def __enter__(self) -> InstanceLock:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    def _create(self) -> None:
        """`O_EXCL` 创建并写入。fd 留着不关。"""
        info = LockInfo(
            pid=os.getpid(),
            created_at=self._now(),
            process_started_at=self._started_at(os.getpid()) or 0.0,
            hostname=socket.gethostname(),
            instance_dir=str(self._path.parent),
        )
        payload = json.dumps(info.to_json(), ensure_ascii=False).encode("utf-8")
        try:
            fd = os.open(self._path, _OPEN_FLAGS, 0o600)
        except FileExistsError:
            raise
        except OSError as exc:
            raise KaryviaError(
                ErrorCode.PERSISTENCE_WRITE_FAILED,
                "无法创建实例锁文件。",
                detail={"path": str(self._path), "errno": exc.errno},
            ) from exc
        try:
            os.write(fd, payload)
            os.fsync(fd)
        except OSError as exc:
            os.close(fd)
            try:
                self._path.unlink()
            except OSError:
                pass
            raise KaryviaError(
                ErrorCode.PERSISTENCE_WRITE_FAILED,
                "无法写入实例锁文件。",
                detail={"path": str(self._path), "errno": exc.errno},
            ) from exc
        self._fd = fd
        self._info = info

    def _read_holder(self) -> tuple[LockInfo | None, str]:
        """读占用者。返回 `(解码结果或 None, 原始内容摘要)`。"""
        try:
            raw_bytes = self._path.read_bytes()
        except OSError:
            return (None, "")
        raw = raw_bytes.decode("utf-8", "replace")[:_RAW_EXCERPT_LIMIT]
        try:
            return (LockInfo.from_json(json.loads(raw)), raw)
        except ValueError:
            return (None, raw)

    def _reclaim_or_fail(self) -> None:
        """对占用者分类：陈旧就回收，否则抛 `CONFIG_INSTANCE_LOCKED`。"""
        holder, raw = self._read_holder()

        if holder is None:
            # 不可解析的锁给不出 PID，也给不出恢复路径（只能手工删）。一次「create 与
            # write 之间崩溃」就会永久砖掉实例，所以当陈旧处理——窗口只有微秒，且回收
            # 会被记录下来。
            self._reclaim(_REASON_UNREADABLE, None, raw)
            return

        if holder.hostname and holder.hostname != socket.gethostname():
            raise self._occupied(holder, "该实例目录正被另一台主机上的进程使用。")

        if holder.pid == os.getpid():
            # 这是重复获取的 bug。藏起来比报出来更糟。
            raise self._occupied(holder, "本进程已持有该实例锁。")

        state = self._liveness(holder.pid)
        if state is Liveness.DEAD:
            self._reclaim(_REASON_DEAD_PID, holder, raw)
            return
        if state is Liveness.UNKNOWN:
            # 模糊的答案永远不得授权抢走一把活锁。
            raise self._occupied(holder, "无法确认实例锁持有者是否仍在运行。")

        observed = self._started_at(holder.pid)
        if (
            observed is not None
            and holder.created_at
            and observed > holder.created_at + _START_TIME_SLACK_SECONDS
        ):
            # 这个 PID 上现在跑的是个更年轻的进程：PID 被复用了，锁是陈旧的。
            self._reclaim(_REASON_PID_REUSED, holder, raw)
            return

        raise self._occupied(holder, "另一个 Karyvia 实例正在使用该实例目录。")

    def _reclaim(self, reason: str, holder: LockInfo | None, raw: str) -> None:
        """删掉陈旧锁文件。删不掉就说明它其实是活的（见模块 docstring 的 Windows 一段）。"""
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise self._occupied(
                holder,
                "实例锁文件看似陈旧但无法删除，按持有者仍在运行处理。",
                errno=exc.errno,
            ) from exc
        self._reclaimed = StaleLockReclaimed(reason=reason, holder=holder, raw=raw)

    def _occupied(
        self,
        holder: LockInfo | None,
        message: str,
        *,
        errno: int | None = None,
    ) -> KaryviaError:
        """组装 `CONFIG_INSTANCE_LOCKED`。持有者已知时把 PID 写进人可读消息里。"""
        detail: dict[str, JsonValue] = {"path": str(self._path)}
        if holder is not None:
            detail["holder_pid"] = holder.pid
            detail["holder_hostname"] = holder.hostname
            detail["holder_created_at"] = holder.created_at
            message = f"{message} 持有者：{holder.describe()}。"
        if errno is not None:
            detail["errno"] = errno
        return KaryviaError(ErrorCode.CONFIG_INSTANCE_LOCKED, message, detail=detail)
