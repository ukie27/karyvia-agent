"""`feishu` 插件的配置读取与校验（插件只看得见自己那一块）。

职责：把 `ctx.config` 校验成不可变的 `FeishuSettings`，并派生 `normalize.InboundGate`。
全部校验在 `setup()` 时发生一次。
不负责：读凭据（那是 `ctx.secret()`，在 `__init__.py`）、接触平台、任何 IO。

三条：

- **默认值一字不改地沿用 legacy**，只换单位与命名（`_STREAM_EDIT_INTERVAL = 0.5` →
  `stream_edit_interval_ms = 500`：新层配置里所有时长都是毫秒）。`allow_from` 为空 =
  **允许所有**，与 legacy 一致——改它等于静默改变谁能用这个 bot。
- **`operators` 与 `allow_chats` 是新增的**：前者是因为契约要求 `Sender.is_operator` 由
  Channel 在边界决定（legacy 没有这个概念），后者限制允许驱动 Agent 的会话。
  两者默认空，`operators` 空 = 无人是 operator（安全的一侧）。
- **`encrypt_key` / `verification_token` 不在这里**：WS 长连接下 SDK 走
  `do_without_validation`，**没有 AES 解密也没有签名校验**（那是 webhook 模式才有的），
  legacy 传的恒是可空值、从来没被用过。保留一个永远不生效的安全配置项比没有它更糟——
  它会让运维以为自己配了一层校验。README 里有一行差异说明。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from karyvia.contracts import ErrorCode, InstanceId, JsonValue, KaryviaError
from karyvia.sdk import PluginContext

from .mentions import GROUP_POLICY_MENTION, GROUP_POLICY_OPEN
from .normalize import InboundGate
from .tool_hints import DEFAULT_PREFIX

__all__ = [
    "CAPABILITY_NAME",
    "CONFIG_KEYS",
    "DOMAINS",
    "GROUP_POLICIES",
    "SECRET_APP_ID",
    "SECRET_APP_SECRET",
    "FeishuSettings",
    "resolve_settings",
]

#: 本插件的能力名，同时是 `channel_id` 的默认值。
CAPABILITY_NAME: Final = "feishu"

#: 凭据名固定不可配置，使配置路径与 `ctx.secret()` 调用保持同源。
#: **两条都走 secrets**（包括看起来不敏感的 `app_id`）：`ctx.config` 不解析 `${VAR}`，
#: 把 `app_id` 放 config 会让写 `${FEISHU_APP_ID}` 的人拿到字面串并在连接时得到一个
#: 无法诊断的 401。凭据是一对，就一起走凭据通道。
SECRET_APP_ID: Final = "app_id"
SECRET_APP_SECRET: Final = "app_secret"

#: 两个品牌域。它决定 SDK 的 domain 常量——填错会连到另一个租户体系上去。
DOMAINS: Final[frozenset[str]] = frozenset({"feishu", "lark"})

GROUP_POLICIES: Final[frozenset[str]] = frozenset({GROUP_POLICY_MENTION, GROUP_POLICY_OPEN})

_DEFAULT_STREAM_EDIT_INTERVAL_MS: Final = 500
_DEFAULT_REACT_EMOJI: Final = "THUMBSUP"

#: 全部配置键。与 manifest 的 `CONFIG_SCHEMA` 由一条对照用例钉住——两处都「自洽」而对不上
#: 时，一个写对了的配置会在加载前校验 被 schema 拒掉，而错误指向的是 schema 不是这里。
CONFIG_KEYS: Final[frozenset[str]] = frozenset(
    {
        "channel_id",
        "instance_id",
        "domain",
        "allow_from",
        "allow_chats",
        "operators",
        "group_policy",
        "topic_isolation",
        "reply_to_message",
        "streaming",
        "stream_edit_interval_ms",
        "react_emoji",
        "done_emoji",
        "tool_hint_prefix",
    }
)

_NOT_A_STRING: Final = "该配置项必须是字符串。"
_NOT_A_BOOL: Final = "该配置项必须是布尔值。"
_NOT_AN_INT: Final = "该配置项必须是整数且不小于下限。"
_NOT_A_LIST: Final = "该配置项必须是数组。"
_NOT_AN_ID: Final = "名单里的每一项都必须是非空 id 字符串。"
_BAD_CHOICE: Final = "该配置项的取值受限。"


def _invalid(message: str, **detail: object) -> KaryviaError:
    return KaryviaError(ErrorCode.CONFIG_INVALID, message, detail=detail)


def _read_str(config: Mapping[str, JsonValue], key: str, *, default: str) -> str:
    value = config.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise _invalid(_NOT_A_STRING, key=key, actual_type=type(value).__name__)
    return value


def _read_choice(
    config: Mapping[str, JsonValue], key: str, *, default: str, allowed: frozenset[str]
) -> str:
    value = _read_str(config, key, default=default)
    if value not in allowed:
        raise _invalid(_BAD_CHOICE, key=key, allowed=sorted(allowed), actual=value)
    return value


def _read_bool(config: Mapping[str, JsonValue], key: str, *, default: bool) -> bool:
    value = config.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise _invalid(_NOT_A_BOOL, key=key, actual_type=type(value).__name__)
    return value


def _read_int(config: Mapping[str, JsonValue], key: str, *, default: int, minimum: int) -> int:
    value = config.get(key)
    if value is None:
        return default
    # `bool` 是 `int` 的子类，但 `"stream_edit_interval_ms": true` 是配置写错了。
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise _invalid(_NOT_AN_INT, key=key, minimum=minimum, actual_type=type(value).__name__)
    return value


def _read_ids(config: Mapping[str, JsonValue], key: str) -> frozenset[str]:
    """读一份 id 名单。飞书的 open_id / chat_id 都是字符串，这里不接受数字。"""
    value = config.get(key)
    if value is None:
        return frozenset()
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise _invalid(_NOT_A_LIST, key=key, actual_type=type(value).__name__)
    items: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise _invalid(_NOT_AN_ID, key=key)
        items.add(item.strip())
    return frozenset(items)


@dataclass(frozen=True, slots=True)
class FeishuSettings:
    """一份已校验的配置。构造后不可变，每条消息直接读它。"""

    instance_id: InstanceId
    channel_id: str
    domain: str
    allow_from: frozenset[str]
    allow_chats: frozenset[str]
    operators: frozenset[str]
    group_policy: str
    topic_isolation: bool
    reply_to_message: bool
    streaming: bool
    stream_edit_interval_ms: int
    react_emoji: str
    done_emoji: str
    #: 工具提示行的前缀。**空串 = 关闭**（`tool_hints.render` 恒返回空串，`channel.py`
    #: 连那条泵都不派生）。默认值沿用 legacy 的 `tool_hint_prefix`。
    tool_hint_prefix: str

    def gate(self, *, bot_open_id: str = "") -> InboundGate:
        """派生归一化门控。**每条 Channel 只建一次并复用**——它持有去重表。

        `bot_open_id` 要等连上之后调 `/open-apis/bot/v3/info` 才知道，因此它是
        `InboundGate` 上的可变字段而不是构造参数的一部分（由 `channel.py` 在拿到之后回填）。
        """
        return InboundGate(
            instance_id=self.instance_id,
            channel_id=self.channel_id,
            bot_open_id=bot_open_id,
            allow_from=self.allow_from,
            allow_chats=self.allow_chats,
            operators=self.operators,
            group_policy=self.group_policy,
            topic_isolation=self.topic_isolation,
        )


def resolve_settings(ctx: PluginContext) -> FeishuSettings:
    """把 `ctx.config` 校验成一份设置。

    **异常约定**：类型或取值不对抛 `CONFIG_INVALID`。校验在 `setup()` 时发生一次，
    不拖到第一条消息。
    """
    config = ctx.config
    return FeishuSettings(
        instance_id=InstanceId(_read_str(config, "instance_id", default="default")),
        channel_id=_read_str(config, "channel_id", default=CAPABILITY_NAME),
        domain=_read_choice(config, "domain", default="feishu", allowed=DOMAINS),
        allow_from=_read_ids(config, "allow_from"),
        allow_chats=_read_ids(config, "allow_chats"),
        operators=_read_ids(config, "operators"),
        group_policy=_read_choice(
            config, "group_policy", default=GROUP_POLICY_MENTION, allowed=GROUP_POLICIES
        ),
        topic_isolation=_read_bool(config, "topic_isolation", default=True),
        reply_to_message=_read_bool(config, "reply_to_message", default=False),
        streaming=_read_bool(config, "streaming", default=True),
        stream_edit_interval_ms=_read_int(
            config,
            "stream_edit_interval_ms",
            default=_DEFAULT_STREAM_EDIT_INTERVAL_MS,
            minimum=100,
        ),
        react_emoji=_read_str(config, "react_emoji", default=_DEFAULT_REACT_EMOJI),
        done_emoji=_read_str(config, "done_emoji", default=""),
        tool_hint_prefix=_read_str(config, "tool_hint_prefix", default=DEFAULT_PREFIX),
    )
