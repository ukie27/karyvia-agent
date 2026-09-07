"""markdown → 飞书卡片元素。

职责：把一段 markdown 拆成 `markdown` / `div` / `table` 三种卡片元素，并**按「一张卡片
最多一个表格」拆成多组**。纯函数，零 IO。
不负责：格式判定（`outbound.py`）、真的发出去（`client.py`）、流式（`stream.py`）。

**一表一卡是平台的硬约束，不是排版偏好**：飞书对含多个表格的卡片直接返回 API error 11310。
因此一段带三个表格的回答会变成三条卡片消息——每个表格都送达，而不是整条发不出去。

**代码块要先保护起来**：表格与标题的正则都是行首锚定的多行匹配，而代码块里完全可能有
`|---|` 或 `# 注释`。先把代码块换成哨兵（`\\x00CODE<i>\\x00`）、拆完再换回来，是 legacy
的做法，这里原样保留——它是这个文件里唯一一处「看起来绕但每一步都必要」的地方。

**表格单元格里的 markdown 标记要剥掉**：飞书的 table 元素不渲染 markdown，留着 `**` 会让
用户看到一堆星号。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from karyvia.contracts import JsonValue

__all__ = [
    "MAX_TABLES_PER_CARD",
    "build_elements",
    "split_by_table_limit",
    "strip_markdown_marks",
]

#: 飞书对一张卡片里的表格数量上限。超过即 API error 11310。**不是可调项。**
MAX_TABLES_PER_CARD: Final = 1

#: markdown 表格：表头 + 分隔行 + 至少一行数据。
_TABLE_RE: Final = re.compile(
    r"((?:^[ \t]*\|.+\|[ \t]*\n)(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
    re.MULTILINE,
)
_HEADING_RE: Final = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_CODE_BLOCK_RE: Final = re.compile(r"(```[\s\S]*?```)", re.MULTILINE)

_BOLD_RE: Final = re.compile(r"\*\*(.+?)\*\*")
_BOLD_UNDERSCORE_RE: Final = re.compile(r"__(.+?)__")
_ITALIC_RE: Final = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_STRIKE_RE: Final = re.compile(r"~~(.+?)~~")

#: 代码块哨兵。用 NUL 是因为它不可能出现在模型产出的 markdown 里。
_SENTINEL: Final = "\x00CODE{index}\x00"


def strip_markdown_marks(text: str) -> str:
    """剥掉粗体 / 斜体 / 删除线标记。飞书的表格单元格与标题不渲染 markdown。"""
    text = _BOLD_RE.sub(r"\1", text)
    text = _BOLD_UNDERSCORE_RE.sub(r"\1", text)
    text = _ITALIC_RE.sub(r"\1", text)
    return _STRIKE_RE.sub(r"\1", text)


def _protect_code_blocks(content: str) -> tuple[str, list[str]]:
    """把代码块换成哨兵。返回 `(替换后的文本, 代码块列表)`。"""
    protected = content
    blocks: list[str] = []
    for match in _CODE_BLOCK_RE.finditer(content):
        blocks.append(match.group(1))
        protected = protected.replace(match.group(1), _SENTINEL.format(index=len(blocks) - 1), 1)
    return protected, blocks


def _restore_code_blocks(
    elements: list[dict[str, JsonValue]], blocks: Sequence[str]
) -> list[dict[str, JsonValue]]:
    """把哨兵换回代码块。**只在 `markdown` 元素里换**——别的元素类型不承载正文。"""
    for index, block in enumerate(blocks):
        sentinel = _SENTINEL.format(index=index)
        for element in elements:
            if element.get("tag") != "markdown":
                continue
            content = element.get("content")
            if isinstance(content, str) and sentinel in content:
                element["content"] = content.replace(sentinel, block)
    return elements


def _parse_table(table_text: str) -> dict[str, JsonValue] | None:
    """一段 markdown 表格 → 飞书 table 元素。行数不够（缺表头或分隔行）返回 `None`。"""
    lines = [line.strip() for line in table_text.strip().split("\n") if line.strip()]
    if len(lines) < 3:
        return None

    def cells(line: str) -> list[str]:
        return [cell.strip() for cell in line.strip("|").split("|")]

    headers = [strip_markdown_marks(header) for header in cells(lines[0])]
    rows = [[strip_markdown_marks(cell) for cell in cells(line)] for line in lines[2:]]
    return {
        "tag": "table",
        # `page_size` 要大于等于行数，否则飞书会分页显示、后面几行用户看不见。
        "page_size": len(rows) + 1,
        "columns": [
            {"tag": "column", "name": f"c{index}", "display_name": header, "width": "auto"}
            for index, header in enumerate(headers)
        ],
        "rows": [
            {f"c{index}": row[index] if index < len(row) else "" for index in range(len(headers))}
            for row in rows
        ],
    }


def _split_headings(content: str) -> list[dict[str, JsonValue]]:
    """按标题切段。标题变成加粗的 `div`——飞书的卡片没有原生标题元素。"""
    protected, blocks = _protect_code_blocks(content)
    elements: list[dict[str, JsonValue]] = []
    cursor = 0
    for match in _HEADING_RE.finditer(protected):
        before = protected[cursor : match.start()].strip()
        if before:
            elements.append({"tag": "markdown", "content": before})
        text = strip_markdown_marks(match.group(2).strip())
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**{text}**" if text else ""}}
        )
        cursor = match.end()
    remaining = protected[cursor:].strip()
    if remaining:
        elements.append({"tag": "markdown", "content": remaining})
    if not elements:
        return [{"tag": "markdown", "content": content}]
    return _restore_code_blocks(elements, blocks)


def build_elements(content: str) -> list[dict[str, JsonValue]]:
    """一段 markdown → 卡片元素列表。

    先按表格切，段间的部分再按标题切；代码块全程被哨兵保护着（见模块 docstring）。
    解析不出来的表格原样留成 `markdown` 元素——**降级成「渲染得不好看」而不是「发不出去」**。
    """
    protected, blocks = _protect_code_blocks(content)
    elements: list[dict[str, JsonValue]] = []
    cursor = 0
    for match in _TABLE_RE.finditer(protected):
        before = protected[cursor : match.start()]
        if before.strip():
            elements.extend(_split_headings(before))
        elements.append(_parse_table(match.group(1)) or {"tag": "markdown", "content": match.group(1)})
        cursor = match.end()
    remaining = protected[cursor:]
    if remaining.strip():
        elements.extend(_split_headings(remaining))
    if not elements:
        return [{"tag": "markdown", "content": content}]
    return _restore_code_blocks(elements, blocks)


def split_by_table_limit(
    elements: Sequence[dict[str, JsonValue]], *, max_tables: int = MAX_TABLES_PER_CARD
) -> list[list[dict[str, JsonValue]]]:
    """按「一张卡片最多 N 个表格」把元素拆成多组。**空输入返回 `[[]]`**（一张空卡）。

    这不是字符数分段，是平台的结构约束（API error 11310）。拆开之后每个表格都送达；
    不拆的话整条消息发不出去，用户什么也看不到。
    """
    if not elements:
        return [[]]
    groups: list[list[dict[str, JsonValue]]] = []
    current: list[dict[str, JsonValue]] = []
    tables = 0
    for element in elements:
        if element.get("tag") == "table":
            if tables >= max_tables:
                if current:
                    groups.append(current)
                current = []
                tables = 0
            current.append(element)
            tables += 1
            continue
        current.append(element)
    if current:
        groups.append(current)
    return groups or [[]]


def card_payload(elements: Sequence[dict[str, JsonValue]]) -> dict[str, JsonValue]:
    """一组元素 → `interactive` 消息的卡片体（schema 1.0，`wide_screen_mode`）。"""
    return {
        "config": {"wide_screen_mode": True},
        "elements": list(elements),
    }
