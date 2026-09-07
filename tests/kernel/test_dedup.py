"""去重 LRU 的测试。

主线是 ：同一 `(channel_id, message_id)` 的重投必须被识别并拿到上一次的
`turn_id`。容量与 TTL 是它的两条边界——去重表必须有界，否则它自己就是一个内存泄漏。

TTL 全程用注入的假时钟，不用 `sleep`：真实等待 10 分钟不可能，等 0.1 秒又只是在赌调度。
"""

from __future__ import annotations

import pytest

from karyvia.contracts import ErrorCode, KaryviaError, TurnId
from karyvia.kernel.routing import (
    DEFAULT_DEDUP_CAPACITY,
    DEFAULT_DEDUP_TTL_MS,
    DedupCache,
)


class FakeClock:
    """可推进的单调时钟。`advance` 的单位是秒，与 `time.monotonic` 一致。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def turn(name: str) -> TurnId:
    return TurnId(name)


# --------------------------------------------------------------------------- 基本语义


def test_first_delivery_is_remembered_and_not_a_hit() -> None:
    cache = DedupCache()
    assert cache.remember("telegram", "42", turn("t1")) is None
    assert len(cache) == 1


def test_redelivery_hits_and_reports_the_previous_turn() -> None:
    """ 的核心：重投拿到的是**上一次**的 turn，调用方据此跳过执行。"""
    cache = DedupCache()
    cache.remember("telegram", "42", turn("t1"))

    hit = cache.remember("telegram", "42", turn("t2"))

    assert hit is not None
    assert hit.turn_id == turn("t1")


def test_hit_does_not_overwrite_the_original_record() -> None:
    """连续三次重投都应指向最初那个 turn，而不是上一次重投带来的 turn。"""
    cache = DedupCache()
    cache.remember("cli", "m1", turn("t1"))

    for candidate in ("t2", "t3", "t4"):
        hit = cache.remember("cli", "m1", turn(candidate))
        assert hit is not None
        assert hit.turn_id == turn("t1")


def test_same_message_id_on_another_channel_is_not_a_duplicate() -> None:
    """`message_id` 只在渠道内唯一——两个平台都叫 `1` 的消息是两条不同的消息。"""
    cache = DedupCache()
    cache.remember("telegram", "1", turn("t1"))

    assert cache.remember("chat", "1", turn("t2")) is None


def test_rebind_points_a_merged_message_at_the_executing_turn() -> None:
    cache = DedupCache()
    cache.remember("chat", "m1", turn("candidate"))

    cache.rebind("chat", "m1", turn("executed"))

    hit = cache.remember("chat", "m1", turn("duplicate"))
    assert hit is not None
    assert hit.turn_id == turn("executed")


def test_discard_only_removes_the_matching_unexecuted_candidate() -> None:
    cache = DedupCache()
    cache.remember("chat", "m1", turn("candidate"))

    cache.discard("chat", "m1", turn("another"))
    assert cache.remember("chat", "m1", turn("duplicate")) is not None

    cache.discard("chat", "m1", turn("candidate"))
    assert cache.remember("chat", "m1", turn("retry")) is None


# --------------------------------------------------------------------------- 边界


def test_capacity_evicts_the_oldest_entry() -> None:
    """有界性：撑满之后最旧的那条不再算重复，表的大小不再增长。"""
    cache = DedupCache(capacity=3)
    for index in range(4):
        cache.remember("cli", f"m{index}", turn(f"t{index}"))

    assert len(cache) == 3
    assert cache.remember("cli", "m0", turn("again")) is None
    assert cache.remember("cli", "m3", turn("again")) is not None


def test_entries_expire_after_the_ttl() -> None:
    clock = FakeClock()
    cache = DedupCache(ttl_ms=1000, clock=clock)
    cache.remember("cli", "m1", turn("t1"))

    clock.advance(0.999)
    assert cache.remember("cli", "m1", turn("t2")) is not None

    clock.advance(0.002)
    assert cache.remember("cli", "m1", turn("t3")) is None


def test_expired_entries_are_reclaimed_not_merely_ignored() -> None:
    """过期项必须真的从表里消失，否则「有界」只对未过期的部分成立。"""
    clock = FakeClock()
    cache = DedupCache(ttl_ms=1000, clock=clock)
    for index in range(5):
        cache.remember("cli", f"m{index}", turn(f"t{index}"))

    clock.advance(2.0)
    cache.remember("cli", "fresh", turn("t9"))

    assert len(cache) == 1


def test_hit_reports_how_long_ago_the_message_was_first_seen() -> None:
    clock = FakeClock()
    cache = DedupCache(clock=clock)
    cache.remember("cli", "m1", turn("t1"))
    first_seen = clock.now * 1000.0

    clock.advance(3.0)
    hit = cache.remember("cli", "m1", turn("t2"))

    assert hit is not None
    assert hit.first_seen_ms == pytest.approx(first_seen)


@pytest.mark.parametrize(("capacity", "ttl_ms"), [(0, 1000), (-1, 1000), (10, 0), (10, -5)])
def test_non_positive_bounds_are_rejected(capacity: int, ttl_ms: int) -> None:
    """容量为 0 的去重表什么都不去重，那比没有去重更难查——构造时就拦掉。"""
    with pytest.raises(KaryviaError) as exc:
        DedupCache(capacity=capacity, ttl_ms=ttl_ms)

    assert exc.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


def test_defaults_match_the_documented_budget() -> None:
    """默认去重窗口是 4096 条 / 10 分钟。"""
    assert DEFAULT_DEDUP_CAPACITY == 4096
    assert DEFAULT_DEDUP_TTL_MS == 600_000
