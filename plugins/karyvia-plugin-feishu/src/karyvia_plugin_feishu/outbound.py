"""契约 `OutboundMessage` → 飞书消息体。

职责：**三选一的格式判定**、markdown → post 体、终态标记、超长回落分块。纯函数，零 IO。
不负责：卡片元素构建（`cards.py`）、流式时机（`stream.py`）、真的发出去（`client.py`）。

**格式级联的顺序就是「一条消息在飞书里长什么样」的全部定义**，逐条从 legacy
（`runtime.py:1592`）搬过来，一条没改也一条没换位置。换位置的后果都是可见的：把「长度」
放到「粗体」之前，一段 2000+ 的纯文本就会走卡片而不是 post；把「短文本」放到「链接」之前，
一条 20 字带链接的消息就会变成纯文本、链接不可点。

**`markdown_to_post` 的 `zh_cn` 是 post 消息体的必需结构，不是 i18n 选择。**
飞书的 post 体形状就是 `{"<locale>": {"content": [[…]]}}`，而 `zh_cn` 是唯一一个所有租户
都认的键。下一个人看到它会想「顺手做成配置吧」——不要，那会让消息在某些租户上发不出去。

`TERMINAL_MARKERS` 与 `builtins/cli_entry/console.py` 逐字相同。`R4` 够不着彼此，因此各写一份
并用对照用例防止漂移。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from karyvia.contracts import AttachmentRef, AttachmentSource, JsonValue, StreamState

__all__ = [
    "FORMAT_INTERACTIVE",
    "FORMAT_POST",
    "FORMAT_TEXT",
    "POST_MAX_LEN",
    "TERMINAL_MARKERS",
    "TEXT_MAX_LEN",
    "OutboundBody",
    "attachment_lines",
    "compose_body",
    "detect_format",
    "fallback_chunks",
    "marker_for",
    "markdown_to_post",
    "text_body",
]

FORMAT_TEXT: Final = "text"
FORMAT_POST: Final = "post"
FORMAT_INTERACTIVE: Final = "interactive"

#: 纯文本的长度上限；超过就用 post。**不做成配置**：它是「消息长什么样」的判定，
#: 做成旋钮会让「为什么这条是卡片」有 N 个答案。
TEXT_MAX_LEN: Final = 200

#: post 的长度上限；超过就用卡片。同上，不做成配置。
POST_MAX_LEN: Final = 2000

#: 卡片整条链失败时的回落分块上限。飞书对文本消息的实际容量远大于此，取 3500 是为了
#: 留出卡片 JSON 的开销余量（legacy 原值）。
FALLBACK_CHUNK_LEN: Final = 3500

#: 非完整答案的标记。与 `builtins/cli_entry/console.py` 的同名表逐字相同。
TERMINAL_MARKERS: Final[Mapping[StreamState, str]] = {
    StreamState.CANCELLED: "[已中断：以上是中断前已产生的内容]",
    StreamState.FAILED: "[本轮失败]",
}

#: 本插件尚未实现 workspace 文件上传时的明确降级文案。
_UNSENDABLE: Final = "[附件：{name}（本轮无法上传）]"

# 以下五个正则逐字来自 legacy `runtime.py:1560–1584`。改它们就是改格式判定。
_COMPLEX_MD_RE: Final = re.compile(
    r"```"  # 围栏代码块
    r"|^\|.+\|.*\n\s*\|[-:\s|]+\|"  # markdown 表格（表头 + 分隔行）
    r"|^#{1,6}\s+",  # 标题
    re.MULTILINE,
)
_SIMPLE_MD_RE: Final = re.compile(
    r"\*\*.+?\*\*"  # **粗体**
    r"|__.+?__"  # __粗体__
    r"|(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)"  # *斜体*（单星号）
    r"|~~.+?~~",  # ~~删除线~~
    re.DOTALL,
)
_MD_LINK_RE: Final = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")
_LIST_RE: Final = re.compile(r"^[\s]*[-*+]\s+", re.MULTILINE)
_OLIST_RE: Final = re.compile(r"^[\s]*\d+\.\s+", re.MULTILINE)


def detect_format(content: str) -> str:
    """选一种飞书消息格式。**七条判据，顺序不可换**（见模块 docstring）。"""
    stripped = content.strip()
    if _COMPLEX_MD_RE.search(stripped):
        return FORMAT_INTERACTIVE  # 代码块 / 表格 / 标题：post 渲染不了
    if len(stripped) > POST_MAX_LEN:
        return FORMAT_INTERACTIVE  # 长内容用卡片版式更可读
    if _SIMPLE_MD_RE.search(stripped):
        return FORMAT_INTERACTIVE  # 粗体 / 斜体 / 删除线：post 渲染不了
    if _LIST_RE.search(stripped) or _OLIST_RE.search(stripped):
        return FORMAT_INTERACTIVE  # post 的列表项渲染是坏的
    if _MD_LINK_RE.search(stripped):
        return FORMAT_POST  # post 支持 <a> 标签
    if len(stripped) <= TEXT_MAX_LEN:
        return FORMAT_TEXT  # 短纯文本发卡片是噪声
    return FORMAT_POST


def text_body(content: str) -> str:
    """`text` 消息体。飞书要的是 JSON 字符串而不是裸文本。"""
    return json.dumps({"text": content}, ensure_ascii=False)


def markdown_to_post(content: str) -> str:
    """markdown → post 消息体（JSON 字符串）。

    `[文本](链接)` 变成 `a` 标签，其余是 `text`；**每一行是一个段落**。
    空行留一个空 `text` 元素——飞书的 post 靠段落分行，去掉它整段会挤成一坨。

    **`zh_cn` 硬编码**：见模块 docstring，它是结构不是选择。
    """
    paragraphs: list[list[dict[str, JsonValue]]] = []
    for line in content.strip().split("\n"):
        elements: list[dict[str, JsonValue]] = []
        cursor = 0
        for match in _MD_LINK_RE.finditer(line):
            before = line[cursor : match.start()]
            if before:
                elements.append({"tag": "text", "text": before})
            elements.append({"tag": "a", "text": match.group(1), "href": match.group(2)})
            cursor = match.end()
        remaining = line[cursor:]
        if remaining:
            elements.append({"tag": "text", "text": remaining})
        if not elements:
            elements.append({"tag": "text", "text": ""})
        paragraphs.append(elements)
    return json.dumps({"zh_cn": {"content": paragraphs}}, ensure_ascii=False)


def marker_for(state: StreamState) -> str:
    """该终态要附加的标记；完整答案返回空串。"""
    return TERMINAL_MARKERS.get(state, "")


def attachment_lines(attachments: Sequence[AttachmentRef]) -> list[str]:
    """附件的正文呈现。

    `URL` 直接成行；其余来源（`WORKSPACE` / `OPAQUE` / `INLINE`）本轮上传不了，如实说一句。
    真正的飞书文件上传以后可以在本 Channel 插件内替换这条降级，不需要改变消息契约。
    """
    lines: list[str] = []
    for item in attachments:
        if item.source is AttachmentSource.URL:
            lines.append(item.locator)
            continue
        lines.append(_UNSENDABLE.format(name=item.filename or item.locator))
    return lines


def compose_body(content: str, attachments: Sequence[AttachmentRef], state: StreamState) -> str:
    """正文 + 附件行 + 终态标记，拼成最终要发的那段文本。

    **标记与正文在同一段里**：分开发会让被中断的半截答案孤零零留在上面，
    看起来像一个完整回答。
    """
    parts = [content] if content else []
    parts.extend(attachment_lines(attachments))
    marker = marker_for(state)
    if marker:
        parts.append(marker)
    return "\n\n".join(part for part in parts if part)


def fallback_chunks(text: str, limit: int = FALLBACK_CHUNK_LEN) -> list[str]:
    """卡片整条链失败时的回落分块。优先在最后一个换行处切。

    切点落在前半段之前就硬切——那说明这一块里根本没有合适的换行，为了「好看」把一块切成
    很小的一段只会多发几条消息。
    """
    if not text:
        return []
    if limit <= 0 or len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        head = remaining[:limit]
        cut = head.rfind("\n")
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return chunks


@dataclass(frozen=True, slots=True)
class OutboundBody:
    """一条要发出去的飞书消息：类型 + 已经序列化好的消息体。"""

    msg_type: str
    content: str


def plan_simple(body: str) -> OutboundBody:
    """把一段文本按格式级联拍成 `text` 或 `post`。

    **卡片不走这里**——它要先经 `cards.build_elements()` 拆成一到多张，
    因此由调用方（`channel.py`）在拿到 `FORMAT_INTERACTIVE` 时另走一条路。
    """
    if detect_format(body) == FORMAT_TEXT:
        return OutboundBody(FORMAT_TEXT, text_body(body))
    return OutboundBody(FORMAT_POST, markdown_to_post(body))
