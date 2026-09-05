"""跨平台的 PID 存活探测（`EDG-507`、`DST-005` 的判定依据）。

职责：判定一个 PID 是存活、已死还是无法确定，并尽力取得它的进程创建时间。
不负责：加锁、决定陈旧锁怎么处理——那些结论在 `lock.py`。

本包唯一有 `sys.platform` 分支的模块。隔离出来是为了让 `lock.py` 里没有任何平台判断，
并让锁的测试可以直接注入假的探测函数。

**`Liveness` 的三态是承重的。** `UNKNOWN` 表示「问不出来」，它绝不能授权回收一把锁；
塌成 bool 就等于替调用方在两个错误方向里选一个：判活会永久砖掉实例，判死会让两个进程
同时写同一份会话。
"""

from __future__ import annotations

import os
import sys
from enum import StrEnum

__all__ = ["Liveness", "process_is_alive", "process_started_at"]

#: FILETIME（100ns 单位，1601 纪元）到 Unix 纪元的偏移。
_FILETIME_EPOCH_OFFSET = 116_444_736_000_000_000
_FILETIME_TICKS_PER_SECOND = 10_000_000

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87

#: `/proc/<pid>/stat` 里 `starttime` 的字段序号（1-based，man proc 的第 22 项）。
_PROC_STAT_STARTTIME_FIELD = 22


class Liveness(StrEnum):
    """PID 的存活结论。"""

    ALIVE = "alive"
    DEAD = "dead"
    #: 探测失败。**不得**据此回收锁。
    UNKNOWN = "unknown"


def process_is_alive(pid: int) -> Liveness:
    """判定 `pid` 是否存活。

    `pid <= 0` 在任何 syscall **之前**拒绝：POSIX 上 `os.kill(0, 0)` 打的是整个进程组，
    `-1` 是「除 init 外所有进程」。一个损坏的锁文件不该有能力向本机广播信号。
    """
    if pid <= 0:
        return Liveness.DEAD
    if sys.platform == "win32":
        return _windows_is_alive(pid)
    return _posix_is_alive(pid)


def process_started_at(pid: int) -> float | None:
    """尽力取得进程创建时间（Unix 秒）。取不到返回 `None`。

    这是 PID 复用的护栏：记录在锁里的启动时间与当前进程的实测值不一致，说明 PID 被复用，
    那把锁是陈旧的。取不到时只能退化成「只看存活性」——宁可拒绝启动，也不抢走一把活锁。
    """
    if pid <= 0:
        return None
    if sys.platform == "win32":
        return _windows_started_at(pid)
    if sys.platform.startswith("linux"):
        return _linux_started_at(pid)
    # macOS 与其余平台：需要 sysctl KERN_PROC，成本远超收益（见 §风险 4）。
    return None


def _posix_is_alive(pid: int) -> Liveness:
    """信号 0 只做权限与存在性检查，不投递。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return Liveness.DEAD
    except PermissionError:
        # EPERM = 进程存在但不属于我们。存在性正是我们要问的。
        return Liveness.ALIVE
    except OSError:
        return Liveness.UNKNOWN
    return Liveness.ALIVE


if sys.platform == "win32":  # pragma: no cover - 平台分支，由两侧各自的 CI 覆盖
    import ctypes
    from ctypes import wintypes

    def _kernel32() -> ctypes.WinDLL:
        """带 `use_last_error` 的 kernel32。

        必须用 `use_last_error=True` + `ctypes.get_last_error()`：直接调 `GetLastError`
        读到的可能是被 ctypes 自己的 CRT 调用覆盖过的值。
        """
        dll = ctypes.WinDLL("kernel32", use_last_error=True)
        # 显式声明签名。不声明 restype 会让 HANDLE 在 Win64 上被截成 32 位 int，
        # 于是句柄泄漏 + 对垃圾值调 CloseHandle。
        dll.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        dll.OpenProcess.restype = wintypes.HANDLE
        dll.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        dll.GetExitCodeProcess.restype = wintypes.BOOL
        dll.GetProcessTimes.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        )
        dll.GetProcessTimes.restype = wintypes.BOOL
        dll.CloseHandle.argtypes = (wintypes.HANDLE,)
        dll.CloseHandle.restype = wintypes.BOOL
        return dll

    def _windows_is_alive(pid: int) -> Liveness:
        """用 `OpenProcess` 探测。

        **绝不用 `os.kill(pid, 0)`**：CPython 在 Windows 上把非 CTRL 的信号映射到
        `TerminateProcess`，那个「探测」会杀掉目标进程。
        """
        dll = _kernel32()
        handle = dll.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            code = ctypes.get_last_error()
            if code == _ERROR_INVALID_PARAMETER:
                return Liveness.DEAD
            if code == _ERROR_ACCESS_DENIED:
                # 进程存在，只是不许我们查询。
                return Liveness.ALIVE
            return Liveness.UNKNOWN
        try:
            exit_code = wintypes.DWORD()
            if not dll.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return Liveness.UNKNOWN
            # 已知瑕疵：真以 259 退出的进程会读成存活。偏向安全方向（拒绝抢锁），接受。
            return Liveness.ALIVE if exit_code.value == _STILL_ACTIVE else Liveness.DEAD
        finally:
            dll.CloseHandle(handle)

    def _windows_started_at(pid: int) -> float | None:
        dll = _kernel32()
        handle = dll.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user_time = wintypes.FILETIME()
            ok = dll.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            )
            if not ok:
                return None
            ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return (ticks - _FILETIME_EPOCH_OFFSET) / _FILETIME_TICKS_PER_SECOND
        finally:
            dll.CloseHandle(handle)

else:

    def _windows_is_alive(pid: int) -> Liveness:
        """非 Windows 上不可达；留着让 `process_is_alive` 的分支两侧都有定义。"""
        return Liveness.UNKNOWN

    def _windows_started_at(pid: int) -> float | None:
        return None


def _linux_started_at(pid: int) -> float | None:
    """`/proc/<pid>/stat` 的 starttime（自启动起的时钟节拍）+ `/proc/stat` 的 btime。

    `stat` 的第二个字段是可执行文件名，可能含空格甚至 `)`，所以从**最后一个** `)` 之后
    开始切分——按空格 split 整行会在 `(my proc)` 这类名字上错位。
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read().decode("utf-8", "replace")
        tail = raw[raw.rindex(")") + 2 :].split()
        # tail[0] 是第 3 个字段，所以 starttime 的下标是 22 - 3 = 19。
        ticks = int(tail[_PROC_STAT_STARTTIME_FIELD - 3])
        hertz = os.sysconf("SC_CLK_TCK")
        boot_time = _linux_boot_time()
    except (OSError, ValueError, IndexError):
        return None
    if boot_time is None or hertz <= 0:
        return None
    return boot_time + ticks / hertz


def _linux_boot_time() -> float | None:
    try:
        with open("/proc/stat", "rb") as handle:
            for line in handle:
                if line.startswith(b"btime "):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None
