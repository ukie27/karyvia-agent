"""`expr.py` 的用例：5 字段语法、日/周并集、月末与闰年、DST 前后的跳变。

**一条也不依赖 tzdata**：所有带时区的断言用 `UTC` 或 `_cron_fakes.FakeDst`
（手写的跳表时区）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from _cron_fakes import FakeDst
from karyvia_plugin_cron.expr import MAX_SEARCH_DAYS, parse_expr

from karyvia.contracts import ErrorCode, KaryviaError


def at(text: str, zone: object = UTC) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=zone)  # type: ignore[arg-type]


def next_after(expression: str, moment: str, zone: object = UTC) -> datetime:
    return parse_expr(expression).next_after(at(moment, zone))


# ------------------------------------------------------------------------------ 语法


@pytest.mark.parametrize(
    ("expression", "field", "expected"),
    [
        ("*/15 * * * *", "minutes", {0, 15, 30, 45}),
        ("0,30 * * * *", "minutes", {0, 30}),
        ("5 * * * *", "minutes", {5}),
        ("5/15 * * * *", "minutes", {5, 20, 35, 50}),
        ("* 9-17 * * *", "hours", set(range(9, 18))),
        ("* 9-17/4 * * *", "hours", {9, 13, 17}),
        ("* * * JAN,jul *", "months", {1, 7}),
        ("* * * * MON-FRI", "weekdays", {1, 2, 3, 4, 5}),
        ("* * * * 7", "weekdays", {0}),
        ("* * * * 0,7", "weekdays", {0}),
    ],
)
def test_field_syntax(expression: str, field: str, expected: set[int]) -> None:
    assert set(getattr(parse_expr(expression), field)) == expected


@pytest.mark.parametrize(
    "expression",
    [
        "* * * *",  # 字段数不对
        "* * * * * *",  # 秒字段不支持
        "60 * * * *",  # 越界
        "* 24 * * *",
        "* * 0 * *",
        "* * * 13 *",
        "* * * * 8",
        "5-1 * * * *",  # 范围倒置
        "*/0 * * * *",  # 步长为 0
        "*/ * * * *",
        "0 0 L * *",  # 不支持的扩展语法
        "0 0 * * 5#2",
        "@daily",
        "abc * * * *",
        "0,, * * * *",
    ],
)
def test_rejects_bad_expressions(expression: str) -> None:
    with pytest.raises(KaryviaError) as caught:
        parse_expr(expression)
    assert caught.value.code is ErrorCode.INPUT_MALFORMED


def test_unsupported_syntax_is_told_apart_from_a_typo() -> None:
    """croniter 的扩展语法要说「不支持」而不是「写错了」——用户要知道的是哪一种。"""
    with pytest.raises(KaryviaError) as caught:
        parse_expr("0 0 * * 5#2")
    assert caught.value.detail["characters"] == ["#"]

    with pytest.raises(KaryviaError) as caught:
        parse_expr("0 0 L * *")
    assert caught.value.detail == {"field": "day", "value": "L"}


def test_month_and_weekday_names_containing_l_or_w_still_parse() -> None:
    """`jul` 含 `l`、`wed` 含 `w`：全文扫 `L`/`W` 会把它们一并拒掉。"""
    assert set(parse_expr("* * * jul *").months) == {7}
    assert set(parse_expr("* * * * wed").weekdays) == {3}


# ------------------------------------------------------------------------------ 推进


@pytest.mark.parametrize(
    ("expression", "moment", "expected"),
    [
        # 每小时整点
        ("0 * * * *", "2026-08-15T09:30:00", "2026-08-15T10:00:00"),
        # 恰好落在匹配时刻上：**必须严格向后**，不能返回自己
        ("0 * * * *", "2026-08-15T09:00:00", "2026-08-15T10:00:00"),
        # 工作日 9 点：2026-08-15 是周六，下一次是周一
        ("0 9 * * 1-5", "2026-08-15T09:30:00", "2026-08-17T09:00:00"),
        # 跨月
        ("0 0 1 * *", "2026-08-15T09:00:00", "2026-09-01T00:00:00"),
        # 跨年
        ("0 0 1 1 *", "2026-08-15T09:00:00", "2027-01-01T00:00:00"),
        # 闰年 2 月 29 日：2028 是下一个闰年
        ("0 0 29 2 *", "2026-08-15T09:00:00", "2028-02-29T00:00:00"),
        # 月末 31 号：9 月只有 30 天，跳到 10 月
        ("0 0 31 * *", "2026-09-01T00:00:00", "2026-10-31T00:00:00"),
    ],
)
def test_next_after(expression: str, moment: str, expected: str) -> None:
    assert next_after(expression, moment) == at(expected)


def test_day_and_weekday_are_a_union() -> None:
    """POSIX 传统语义：两者都被限定时取**并集**而不是交集。

    `0 0 13 * 5` = 每月 13 号**或**每个周五。2026-08-15 是周六，下一个周五是 8-21，
    而 9 月 13 号更晚——因此答案是周五那次。
    """
    assert next_after("0 0 13 * 5", "2026-08-15T09:00:00") == at("2026-08-21T00:00:00")


def test_day_alone_is_not_a_union() -> None:
    """只限定日字段时，星期是 `*`，不参与并集。"""
    assert next_after("0 0 13 * *", "2026-08-15T09:00:00") == at("2026-09-13T00:00:00")


def test_never_matching_expression_is_reported() -> None:
    """`0 0 30 2 *`（2 月 30 日）永远不匹配。**报错而不是返回一个假时刻。**"""
    with pytest.raises(KaryviaError) as caught:
        next_after("0 0 30 2 *", "2026-08-15T09:00:00")
    assert caught.value.detail["expr"] == "0 0 30 2 *"


def test_naive_moment_is_rejected() -> None:
    """不带时区的时刻算不出下一次——跨时区的任务表在两台机器上会排出不同的时间表。"""
    with pytest.raises(KaryviaError):
        parse_expr("* * * * *").next_after(datetime(2026, 8, 15, 9, 0))


def test_search_bound_covers_a_leap_year_gap() -> None:
    """上界要足够跨过闰年那 4 年，否则 `0 0 29 2 *` 会被误判成永不匹配。"""
    assert MAX_SEARCH_DAYS > 366 * 4


# ------------------------------------------------------------------------------ DST


def test_spring_forward_skips_a_nonexistent_wall_clock() -> None:
    """2026-03-08 02:30 在跳表时区里不存在，那天的 `30 2 * * *` 不运行。

    **不挪到 03:30**：那会让「每天 02:30」在那一天变成一个没人要求过的时刻。
    """
    zone = FakeDst()
    result = next_after("30 2 * * *", "2026-03-07T03:00:00", zone)
    assert (result.year, result.month, result.day) == (2026, 3, 9)


def test_fall_back_fires_once() -> None:
    """2026-11-01 01:30 在跳表时区里出现两次，只跑第一次（`fold=0`）。"""
    zone = FakeDst()
    first = next_after("30 1 * * *", "2026-10-31T12:00:00", zone)
    assert (first.month, first.day, first.hour, first.minute) == (11, 1, 1, 30)
    # 从第一次之后再推进：下一次必须是第二天，而不是同一天的第二个 01:30。
    second = parse_expr("30 1 * * *").next_after(first)
    assert (second.month, second.day) == (11, 2)


def test_hourly_stays_strictly_increasing_across_fall_back() -> None:
    """回表那天逐小时推进，**瞬间必须严格递增**——否则调度循环会原地打转。"""
    zone = FakeDst()
    cursor = next_after("0 * * * *", "2026-10-31T23:30:00", zone)
    for _ in range(30):
        following = parse_expr("0 * * * *").next_after(cursor)
        assert following > cursor
        cursor = following
