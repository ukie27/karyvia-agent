"""官方 Feishu 插件：通过飞书或 Lark 长连接提供 Channel 能力。

职责：归一化入站消息与附件、执行群聊和发送者准入、用文本/富文本/CardKit 投递普通与流式
回复，并提供工具进度提示。不负责：Turn 执行、Session 存储、命令路由或创建飞书应用。

一个 Karyvia 实例连接一个飞书应用；多个应用应使用独立实例。插件只支持长连接，不支持
webhook、语音转写或扫码建应用。群聊默认要求提及机器人，话题隔离时每个话题使用独立会话。
平台附件在入站侧保留为 `OPAQUE` 引用。工具事件不包含参数，所以进度提示只显示工具名，
避免把文件内容、路径或命令传播到事件日志和聊天平台。
"""

from __future__ import annotations

from typing import Final

from karyvia.contracts import (
    CapabilityKind,
    ErrorCode,
    EventName,
    KaryviaError,
    SecretStr,
)
from karyvia.sdk import (
    CapabilityDecl,
    KaryviaAPI,
    ManifestJsonSchema,
    PluginContext,
    PluginManifest,
)

from .cards import build_elements, split_by_table_limit
from .channel import FeishuChannel
from .client import FeishuClient
from .content import extract_interactive, extract_post, extract_share_card
from .gateway import MISSING_SDK_FIX, FeishuGateway, event_to_raw
from .indicators import Indicators
from .mentions import Mention, is_addressed_to_bot, resolve_mentions, strip_leading_bot_mention
from .normalize import (
    InboundGate,
    RawInbound,
    decode_conversation,
    encode_conversation,
    normalize,
)
from .outbound import (
    POST_MAX_LEN,
    TERMINAL_MARKERS,
    TEXT_MAX_LEN,
    detect_format,
    markdown_to_post,
)
from .settings import (
    CAPABILITY_NAME,
    CONFIG_KEYS,
    SECRET_APP_ID,
    SECRET_APP_SECRET,
    FeishuSettings,
    resolve_settings,
)
from .stream import StreamRelay

__all__ = [
    "CAPABILITY_NAME",
    "CONFIG_KEYS",
    "CONFIG_SCHEMA",
    "MANIFEST",
    "MISSING_SDK_FIX",
    "POST_MAX_LEN",
    "SECRET_APP_ID",
    "SECRET_APP_SECRET",
    "TERMINAL_MARKERS",
    "TEXT_MAX_LEN",
    "FeishuChannel",
    "FeishuClient",
    "FeishuGateway",
    "FeishuSettings",
    "InboundGate",
    "Indicators",
    "Mention",
    "RawInbound",
    "StreamRelay",
    "build_elements",
    "decode_conversation",
    "detect_format",
    "encode_conversation",
    "event_to_raw",
    "extract_interactive",
    "extract_post",
    "extract_share_card",
    "is_addressed_to_bot",
    "markdown_to_post",
    "normalize",
    "resolve_mentions",
    "resolve_settings",
    "setup",
    "split_by_table_limit",
    "strip_leading_bot_mention",
]

#: 插件配置块的 JSON Schema。加载前校验 在**加载之前**按它校验形状，`settings.py` 在 `setup()`
#: 里再校验语义（枚举、区间）。两处由一条 `set(properties) == CONFIG_KEYS` 用例钉住。
#:
#: 标注成 `ManifestJsonSchema` 而不是 `contracts.JsonSchema`：契约那个类型进不了
#: pydantic 模型（会 `RecursionError`），细节与另外两个被否掉的候选见
#: `sdk/manifest.py::ManifestJsonValue`。这里必须显式标注，因为字段
#: 类型是 pydantic 的 `JsonValue`，`dict` 值不变导致嵌套字面量怎么标都不成子类型。
CONFIG_SCHEMA: Final[ManifestJsonSchema] = {
    "type": "object",
    "properties": {
        "channel_id": {"type": "string"},
        "instance_id": {"type": "string"},
        "domain": {"type": "string", "enum": ["feishu", "lark"]},
        "allow_from": {"type": "array", "items": {"type": "string"}},
        "allow_chats": {"type": "array", "items": {"type": "string"}},
        "operators": {"type": "array", "items": {"type": "string"}},
        "group_policy": {"type": "string", "enum": ["mention", "open"]},
        "topic_isolation": {"type": "boolean"},
        "reply_to_message": {"type": "boolean"},
        "streaming": {"type": "boolean"},
        "stream_edit_interval_ms": {"type": "integer", "minimum": 100},
        "react_emoji": {"type": "string"},
        "done_emoji": {"type": "string"},
        "tool_hint_prefix": {"type": "string"},
    },
    "additionalProperties": False,
}

MANIFEST: Final = PluginManifest(
    id="feishu",
    version="0.1.0",
    sdk_range=">=5.0.0,<6.0.0",
    setup="karyvia_plugin_feishu:setup",
    # **不写 `overrides`**（它不取代任何内建）、**不写 `priority`**（默认值 100 会被原样
    # 采纳，而内建基准是 0—— 记的坑）。
    capabilities=(CapabilityDecl(kind=CapabilityKind.CHANNEL, name=CAPABILITY_NAME),),
    # `app_id` 也走 secrets：`ctx.config` 不解析 `${VAR}`，放 config 会让写
    # `${FEISHU_APP_ID}` 的人拿到字面串并在连接时得到一个无法诊断的 401。凭据是一对。
    config_schema=CONFIG_SCHEMA,
)

_MISSING_CREDENTIALS: Final = "飞书 Channel 必须同时配置 app_id 与 app_secret。"


def setup(api: KaryviaAPI) -> None:
    """注册 Channel。配置与凭据在这里各解析一次，不拖到第一条消息。

    **顺带订阅 `tool.call_started`**：工具提示的唯一数据源（`channel.py` 的 docstring 说明
    了为什么不能从出站流里拿）。事件订阅的生命周期就是插件的生命周期，由 Kernel 在
    禁用时统一取消。
    """
    settings = resolve_settings(api.ctx)
    channel = FeishuChannel(
        settings,
        app_id=_required_secret(api.ctx, SECRET_APP_ID),
        app_secret=_required_secret(api.ctx, SECRET_APP_SECRET),
    )
    if settings.tool_hint_prefix:
        api.ctx.events.subscribe(EventName.TOOL_CALL_STARTED, channel.on_tool_call)
    api.register_channel(CAPABILITY_NAME, channel)


def _required_secret(ctx: PluginContext, name: str) -> SecretStr:
    """取一条必填凭据。**没配就是配置错误**——一个没有凭据的飞书 Channel 连不上任何东西，
    让它「起来了但什么都不做」比直接说清楚更糟。

    `ctx.secret()` 的其他错误原样抛出；这里只把缺失凭据改写成带配置路径的错误。
    """
    try:
        return ctx.secret(name)
    except KaryviaError as exc:
        if exc.code is ErrorCode.CONFIG_SECRET_MISSING:
            raise KaryviaError(
                ErrorCode.CONFIG_INVALID,
                _MISSING_CREDENTIALS,
                detail={
                    "pointer": f"/plugins/{MANIFEST.id}/secrets/{name}",
                    "fix": "配一个 ${VAR} 引用，例如 ${FEISHU_APP_ID} / ${FEISHU_APP_SECRET}。",
                },
            ) from exc
        raise
