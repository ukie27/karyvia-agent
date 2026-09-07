"""官方 OpenAI API 插件：把实例暴露为 OpenAI 兼容的 HTTP Channel。

职责：提供 chat completions、models 与 health 端点，在 HTTP/SSE 与 Karyvia 消息契约之间
转换。不负责：执行 Turn、管理 Session 历史或终端渲染。

插件必须作为 Channel 工作，因为流式增量只通过出站投递路径返回。同一 conversation 串行，
不同 conversation 并发，保证响应关联稳定。会话历史由 SessionStore 管理，用量从事件总线
聚合。插件自行拥有监听端口；非回环地址必须配置 API key。鉴权只控制端点访问，不构成进程
或工具权限隔离。
"""

from __future__ import annotations

from typing import Final

from karyvia.contracts import (
    CapabilityKind,
    ErrorCode,
    KaryviaError,
)
from karyvia.sdk import (
    CapabilityDecl,
    KaryviaAPI,
    PluginContext,
    PluginManifest,
)

from .channel import ApiChannel
from .hub import SessionHub
from .settings import ApiSettings, resolve_settings
from .usage import UsageTracker

__all__ = [
    "CAPABILITY_NAME",
    "MANIFEST",
    "SECRET_NAME",
    "ApiChannel",
    "ApiSettings",
    "SessionHub",
    "UsageTracker",
    "resolve_settings",
    "setup",
]

#: 本插件提供的 Channel 能力名。
CAPABILITY_NAME: Final = "openai"

#: 可选的 Bearer 凭据在插件配置块里的键名（`plugins.openai-api.secrets.api_key`）。
#: 固定成常量，使配置路径与 `ctx.secret()` 的调用点保持同源。
SECRET_NAME: Final = "api_key"

MANIFEST: Final = PluginManifest(
    id="openai-api",
    version="0.1.0",
    sdk_range=">=5.0.0,<6.0.0",
    setup="karyvia_plugin_openai_api:setup",
    capabilities=(CapabilityDecl(kind=CapabilityKind.CHANNEL, name=CAPABILITY_NAME),),
    config_schema={
        "type": "object",
        "properties": {
            "host": {"type": "string"},
            # `0` 是合法的：内核分配一个空闲端口，真实端口由 `ApiChannel.bound_port`
            # 报出来。测试与「随便给我一个空闲端口」的部署都用它。
            "port": {"type": "integer", "minimum": 0, "maximum": 65535},
            "model": {"type": "string"},
            "conversation": {"type": "string"},
            "channel_id": {"type": "string"},
            "instance_id": {"type": "string"},
            "show_reasoning": {"type": "boolean"},
            "request_timeout_ms": {"type": "integer", "minimum": 1000},
        },
        "additionalProperties": False,
    },
)


def setup(api: KaryviaAPI) -> None:
    """注册 Channel。配置在这里校验一次，不拖到第一次请求。"""
    settings = resolve_settings(api.ctx)
    secret = _optional_secret(api.ctx)
    if settings.requires_auth and secret is None:
        raise KaryviaError(
            ErrorCode.CONFIG_INVALID,
            "绑定非回环地址的 OpenAI 兼容接口必须配置 api_key。",
            detail={
                "pointer": f"/plugins/{MANIFEST.id}/config/host",
                "host": settings.host,
                "fix": f"在 /plugins/{MANIFEST.id}/secrets/{SECRET_NAME} 配一个 ${{VAR}} 引用，"
                "或把 host 改回 127.0.0.1。",
            },
        )
    hub = SessionHub(settings, ctx=api.ctx)
    hub.usage.subscribe(api.ctx.events)
    api.register_channel(CAPABILITY_NAME, ApiChannel(hub, api_key=secret))


def _optional_secret(ctx: PluginContext) -> str | None:
    """取 Bearer 凭据。**没配不是错误**——回环上的本地实例不强制鉴权。

    `ctx.secret()` 在没配置引用时抛 `CONFIG_SECRET_MISSING`，那正是「用户没打算开鉴权」
    的形状，因此折成 `None`。其他错误原样抛出。
    """
    try:
        return ctx.secret(SECRET_NAME).reveal()
    except KaryviaError as exc:
        if exc.code is ErrorCode.CONFIG_SECRET_MISSING:
            return None
        raise
