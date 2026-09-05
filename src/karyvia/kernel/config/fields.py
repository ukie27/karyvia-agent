"""配置字段的类型、默认值与逐字段校验（技术方案 §6.7、`CFG-001`）。

职责：定义 `FieldKind` / `FieldSpec` 两个声明式积木，把一个值按 spec 校验成
`(采用的值, 问题或 None)`，给拼错的键一个近似建议，并把**已校验**的值按类型收窄回来。
不负责：知道有哪些字段（那张表在 `schema.py` 的 `SECTION_SPECS`）、组装小节、读取任何
来源。本模块不认识任何具体配置项，因此可以被字段表反过来使用而不成环。

从 `schema.py` 拆出来是因为那边贴着 `kernel/` 的 500 行上限：字段表在长（`D13` 加了
`routing` 小节，`D24` 加了顶层 `$schema`），而**校验积木不该随字段数增长**。分界线是
「认不认识具体字段」——本模块一个字段名都不认识，`schema.py` 除了字段什么都不放。
六个 `*_at()` 收窄器（`D24` 从 `schema.py` 搬过来）同样一个字段名都不认识：它们只回答
「把一个已校验的 `JsonValue` 收窄成 `int` / `str` / `bool` / `tuple[str, ...]`」。

**`detail` 里绝不放配置值**，只放指针与类型名，理由见 `schema.py` 的模块 docstring。
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Mapping, Sequence

from ...contracts import ErrorCode, KaryviaError

if TYPE_CHECKING:
    from ...contracts import JsonValue

__all__ = [
    "FieldKind",
    "FieldSpec",
    "bool_at",
    "coerce_value",
    "int_at",
    "issue",
    "opt_int_at",
    "opt_str_at",
    "str_at",
    "str_tuple_at",
    "suggest",
]


class FieldKind(StrEnum):
    """字段类型。取值刻意少：配置里出现第七种形状时应当先想清楚它是否真该进配置。"""

    BOOL = "bool"
    POSITIVE_INT = "positive_int"
    #: 允许 `null`，含义由字段自己的 docstring 给出（不等于「无限制」）。
    OPTIONAL_POSITIVE_INT = "optional_positive_int"
    STR = "str"
    OPTIONAL_STR = "optional_str"
    STR_LIST = "str_list"


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """一个字段的类型与默认值。

    `choices` 只对 `STR` 有意义：取值受限的字段（如并发策略）必须在**校验时**就带着指针
    报错，否则错误会推迟到构造调度器那一刻，那时既没有指针也没有「你可以写哪几个」。
    """

    kind: FieldKind
    default: JsonValue
    choices: tuple[str, ...] = ()


def suggest(unknown: str, known: Sequence[str]) -> str:
    """给拼错的键一个近似建议。

    这是「只认 snake_case」这条规则可教的关键：`maxIterations` 会得到
    「是否想写 max_iterations？」，而不是一句干巴巴的「未知字段」。
    """
    close = difflib.get_close_matches(unknown, known, n=1, cutoff=0.6)
    if close:
        return f"是否想写 {close[0]}？"
    return f"删除该键；该小节可用的字段有：{', '.join(sorted(known))}。"


def issue(code: ErrorCode, message: str, pointer: str, **detail: JsonValue) -> KaryviaError:
    """构造一处带位置的问题。**detail 里只有指针与类型名，绝不含配置值。**"""
    return KaryviaError(code, message, detail={"pointer": pointer, **detail})


def _coerce_str(
    value: JsonValue, spec: FieldSpec, pointer: str
) -> tuple[JsonValue, KaryviaError | None]:
    """`STR` / `OPTIONAL_STR` 分支。单独成函数是为了给 `choices` 留出位置而不撑爆
    `coerce_value` 的圈复杂度上限。"""
    if value is None and spec.kind is FieldKind.OPTIONAL_STR:
        return (None, None)
    if not isinstance(value, str):
        return (spec.default, issue(ErrorCode.CONFIG_INVALID, "该字段必须是字符串。", pointer))
    if spec.choices and value not in spec.choices:
        # 取值本身是枚举名而不是用户数据，放进消息里不会泄漏任何东西。
        return (
            spec.default,
            issue(
                ErrorCode.CONFIG_INVALID,
                f"该字段只能取 {'、'.join(spec.choices)} 之一。",
                pointer,
            ),
        )
    return (value, None)


def coerce_value(
    value: JsonValue, spec: FieldSpec, pointer: str
) -> tuple[JsonValue, KaryviaError | None]:
    """按 spec 校验一个值。返回 `(采用的值, 问题或 None)`。

    出问题时返回 spec 的默认值，让校验能继续走完剩下的字段——一次报全比逐条中断有用。
    """
    kind = spec.kind
    if kind is FieldKind.BOOL:
        if isinstance(value, bool):
            return (value, None)
        return (spec.default, issue(ErrorCode.CONFIG_INVALID, "该字段必须是布尔值。", pointer))

    if kind in (FieldKind.POSITIVE_INT, FieldKind.OPTIONAL_POSITIVE_INT):
        if value is None and kind is FieldKind.OPTIONAL_POSITIVE_INT:
            return (None, None)
        # bool 是 int 的子类，但 `max_iterations: true` 显然是写错了。
        if isinstance(value, bool) or not isinstance(value, int):
            return (spec.default, issue(ErrorCode.CONFIG_INVALID, "该字段必须是整数。", pointer))
        if value <= 0:
            return (spec.default, issue(ErrorCode.CONFIG_INVALID, "该字段必须是正整数。", pointer))
        return (value, None)

    if kind in (FieldKind.STR, FieldKind.OPTIONAL_STR):
        return _coerce_str(value, spec, pointer)

    # STR_LIST
    if isinstance(value, str) or not isinstance(value, Sequence):
        return (spec.default, issue(ErrorCode.CONFIG_INVALID, "该字段必须是字符串数组。", pointer))
    items = list(value)
    if not all(isinstance(item, str) for item in items):
        return (
            spec.default,
            issue(ErrorCode.CONFIG_INVALID, "该数组的每一项都必须是字符串。", pointer),
        )
    return (tuple(items), None)


# --------------------------------------------------------------------- 类型收窄
#
# 六个「取一个**已校验**的值」的收窄器。`coerce_value()` 已经保证了形状，这里只是把
# `JsonValue` 收回具体类型——`schema.py` 的小节构造要具名传参，而展开一个
# `dict[str, JsonValue]` 会让每个字段都退化成 `JsonValue`。
#
# 它们与本模块其余部分同属一条分界线：一个字段名都不认识。


def int_at(values: Mapping[str, JsonValue], key: str, fallback: int) -> int:
    """取一个已校验的整数。`bool` 是 `int` 的子类，但它不是这里要的东西。"""
    value = values.get(key, fallback)
    return value if isinstance(value, int) and not isinstance(value, bool) else fallback


def opt_int_at(values: Mapping[str, JsonValue], key: str) -> int | None:
    value = values.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def str_at(values: Mapping[str, JsonValue], key: str, fallback: str) -> str:
    value = values.get(key, fallback)
    return value if isinstance(value, str) else fallback


def opt_str_at(values: Mapping[str, JsonValue], key: str) -> str | None:
    value = values.get(key)
    return value if isinstance(value, str) else None


def bool_at(values: Mapping[str, JsonValue], key: str, fallback: bool) -> bool:
    value = values.get(key, fallback)
    return value if isinstance(value, bool) else fallback


def str_tuple_at(values: Mapping[str, JsonValue], key: str) -> tuple[str, ...]:
    value = values.get(key, ())
    if isinstance(value, str) or not isinstance(value, Sequence):
        return ()
    return tuple(item for item in value if isinstance(item, str))
