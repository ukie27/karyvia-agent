"""线格式翻译：远端 schema → `ToolSpec.parameters`，远端结果 → 给模型的文本。全是纯函数。

职责：把 MCP 交出来的两样东西（`inputSchema` 与 `CallToolResult`）翻成契约认识的形状。
不负责：连接（`client.py`）、命名（`naming.py`）、构造 `ToolResult`（`tool.py`）。

两条决定了本模块形状的规则：

- **参数 schema 原样透传，只补最外层的 `type: object`**。kernel 的
  `ToolInvoker._compile()` 会拿它做真正的校验（`jsonschema`），我们在中间改写它的语义
  只会让「模型看到的约束」与「实际生效的约束」分叉。补 `type` 是因为契约要求
  `spec.parameters["type"] == "object"`，而不少 server 省略它。
- **非文本内容部件说明它存在，而不是假装没有**。`ToolResult.content` 是纯文本
  （契约层没有多模态槽位），因此图像与资源引用只能变成一行说明。静默丢掉它们会让模型
  以为工具什么都没返回。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from karyvia.contracts import JsonSchema, JsonValue

from .session import RemoteResult

__all__ = ["describe_tool", "render_result", "summarise_parts", "tool_parameters", "truncate"]

#: 截断标记。`{shown}` / `{total}` 是字符数。与 `web` 插件那份逐字相同——`R4` 让两个
#: 独立发行包无法共享它，重复比耦合便宜（`AGENTS.md` 原则 5）。
_TRUNCATION_MARKER: Final = "\n… [truncated: 已显示 {shown}/{total} 字符]"

_EMPTY_SCHEMA: Final[JsonSchema] = {"type": "object", "properties": {}}


def tool_parameters(raw: JsonValue | None) -> JsonSchema:
    """把远端 `inputSchema` 变成一份可用的参数 schema。

    读不懂时退回一个**空对象 schema**而不是拒绝这个工具：一个不声明参数的 server 是
    合法的（不少工具确实不收参数），而拒绝它会让用户看着 server 里明明有的工具在
    `karyvia capabilities` 里消失。真正写错的 schema 会在 `ToolInvoker._compile()` 那里报出来。
    """
    if not isinstance(raw, Mapping):
        return dict(_EMPTY_SCHEMA)
    schema: dict[str, JsonValue] = dict(raw)
    # 契约要求最外层是 object；不少 server 省略它。补的是**缺失**，不是改写。
    if schema.get("type") != "object":
        schema["type"] = "object"
    return schema


def describe_tool(server: str, remote_name: str, description: str) -> str:
    """给模型看的工具说明。

    **带上它来自哪个 server 与原名**：模型看到的是归一化之后的本地名，而用户在
    server 那边看到的是原名。出问题时两边要能对得上。
    """
    body = description.strip() or "（该 MCP server 未提供说明）"
    return f"{body}\n\n[来自 MCP server「{server}」的工具 {remote_name}]"


def render_result(result: RemoteResult, limit: int) -> tuple[str, bool]:
    """把一次远端结果渲染成给模型的文本，返回 `(文本, 是否截断)`。"""
    parts = [result.text.strip()] if result.text.strip() else []
    parts.extend(result.attachments)
    if not parts:
        parts.append("（该工具没有返回任何内容）")
    return truncate("\n".join(parts), limit)


def truncate(text: str, limit: int) -> tuple[str, bool]:
    """收进 `limit` 字符内，返回 `(文本, 是否截断)`。

    **标记算在上限里**：返回值的长度恒 ≤ `limit`。下游是契约的
    `MAX_TOOL_RESULT_LENGTH`，那里超一个字符就构造失败。
    """
    total = len(text)
    if total <= limit:
        return text, False
    keep = limit - len(_TRUNCATION_MARKER.format(shown=limit, total=total))
    if keep <= 0:
        return "", True
    return text[:keep] + _TRUNCATION_MARKER.format(shown=keep, total=total), True


def summarise_parts(parts: Sequence[Mapping[str, JsonValue]]) -> tuple[str, tuple[str, ...]]:
    """把 MCP 的 `content` 数组拆成 `(文本, 非文本部件的说明)`。

    这里认识的部件类型只有 `text` / `image` / `audio` / `resource` 四种，其余按 `type`
    字段原样报出来——**不认识不等于不存在**，一行「有一个 X 类型的部件」比静默丢掉它
    更接近事实。
    """
    texts: list[str] = []
    attachments: list[str] = []
    for part in parts:
        kind = part.get("type")
        if kind == "text":
            value = part.get("text")
            if isinstance(value, str):
                texts.append(value)
            continue
        if kind == "resource":
            resource = part.get("resource")
            uri = resource.get("uri") if isinstance(resource, Mapping) else None
            attachments.append(f"[资源：{uri if isinstance(uri, str) else '未命名'}]")
            continue
        label = kind if isinstance(kind, str) and kind else "未知"
        mime = part.get("mimeType")
        suffix = f"，{mime}" if isinstance(mime, str) and mime else ""
        attachments.append(f"[{label} 内容部件{suffix}，本工具只能返回文本]")
    return "\n".join(texts), tuple(attachments)
