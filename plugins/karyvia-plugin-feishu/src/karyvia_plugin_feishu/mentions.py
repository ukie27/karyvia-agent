"""@ 门控与 mention 占位符（开发方案 `D34`）。

职责：判定一条群聊消息是不是在跟 bot 说话；剥掉前导的 `@bot`；把飞书的 `@_user_N`
占位符换成可读的名字。**纯正则，零 IO，不认识配置也不认识 SDK。**
不负责：门控顺序（`normalize.py`）、正文抽取（`content.py`）。

**为什么独立成一个模块**：飞书的 @ 判定有四条命中路径、一条兜底启发式和一个负向断言，
是这个 channel 最容易被改错的一块。放进 `normalize.py` 之后它会变成那个文件里最不起眼的
三十行；独立之后它的 docstring 就是「@ 门控的规格」。

**身份未知时仍保留兜底启发式。** 飞书的 bot open_id 要调 `/open-apis/bot/v3/info` 才拿得到，
**权限没配好就可能永远拿不到**——没有兜底的话整个群聊
功能会静默失效，而用户看到的现象是「@ 它没反应」。因此这里反过来：宁可多答一次。
代价（同一个群里另一个 bot 被 @ 时会误命中）如实写在 `is_addressed_to_bot` 的 docstring 里。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, cast

from karyvia.contracts import JsonValue

__all__ = [
    "AT_ALL",
    "GROUP_POLICY_MENTION",
    "GROUP_POLICY_OPEN",
    "Mention",
    "is_addressed_to_bot",
    "resolve_mentions",
    "strip_leading_bot_mention",
]

#: 两种群聊门控。取值与配置字面量同名。
GROUP_POLICY_MENTION: Final = "mention"
GROUP_POLICY_OPEN: Final = "open"

#: 飞书的「@所有人」在正文里是这个字面量，它不出现在 `mentions` 列表里。
AT_ALL: Final = "@_all"

#: 机器人的 open_id 前缀。兜底启发式用它。
_BOT_OPEN_ID_PREFIX: Final = "ou_"


@dataclass(frozen=True, slots=True)
class Mention:
    """正文里的一处 @。`key` 是飞书塞进正文的占位符（形如 `@_user_1`）。"""

    key: str
    name: str = ""
    open_id: str = ""
    user_id: str = ""


def mentions_from(raw: Sequence[JsonValue]) -> tuple[Mention, ...]:
    """把事件里的 `message.mentions` 拍成 `Mention`。认不出的条目跳过而不是报错。"""
    parsed: list[Mention] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        key = item.get("key")
        if not isinstance(key, str) or not key:
            continue
        raw_ident = item.get("id")
        # boundary: 平台把 id 装在一个嵌套对象里；取不到就当空表，逐个键仍要判类型。
        ident: Mapping[str, object] = (
            cast("Mapping[str, object]", raw_ident) if isinstance(raw_ident, Mapping) else {}
        )
        name = item.get("name")
        open_id = ident.get("open_id")
        user_id = ident.get("user_id")
        parsed.append(
            Mention(
                key=key,
                name=name if isinstance(name, str) else "",
                open_id=open_id if isinstance(open_id, str) else "",
                user_id=user_id if isinstance(user_id, str) else "",
            )
        )
    return tuple(parsed)


def is_addressed_to_bot(
    *,
    content: str,
    mentions: Sequence[Mention],
    bot_open_id: str,
    group_policy: str,
) -> bool:
    """这条群聊消息是不是在跟 bot 说话。**四条命中路径，顺序有讲究。**

    1. `group_policy == "open"` —— 群里所有消息都答。
    2. 正文含字面量 `@_all` —— **必须在 open_id 匹配之前**：@所有人不出现在 `mentions`
       列表里，先比 open_id 会让它永远漏掉。
    3. 任一 mention 的 `open_id` 等于 bot 自己的。
    4. **bot 身份未知时的兜底**：一条没有 `user_id` 且 `open_id` 以 `ou_` 开头的 mention
       大概率就是机器人（真人 mention 通常带 `user_id`）。

    **兜底的代价，如实写在这里**：同一个群里另一个 bot 被 @ 时会误命中，我们会多答一次。
    取舍是「多答一次」而不是「群聊彻底不工作」——见模块 docstring。
    """
    if group_policy == GROUP_POLICY_OPEN:
        return True
    if AT_ALL in content:
        return True
    if bot_open_id:
        return any(mention.open_id == bot_open_id for mention in mentions)
    return any(
        not mention.user_id and mention.open_id.startswith(_BOT_OPEN_ID_PREFIX)
        for mention in mentions
    )


def strip_leading_bot_mention(
    content: str, mentions: Sequence[Mention], *, bot_open_id: str
) -> str:
    """剥掉正文开头的 `@bot` 占位符。

    **必须在命令路由之前做**：`@bot /help` 不剥的话，`kernel/routing/dispatcher.py` 看到的
    是 `@_user_1 /help`，那不是一条命令，于是整条消息被当成普通文本喂给模型。

    正则的负向断言 `(?![A-Za-z0-9_])` 是必需的：占位符是 `@_user_1` 而正文里可能有
    `@_user_10`，没有断言就会把后者的前缀吃掉、留下一个孤零零的 `0`。
    """
    stripped = content.lstrip()
    for mention in mentions:
        if bot_open_id and mention.open_id != bot_open_id:
            continue
        pattern = rf"^{re.escape(mention.key)}(?![A-Za-z0-9_])"
        replaced = re.sub(pattern, "", stripped, count=1)
        if replaced != stripped:
            return replaced.lstrip()
    return content


def resolve_mentions(content: str, mentions: Sequence[Mention]) -> str:
    """把剩余的 `@_user_N` 占位符换成可读的名字。

    模型看到的是正文；留着 `@_user_2` 这种东西它只能猜。同样用负向断言，理由同上。
    """
    resolved = content
    for mention in mentions:
        label = f"@{mention.name}" if mention.name else "@某人"
        pattern = rf"{re.escape(mention.key)}(?![A-Za-z0-9_])"
        resolved = re.sub(pattern, label, resolved)
    return resolved
