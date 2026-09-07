"""入站正文抽取：飞书的各种消息体 → 一段模型看得懂的文本。

职责：`text` / `post` / `interactive` / `share_*` / `system` / `merge_forward` 六类消息体的
文本抽取；从 post 里捞出内嵌的 `image_key`。**纯函数，零 IO，不认识 SDK 也不认识配置。**
不负责：门控与归一化（`normalize.py`）、@ 占位符（`mentions.py`）、下载任何东西。

**这是原样搬运 + 类型收窄**：legacy `runtime.py:168–488` 那 320 行的判定一条没改，
只是把 `dict[str, Any]` 换成对 `JsonValue` 的窄化（`_as_object` / `_as_list`），并把
legacy 那条 11 分支的 if/elif 链换成 `_TAG_HANDLERS` 派发表（圈复杂度上限逼的，顺带让
「支持哪些 tag」变成一张读得完的表）。**那张表就是「一张卡片在模型眼里长什么样」的全部
定义**——改它就是改模型看到的东西。

**未知 tag 递归进 `elements`**（`_tag_nested`）：飞书一直在加新的卡片元素，而它们几乎都
把子元素放在那里。递归比列一张永远不全的白名单更耐用。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Final, cast

from karyvia.contracts import JsonValue

__all__ = [
    "MSG_TYPE_LABELS",
    "extract_interactive",
    "extract_post",
    "extract_share_card",
    "parse_content",
]

#: 只有标签、没有正文的消息类型。`sticker` 之类东西对模型的全部信息就是「这里有个表情」。
MSG_TYPE_LABELS: Final[Mapping[str, str]] = {
    "image": "[image]",
    "audio": "[audio]",
    "file": "[file]",
    "media": "[video]",
    "sticker": "[sticker]",
}

#: post 消息体的本地化键，按优先级。找不到时回落到「任意一个字典子节点」。
_POST_LOCALES: Final[tuple[str, ...]] = ("zh_cn", "en_us", "ja_jp")


def _as_object(value: JsonValue | None) -> Mapping[str, JsonValue] | None:
    return value if isinstance(value, Mapping) else None


def _as_list(value: JsonValue | None) -> Sequence[JsonValue] | None:
    return value if isinstance(value, list) else None


def _text_of(value: JsonValue | None) -> str:
    """取一个「可能是字符串、也可能是 `{content|text: …}` 对象」的文本。

    飞书的卡片元素在这一点上不自洽：同一个 `text` 字段在 `div` 里是对象、在 `text` 里是
    字符串。两种都认，比在每个分支各写一遍 isinstance 短。
    """
    if isinstance(value, str):
        return value
    obj = _as_object(value)
    if obj is None:
        return ""
    for key in ("content", "text"):
        candidate = obj.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    return ""


def parse_content(raw: str) -> Mapping[str, JsonValue]:
    """飞书事件里的 `message.content` 是一段 **JSON 字符串**。解析失败返回空表。

    解析失败不抛：一条看不懂的消息体应当被降级成「没有正文」，而不是让整条 Channel 因为
    平台加了个新字段就报错（畸形消息丢弃并记录，不得终止 Channel）。
    """
    try:
        # `json.loads` 交回 `Any`。**在这里就收成 `object`**，否则它会顺着返回值一路漏
        # 进调用方（`plugins/…-cron/store.py` 采用的相同规则判定）。
        parsed: object = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, Mapping):
        return {}
    # boundary: 顶层已收窄到映射；值侧的形状由各取用点逐个判定。
    return cast("Mapping[str, JsonValue]", parsed)


# ------------------------------------------------------------------------------ 卡片元素


def _tag_markdown(obj: Mapping[str, JsonValue]) -> list[str]:
    content = obj.get("content")
    return [content] if isinstance(content, str) and content else []


def _tag_text(obj: Mapping[str, JsonValue]) -> list[str]:
    text = obj.get("text")
    return [text] if isinstance(text, str) and text.strip() else []


def _tag_div(obj: Mapping[str, JsonValue]) -> list[str]:
    parts: list[str] = []
    text = _text_of(obj.get("text"))
    if text:
        parts.append(text)
    for field in _as_list(obj.get("fields")) or ():
        field_obj = _as_object(field)
        if field_obj is None:
            continue
        value = _text_of(field_obj.get("text"))
        if value:
            parts.append(value)
    return parts


def _tag_anchor(obj: Mapping[str, JsonValue]) -> list[str]:
    parts: list[str] = []
    href = obj.get("href")
    if isinstance(href, str) and href:
        parts.append(f"link: {href}")
    text = obj.get("text")
    if isinstance(text, str) and text:
        parts.append(text)
    return parts


def _tag_button(obj: Mapping[str, JsonValue]) -> list[str]:
    parts: list[str] = []
    label = _text_of(obj.get("text"))
    if label:
        parts.append(label)
    url = obj.get("url")
    if not (isinstance(url, str) and url):
        multi = _as_object(obj.get("multi_url"))
        url = multi.get("url") if multi is not None else None
    if isinstance(url, str) and url:
        parts.append(f"link: {url}")
    return parts


def _tag_image(obj: Mapping[str, JsonValue]) -> list[str]:
    return [_text_of(obj.get("alt")) or "[image]"]


def _tag_nested(obj: Mapping[str, JsonValue]) -> list[str]:
    """把子元素摊平。`note` 与**未知 tag** 共用它。

    未知 tag 递归进 `elements` 是刻意的：飞书一直在加新的卡片元素，而它们几乎都把子元素
    放在那里。递归比列一张永远不全的白名单更耐用。
    """
    parts: list[str] = []
    for nested in _as_list(obj.get("elements")) or ():
        parts.extend(_extract_element(nested))
    return parts


def _tag_columns(obj: Mapping[str, JsonValue]) -> list[str]:
    parts: list[str] = []
    for column in _as_list(obj.get("columns")) or ():
        column_obj = _as_object(column)
        if column_obj is None:
            continue
        for nested in _as_list(column_obj.get("elements")) or ():
            parts.extend(_extract_element(nested))
    return parts


#: tag → 抽取器。**这张表就是「一张卡片在模型眼里长什么样」的全部定义**——改它就是改
#: 模型看到的东西。缺席的 tag 走 `_tag_nested`（见它的 docstring）。
_TAG_HANDLERS: Final[Mapping[str, Callable[[Mapping[str, JsonValue]], list[str]]]] = {
    "markdown": _tag_markdown,
    "lark_md": _tag_markdown,
    "plain_text": _tag_markdown,
    "text": _tag_text,
    "div": _tag_div,
    "a": _tag_anchor,
    "button": _tag_button,
    "img": _tag_image,
    "note": _tag_nested,
    "column_set": _tag_columns,
    "table": lambda obj: _extract_table(obj),
}


def _extract_element(element: JsonValue) -> list[str]:
    """一个卡片元素 → 若干行文本。派发到 `_TAG_HANDLERS`，未知 tag 递归子元素。"""
    obj = _as_object(element)
    if obj is None:
        return []
    tag = obj.get("tag")
    handler = _TAG_HANDLERS.get(tag) if isinstance(tag, str) else None
    return (handler or _tag_nested)(obj)


def _extract_table(element: Mapping[str, JsonValue]) -> list[str]:
    """表格元素 → 表头 + 每行一句。用 `|` 分隔，模型读得懂也复述得出来。"""
    columns: list[tuple[str, str]] = []
    for column in _as_list(element.get("columns")) or ():
        column_obj = _as_object(column)
        if column_obj is None:
            continue
        name = column_obj.get("name")
        if isinstance(name, str) and name:
            display = column_obj.get("display_name")
            columns.append((name, display if isinstance(display, str) and display else name))
    if not columns:
        return []
    parts = [" | ".join(header for _, header in columns)]
    for row in _as_list(element.get("rows")) or ():
        row_obj = _as_object(row)
        if row_obj is None:
            continue
        values: list[str] = []
        for name, _ in columns:
            value = row_obj.get(name)
            if isinstance(value, list):
                values.append(" ".join(str(item).strip() for item in value if item is not None))
            else:
                values.append("" if value is None else str(value).strip())
        line = " | ".join(values).strip()
        if line:
            parts.append(line)
    return parts


def _card_elements(obj: Mapping[str, JsonValue]) -> list[str]:
    """顶层 `elements`：**两种形状**——嵌套的 `[[el, …], …]` 与扁平的 `[el, …]`。"""
    elements = _as_list(obj.get("elements"))
    if elements is None:
        return []
    parts: list[str] = []
    if elements and isinstance(elements[0], list):
        for row in elements:
            for element in _as_list(row) or ():
                parts.extend(_extract_element(element))
        return parts
    for element in elements:
        parts.extend(_extract_element(element))
    return parts


def _top_title(obj: Mapping[str, JsonValue]) -> list[str]:
    """顶层 `title`。它排在全部元素**之前**（legacy 的顺序）。"""
    title = _text_of(obj.get("title"))
    return [f"title: {title}"] if title else []


def _header_title(obj: Mapping[str, JsonValue]) -> list[str]:
    """`header.title`。它排在全部元素**之后**（legacy 的顺序）。

    **两个标题拆成两个函数而不是一个再切片**：它们的位置不同，而按下标切一个合并列表在
    「只有 header 没有 top-level title」时会把 header 标题挪到最前面——那是一处静默的
    顺序错误，写这段时真的踩到过。
    """
    header = _as_object(obj.get("header"))
    if header is None:
        return []
    title = _text_of(header.get("title"))
    return [f"title: {title}"] if title else []


def extract_interactive(content: JsonValue) -> list[str]:
    """交互卡片 → 若干行文本。递归，认五种承载结构。

    **`user_dsl` 优先且取到就返回**：渲染过的卡片会把原始定义放在那里，它比渲染结果信息
    更全；两份合起来会让模型看到重复的正文。
    """
    if isinstance(content, str):
        parsed = parse_content(content)
        if not parsed:
            return [content] if content.strip() else []
        content = dict(parsed)
    obj = _as_object(content)
    if obj is None:
        return []

    dsl = obj.get("user_dsl")
    if isinstance(dsl, str) and dsl.strip():
        nested = parse_content(dsl)
        if nested:
            parts = extract_interactive(dict(nested))
            if parts:
                return parts

    parts = [*_top_title(obj), *_card_elements(obj)]
    body = _as_object(obj.get("body"))
    if body is not None:
        for element in _as_list(body.get("elements")) or ():
            parts.extend(_extract_element(element))
    card = _as_object(obj.get("card"))
    if card:
        parts.extend(extract_interactive(dict(card)))
    parts.extend(_header_title(obj))
    return parts


# ------------------------------------------------------------------------------ post


def _parse_post_block(block: Mapping[str, JsonValue]) -> tuple[str, list[str]]:
    """一个 post 块 → `(文本, image_key 列表)`。"""
    rows = _as_list(block.get("content"))
    if rows is None:
        return "", []
    texts: list[str] = []
    images: list[str] = []
    title = block.get("title")
    if isinstance(title, str) and title:
        texts.append(title)
    for row in rows:
        for item in _as_list(row) or ():
            element = _as_object(item)
            if element is None:
                continue
            tag = element.get("tag")
            if tag in ("text", "a"):
                text = element.get("text")
                if isinstance(text, str):
                    texts.append(text)
            elif tag == "at":
                user = element.get("user_name")
                texts.append(f"@{user if isinstance(user, str) and user else 'user'}")
            elif tag == "code_block":
                lang = element.get("language")
                code = element.get("text")
                texts.append(
                    f"\n```{lang if isinstance(lang, str) else ''}\n"
                    f"{code if isinstance(code, str) else ''}\n```\n"
                )
            elif tag == "img":
                key = element.get("image_key")
                if isinstance(key, str) and key:
                    images.append(key)
    return " ".join(texts).strip(), images


def extract_post(content: Mapping[str, JsonValue]) -> tuple[str, list[str]]:
    """富文本 post → `(文本, image_key 列表)`。**三种载荷形状都认。**

    直接（`{"title":…, "content": [[…]]}`）/ 本地化（`{"zh_cn": {…}}`）/
    包裹（`{"post": {"zh_cn": {…}}}`）。飞书在不同的 API 版本上发过不同的形状，
    只认一种意味着某些客户端发来的富文本会变成空正文。
    """
    root = content
    wrapped = _as_object(content.get("post"))
    if wrapped is not None:
        root = wrapped

    if "content" in root:
        text, images = _parse_post_block(root)
        if text or images:
            return text, images

    for locale in _POST_LOCALES:
        block = _as_object(root.get(locale))
        if block is None:
            continue
        text, images = _parse_post_block(block)
        if text or images:
            return text, images

    for value in root.values():
        block = _as_object(value)
        if block is None:
            continue
        text, images = _parse_post_block(block)
        if text or images:
            return text, images
    return "", []


def extract_share_card(content: Mapping[str, JsonValue], msg_type: str) -> str:
    """分享卡片 / 系统消息 / 合并转发 → 一行摘要。认不出的给 `[<msg_type>]`。"""
    if msg_type == "share_chat":
        return f"[shared chat: {content.get('chat_id', '')}]"
    if msg_type == "share_user":
        return f"[shared user: {content.get('user_id', '')}]"
    if msg_type == "share_calendar_event":
        return f"[shared calendar event: {content.get('event_key', '')}]"
    if msg_type == "system":
        return "[system message]"
    if msg_type == "merge_forward":
        return "[merged forward messages]"
    if msg_type == "interactive":
        parts = extract_interactive(dict(content))
        if parts:
            return "\n".join(parts)
    return f"[{msg_type}]"
