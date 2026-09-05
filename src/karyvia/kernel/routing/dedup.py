"""入站消息去重：有界 LRU + TTL（技术方案 §6.5，需求 `EDG-201`）。

职责：按 `(channel_id, message_id)` 记住最近处理过的入站消息，让重复投递能被识别并拿到
上一次的 `turn_id`。
不负责：决定重复之后做什么（跳过执行是调用方的动作）、持久化（进程重启后重新开始记
——重投窗口是分钟级，跨重启的重复投递本就不该按「重复」处理）、认识 session 与并发策略
（那是 `session_lock.py`）。

**为什么是 `(channel_id, message_id)` 而不是内容哈希**：`EDG-201` 要挡的是同一条外部消息
被平台重投（webhook 重试、断线重连补发），它带着同一个平台 `message_id`。内容哈希会把
用户真的连发两次「继续」也判成重复，那是正常输入。

**命中时不刷新条目**：TTL 从**首次**处理算起。平台每隔几秒重投一次的话，按访问续期会让
一条消息永远留在表里，重投窗口变成无限长。
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from karyvia.contracts import ErrorCode, KaryviaError, TurnId

__all__ = [
    "DEFAULT_DEDUP_CAPACITY",
    "DEFAULT_DEDUP_TTL_MS",
    "DedupCache",
    "DedupHit",
]

#: 记住多少条消息。默认值与 `kernel/config/schema.py` 的同名常量必须相等。
DEFAULT_DEDUP_CAPACITY: Final = 4096

#: 一条记录的存活时长（毫秒）。10 分钟覆盖主流平台的 webhook 重试窗口。
DEFAULT_DEDUP_TTL_MS: Final = 600_000


@dataclass(frozen=True, slots=True)
class DedupHit:
    """一次命中：这条消息之前已经处理过，用的是 `turn_id`。

    带上 `first_seen_ms`（相对 `clock()` 原点的毫秒数）是为了让调用方能在事件里说清
    「3 秒前刚处理过」，而不是只说一句「重复」。
    """

    turn_id: TurnId
    first_seen_ms: float


class DedupCache:
    """`(channel_id, message_id)` 的有界 LRU，带 TTL。

    同步、非线程安全：它只在单线程的准入路径上被调用，加锁只会让每条消息多付一次无谓的
    开销。真要从别的线程投递消息，先把消息送进事件循环，而不是给这里加锁。
    """

    __slots__ = ("_capacity", "_clock", "_entries", "_ttl_seconds")

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_DEDUP_CAPACITY,
        ttl_ms: int = DEFAULT_DEDUP_TTL_MS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """`clock` 可注入：TTL 的测试不该靠 `sleep` 制造时序。

        **异常约定**：`capacity` 或 `ttl_ms` 非正抛 `KERNEL_INVARIANT_VIOLATED`——容量为 0
        的去重表会静默地什么都不去重，那比没有去重更难查。
        """
        if capacity <= 0 or ttl_ms <= 0:
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "去重表的容量与 TTL 必须为正。",
                detail={"capacity": capacity, "ttl_ms": ttl_ms},
            )
        self._capacity = capacity
        self._ttl_seconds = ttl_ms / 1000.0
        self._clock = clock
        # 值是 `(turn_id, 记录时刻)`。插入顺序即淘汰顺序。
        self._entries: OrderedDict[tuple[str, str], tuple[TurnId, float]] = OrderedDict()

    def __len__(self) -> int:
        """当前记录数（含尚未被清理的过期项）。诊断与测试用。"""
        return len(self._entries)

    def remember(self, channel_id: str, message_id: str, turn_id: TurnId) -> DedupHit | None:
        """登记一条消息；若它是重复投递则返回 `DedupHit` 且**不覆盖**原记录。

        返回 `None` 表示「没见过，已登记」，调用方照常执行。返回 `DedupHit` 表示
        「见过」，调用方应当跳过执行并引用上一次的结果（`EDG-201`）——重复执行的代价是
        重复触发有副作用的工具，那不是靠重试能修复的。
        """
        now = self._clock()
        self._evict_expired(now)
        key = (channel_id, message_id)
        found = self._entries.get(key)
        if found is not None:
            previous_turn, first_seen = found
            return DedupHit(turn_id=previous_turn, first_seen_ms=first_seen * 1000.0)

        self._entries[key] = (turn_id, now)
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)
        return None

    def rebind(self, channel_id: str, message_id: str, turn_id: TurnId) -> None:
        """把已登记消息绑定到实际执行它的 Turn；MERGE 批次会用到。"""
        key = (channel_id, message_id)
        found = self._entries.get(key)
        if found is not None:
            self._entries[key] = (turn_id, found[1])

    def discard(self, channel_id: str, message_id: str, turn_id: TurnId) -> None:
        """移除一次未执行的准入占位，不误删后来登记的同名消息。"""
        key = (channel_id, message_id)
        found = self._entries.get(key)
        if found is not None and found[0] == turn_id:
            del self._entries[key]

    def _evict_expired(self, now: float) -> None:
        """清掉已过期的记录。

        只从最旧端扫：记录按插入时刻有序（命中不续期，见模块 docstring），因此遇到第一个
        未过期的就可以停——不需要每次都遍历整张表。
        """
        deadline = now - self._ttl_seconds
        while self._entries:
            key = next(iter(self._entries))
            if self._entries[key][1] > deadline:
                return
            del self._entries[key]
