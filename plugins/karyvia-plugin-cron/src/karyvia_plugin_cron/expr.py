"""5 字段 cron 表达式的解析与「下一次匹配时刻」计算。**纯函数，不碰时钟也不碰 IO。**

职责：把 `"0 9 * * 1-5"` 解析成可判定的字段集合，并在给定的带时区时刻之后找出下一次匹配。
不负责：任务的其余两种调度形态（`schedule.py`）、时区名字到 `tzinfo` 的解析
（`settings.py`）、任务存储（`store.py`）。

**为什么自己写而不依赖 croniter。** CI 用 `--no-deps` 装插件（`AGENTS.md` 的开发命令段
写着这是刻意的），依赖 croniter 会让所有涉及表达式的用例在 CI 环境里跑不起来；而 5 字段
cron 的语法是可穷举的，算法也就是「按位判定 + 逐日推进」。这与  拒掉四张按模型名
gating 的表是同一档判断：能用一段可测试的纯函数表达的东西，不值得换一个依赖。

**支持的语法**：`*`、`n`、`a-b`、`a-b/n`、`*/n`、逗号列表，以及月份与星期的三字母英文名
（`JAN`…`DEC` / `SUN`…`SAT`，大小写不敏感）。星期允许 `0-7`，`7` 与 `0` 同为周日。

**刻意不支持** croniter 的扩展语法：`L`（月末）、`W`（最近工作日）、`#`（第 n 个星期几）、
`?`、秒级第 6 字段、`@daily` 这类别名。它们各自都要一套单独的判定，而本插件的用户可以用
「一条 `every` 任务 + 模型自己判断今天该不该做」表达同样的意图。**不支持就报错**，
不静默把 `L` 当字面量吞掉。

**日与星期两个字段都被限定时取并集**，这是 POSIX cron 的传统语义：
`0 0 13 * 5` 是「每月 13 号**或**每个周五」，不是「13 号且是周五」。

**两条与 DST 有关的判定**（时区由调用方经 `moment.tzinfo` 带进来，本模块不解析名字）：

- **不存在的墙钟时刻被跳过。** 春季跳表那天 02:30 不存在，`0 2 30 * *` 这类任务当天就不
  运行——把它挪到 03:30 会让「每天 02:30」在那一天变成一个没人要求过的时刻。
- **重复出现的墙钟时刻只取第一次**（`fold=0`）。秋季回表那天 01:30 出现两次，跑两遍
  提醒是错的。

返回值恒**严格晚于**传入时刻（按瞬间比较，不是按墙钟），这条由 `next_after` 的循环保证。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Final

from karyvia.contracts import ErrorCode, KaryviaError

__all__ = [
    "FIELD_COUNT",
    "MAX_SEARCH_DAYS",
    "CronExpr",
    "parse_expr",
]

#: 字段个数。第 6 字段（秒）不支持——见模块 docstring。
FIELD_COUNT: Final = 5

#: 逐日搜索的上界。闰年 2 月 29 日最坏要跨将近 4 年，取 8 年留足余量；到上界还没找到
#: 就说明表达式**永远不匹配**（`0 0 30 2 *`），那要显式报错而不是返回一个假的时刻。
MAX_SEARCH_DAYS: Final = 366 * 8

_MONTH_NAMES: Final[dict[str, int]] = {
    name: index
    for index, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
        start=1,
    )
}
#: 星期名。`0` 是周日（POSIX cron当前约定），因此 `sun` 排在最前。
_WEEKDAY_NAMES: Final[dict[str, int]] = {
    name: index
    for index, name in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))
}

_BAD_FIELD_COUNT: Final = "cron 表达式必须是 5 个字段：分 时 日 月 周。"
_BAD_FIELD: Final = "cron 表达式的某个字段不合法。"
_BAD_STEP: Final = "cron 表达式的步长必须是正整数。"
_BAD_RANGE: Final = "cron 表达式的范围起点不能大于终点。"
_OUT_OF_BOUNDS: Final = "cron 表达式的取值超出了该字段的允许范围。"
_UNSUPPORTED: Final = "不支持这种 cron 扩展语法（L / W / # / ? / @别名 / 秒字段）。"
_NEVER_MATCHES: Final = "这个 cron 表达式永远不会匹配任何日期。"
_NAIVE_MOMENT: Final = "计算下一次运行时刻必须传入带时区的时间。"

#: 出现即拒绝的字符。**只列不可能出现在名字里的那几个**——`L` 与 `W` 不在这里，
#: 因为 `jul` 与 `wed` 都含它们，全文扫描会把合法的月份与星期名一并拒掉。
#: `L` / `W` 的判定改在 `_parse_value` 的取值粒度上。
_UNSUPPORTED_CHARS: Final = frozenset("#?@")

#: croniter 的取值级扩展语法：`L`（月末）、`W`（最近工作日）、`15W`、`5L`。
_EXTENSION_TOKEN: Final = re.compile(r"^\d*[lw]+$")


@dataclass(frozen=True, slots=True)
class CronExpr:
    """一条解析好的 5 字段表达式。字段是取值集合，判定因此只是集合成员测试。"""

    source: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    #: 日字段是否被限定（不是 `*`）。与 `weekday_restricted` 一起决定用并集还是交集。
    day_restricted: bool
    #: 星期字段是否被限定。
    weekday_restricted: bool

    def matches(self, moment: datetime) -> bool:
        """本时刻（按墙钟的分钟粒度）是否匹配。秒与微秒不参与判定。"""
        if moment.month not in self.months:
            return False
        if moment.hour not in self.hours or moment.minute not in self.minutes:
            return False
        return self._date_matches(moment)

    def next_after(self, moment: datetime) -> datetime:
        """严格晚于 `moment` 的下一次匹配时刻，秒与微秒为 0。

        **按瞬间严格递增**：DST 回表那天同一个墙钟时刻会出现两次，只取第一次
        （`fold=0`），因此「下一次」永远真的在后面，调度循环不会原地打转。

        **异常约定**：`moment` 不带时区抛 `INPUT_MALFORMED`；表达式永远不匹配抛
        `INPUT_MALFORMED`（`0 0 30 2 *`）。
        """
        if moment.tzinfo is None:
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED, _NAIVE_MOMENT, detail={"expr": self.source}
            )
        zone = moment.tzinfo
        cursor = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
        naive_cursor = cursor.replace(tzinfo=None)
        for _ in range(MAX_SEARCH_DAYS):
            candidate = self._first_slot_on_or_after(naive_cursor)
            if candidate is None:
                # 这一天已经没有更晚的时间槽了，从下一天的 00:00 重新找。
                naive_cursor = (naive_cursor + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            localized = _localize(candidate, zone)
            if localized is not None and localized > moment:
                return localized
            # 不存在的墙钟时刻（春季跳表）或没有向前推进：跳过这一分钟继续找。
            naive_cursor = candidate + timedelta(minutes=1)
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED, _NEVER_MATCHES, detail={"expr": self.source}
        )

    # ------------------------------------------------------------------ 内部

    def _date_matches(self, moment: datetime) -> bool:
        """日 / 星期两个字段的判定。两者都被限定时取并集（见模块 docstring）。"""
        day_hit = moment.day in self.days
        # `weekday()` 是周一=0，cron 是周日=0。
        weekday_hit = ((moment.weekday() + 1) % 7) in self.weekdays
        if self.day_restricted and self.weekday_restricted:
            return day_hit or weekday_hit
        return day_hit and weekday_hit

    def _first_slot_on_or_after(self, naive: datetime) -> datetime | None:
        """本（naive）日期上不早于 `naive` 的第一个匹配时刻，没有则 `None`。

        日期本身不匹配时直接返回 `None`——调用方会推到下一天，因此这里不需要自己找日期。
        """
        if naive.month not in self.months or not self._date_matches(naive):
            return None
        for hour in sorted(hour for hour in self.hours if hour >= naive.hour):
            floor = naive.minute if hour == naive.hour else 0
            minutes = [minute for minute in self.minutes if minute >= floor]
            if minutes:
                return naive.replace(hour=hour, minute=min(minutes))
        return None


def parse_expr(source: str) -> CronExpr:
    """解析一条 5 字段 cron 表达式。

    **异常约定**：任何形状问题抛 `INPUT_MALFORMED`，`detail` 里带上出问题的字段与位置
    ——「表达式不合法」这句话本身帮不了写它的人。
    """
    text = source.strip()
    fields = text.split()
    if len(fields) != FIELD_COUNT:
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            _BAD_FIELD_COUNT,
            detail={"expr": text, "fields": len(fields)},
        )
    unsupported = sorted(_UNSUPPORTED_CHARS.intersection(text))
    if unsupported:
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            _UNSUPPORTED,
            detail={"expr": text, "characters": unsupported},
        )
    minute, hour, day, month, weekday = fields
    weekdays = _parse_field(weekday, "weekday", 0, 7, _WEEKDAY_NAMES)
    return CronExpr(
        source=text,
        minutes=_parse_field(minute, "minute", 0, 59, {}),
        hours=_parse_field(hour, "hour", 0, 23, {}),
        days=_parse_field(day, "day", 1, 31, {}),
        months=_parse_field(month, "month", 1, 12, _MONTH_NAMES),
        # `7` 与 `0` 都是周日，归一成 `0`：判定侧因此只有一种周日。
        weekdays=frozenset(0 if value == 7 else value for value in weekdays),
        day_restricted=day.strip() != "*",
        weekday_restricted=weekday.strip() != "*",
    )


def _parse_field(
    field: str, name: str, low: int, high: int, names: dict[str, int]
) -> frozenset[int]:
    """解析一个字段。空字段、空列表项与越界取值都是错误。"""
    text = field.strip()
    if not text:
        raise KaryviaError(ErrorCode.INPUT_MALFORMED, _BAD_FIELD, detail={"field": name})
    values: set[int] = set()
    for part in text.split(","):
        values.update(_parse_part(part.strip(), name, low, high, names))
    return frozenset(values)


def _parse_part(part: str, name: str, low: int, high: int, names: dict[str, int]) -> set[int]:
    """解析一个逗号项：`*` / `n` / `a-b` / `*/n` / `a-b/n`。"""
    if not part:
        raise KaryviaError(ErrorCode.INPUT_MALFORMED, _BAD_FIELD, detail={"field": name})
    body, _, step_text = part.partition("/")
    step = 1
    if step_text or part.endswith("/"):
        step = _parse_step(step_text, name)
    if body == "*":
        start, stop = low, high
    elif "-" in body:
        start, stop = _parse_range(body, name, low, high, names)
    else:
        value = _parse_value(body, name, low, high, names)
        # `5/15` 的语义是「从 5 起、步长 15、直到字段上界」，与 `5-59/15` 相同。
        start, stop = (value, high) if step > 1 else (value, value)
    return set(range(start, stop + 1, step))


def _parse_range(
    body: str, name: str, low: int, high: int, names: dict[str, int]
) -> tuple[int, int]:
    start_text, _, stop_text = body.partition("-")
    start = _parse_value(start_text, name, low, high, names)
    stop = _parse_value(stop_text, name, low, high, names)
    if start > stop:
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED, _BAD_RANGE, detail={"field": name, "part": body}
        )
    return start, stop


def _parse_step(text: str, name: str) -> int:
    if not text.isdigit() or int(text) <= 0:
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED, _BAD_STEP, detail={"field": name, "step": text}
        )
    return int(text)


def _parse_value(text: str, name: str, low: int, high: int, names: dict[str, int]) -> int:
    """解析一个取值：十进制数字，或该字段允许的三字母名。"""
    token = text.strip().lower()
    if token in names:
        return names[token]
    if _EXTENSION_TOKEN.match(token):
        # `L` / `W` / `15W` 这类 croniter 扩展：单列一条错误，别让它掉进「字段不合法」
        # 里——用户要知道的是「不支持」而不是「写错了」。
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            _UNSUPPORTED,
            detail={"field": name, "value": text.strip()},
        )
    if not token.isdigit():
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED, _BAD_FIELD, detail={"field": name, "value": text}
        )
    value = int(token)
    if not low <= value <= high:
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            _OUT_OF_BOUNDS,
            detail={"field": name, "value": value, "minimum": low, "maximum": high},
        )
    return value


def _localize(naive: datetime, zone: tzinfo) -> datetime | None:
    """把一个墙钟时刻挂到时区上。**不存在的时刻返回 `None`**（春季跳表的那一小时）。

    判据是往返：把结果**经 UTC** 换算回同一时区，墙钟对不上就说明这个墙钟时刻不存在。
    **必须绕 UTC**：`datetime.astimezone(tz)` 在 `tz is self.tzinfo` 时直接返回自身
    （CPython 的短路），不绕一下这个判据就是恒真的。
    `fold=0` 让回表那天重复出现的墙钟只取第一次。
    """
    attached = naive.replace(tzinfo=zone, fold=0)
    round_trip = attached.astimezone(UTC).astimezone(zone)
    if (round_trip.hour, round_trip.minute, round_trip.day, round_trip.month) != (
        naive.hour,
        naive.minute,
        naive.day,
        naive.month,
    ):
        return None
    return attached
