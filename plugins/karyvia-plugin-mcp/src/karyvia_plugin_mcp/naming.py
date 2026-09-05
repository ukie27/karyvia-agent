"""本地工具名的构造：`(server, 远端工具名)` → `<prefix>.<server>.<tool>`。全是纯函数。

职责：把远端名字归一化成契约允许的形状，并把归一化撞车**报出来而不是静默合并**。
不负责：注册（`__init__.py`）、连接（`client.py`）。

**为什么必须归一化**：契约的工具名式样是 `^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)*$`
（`contracts/tool.py`），而 MCP 的工具名常带 `-`、大写与数字开头。这与 `D32` 的
anthropic 插件那条「工具名必须编码」是同一类问题，但**结论相反**：那里的 `.` ↔ `-`
是无碰撞双射，这里的归一化**会**丢信息（`get-file` 与 `get_file` 撞成同一个名字）。
因此这里必须处理撞车，而那里不必。

**撞车的处理是各方都不生效**，与 `kernel/registry/resolution.py` 对同名冲突的判定一致：
选任何一边都是替用户做决定，而这里连「哪一边是用户想要的」都无从推断。撞车会记进
`NameAssignment.collisions`，由调用方写日志——静默丢掉一个工具会让用户在 `karyvia capabilities`
里怎么找都找不到它。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from .session import RemoteTool

__all__ = [
    "DEFAULT_PREFIX",
    "NameAssignment",
    "assign_names",
    "normalise_segment",
    "tool_name",
]

DEFAULT_PREFIX: Final = "mcp"

#: 契约允许的单个名字段。归一化的目标就是让每一段都匹配它。
_SEGMENT: Final = re.compile(r"^[a-z][a-z0-9_]*$")

_NOT_ALLOWED: Final = re.compile(r"[^a-z0-9_]+")
_RUNS: Final = re.compile(r"_{2,}")


def normalise_segment(value: str) -> str:
    """把一个远端名字段归一成契约允许的形状。归一不出来时返回空串。

    规则按顺序：小写 → 非 `[a-z0-9_]` 一律换成 `_` → 压缩连续下划线 → 去掉首尾下划线
    → 数字开头补一个 `n` 前缀。**不做音译、不截断**：截断会把两个长名字变成同一个，
    而那正是本模块要报出来的那种撞车。
    """
    folded = _RUNS.sub("_", _NOT_ALLOWED.sub("_", value.strip().lower())).strip("_")
    if not folded:
        return ""
    if folded[0].isdigit():
        folded = f"n{folded}"
    return folded if _SEGMENT.match(folded) else ""


def tool_name(prefix: str, server: str, tool: str) -> str:
    """拼出本地工具名。任一段归一不出来时返回空串。"""
    segments = [normalise_segment(part) for part in (prefix, server, tool)]
    return "" if not all(segments) else ".".join(segments)


@dataclass(frozen=True, slots=True)
class NameAssignment:
    """一次命名的结果。

    `assigned` 是本地名 → 远端工具；`collisions` 是归一化之后撞在一起的本地名，
    值是撞在那个名字上的**全部**远端原名（每个都没有生效）。
    `rejected` 是连归一化都过不去的远端原名。
    """

    assigned: Mapping[str, RemoteTool] = field(default_factory=dict[str, RemoteTool])
    collisions: Mapping[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]])
    rejected: tuple[str, ...] = ()


def assign_names(
    prefix: str, server: str, tools: Sequence[RemoteTool]
) -> NameAssignment:
    """给一个 server 的工具表分配本地名。

    **撞车的各方都不进 `assigned`**：选任何一边都是替用户做决定，而模型拿到一个
    「名字对得上、行为却是另一个工具」的调用比少一个工具危险得多。
    """
    buckets: dict[str, list[RemoteTool]] = {}
    rejected: list[str] = []
    for tool in tools:
        local = tool_name(prefix, server, tool.name)
        if not local:
            rejected.append(tool.name)
            continue
        buckets.setdefault(local, []).append(tool)
    assigned = {name: group[0] for name, group in buckets.items() if len(group) == 1}
    collisions = {
        name: tuple(sorted(tool.name for tool in group))
        for name, group in buckets.items()
        if len(group) > 1
    }
    return NameAssignment(
        assigned=assigned, collisions=collisions, rejected=tuple(sorted(rejected))
    )
