"""把结构化消息附件投影成当前文本模型可读的内容。

职责：以确定性文本表示附件引用，使文本模型与 Memory 能知道一条消息携带了哪些文件。
不负责：读取文件、解析媒体、决定模型原生多模态格式或持久化；这些分别属于插件、Model
Provider 与 Session 契约。

投影是消费视图，不是存储格式。Session 始终保存结构化 `AttachmentRef`，因此以后增加原生
多模态模型映射时可以替换这里，而不必迁移 locator、文件名等字段。
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from karyvia.contracts import AttachmentRef

__all__ = ["render_message_content"]


def render_message_content(content: str, attachments: Sequence[AttachmentRef]) -> str:
    """合并正文与附件元数据，不读取文件内容。"""
    if not attachments:
        return content
    lines = ["<attachments>"]
    for item in attachments:
        payload: dict[str, str | int] = {
            "source": item.source.value,
            "locator": item.locator,
            "media_type": item.media_type,
        }
        if item.size_bytes is not None:
            payload["size_bytes"] = item.size_bytes
        if item.filename is not None:
            payload["filename"] = item.filename
        lines.append(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    lines.append("</attachments>")
    attachment_text = "\n".join(lines)
    return f"{content}\n\n{attachment_text}" if content else attachment_text
