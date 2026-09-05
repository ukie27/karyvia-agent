"""官方插件 `openai-api`：把实例接到一个 OpenAI 兼容的 HTTP 接口上（开发方案 `D31`）。

职责：声明一条 `CHANNEL` 能力，起一个只有三个端点的 HTTP 服务
（`POST /v1/chat/completions`、`GET /v1/models`、`GET /health`），并把请求翻译成
`InboundMessage`、把 `OutboundMessage` 翻译成 OpenAI 的响应体或 SSE 分片。
不负责：执行 turn、管理会话历史、渲染终端（那三件分别是 `kernel/turn/`、
会话存储能力与 `cli-entry` 的事）。

**它是一个 Channel 而不是一条捷径**（`MSG-007`）。这条设计不是形式主义：出站增量只经
`OrchestratorDeps.deliver` 按 `channel_id` 路由回注册过的 Channel，因此**流式响应只有
Channel 拿得到**——`AgentInstance.submit()` 要等整条 turn 跑完才返回 `TurnReceipt`，
用它做不出 SSE。`cli-entry` 在 `D23` 已经走过同一条路。

**它取代的是被 `D31` 删掉的 `legacy/api/server.py`**，但不是移植：旧实现读的是
`AgentLoop._last_usage` 这个私有属性、并且把整段请求历史当输入。这里两条都不做——
历史归会话存储，用量走事件总线。

**已知的诚实边界**，都在 docstring 与 README 里写着而不是留给用户发现：

- **同一 conversation 的 turn 是串行的，不同 conversation 并发**（`D33` 之后）。装配根的
  Channel 泵按 `conversation_id` 扇出：每个 conversation 一条 lane，lane 内严格按到达顺序
  串行（`EDG-202`），lane 之间互不阻塞。因此**并发客户端只在打同一个 `conversation` 时才
  排队**。`D31`–`D32` 期间它是完全串行的，那条能力回退已经消除。
  同 conversation 内的串行正是下面 `SessionHub` 那套「最老等待者」关联仍然成立的前提。
- **插件自己拥有监听端口**。宿主的 `ctx.net` 是出站 HTTP 服务，不负责监听；启用插件
  就是信任它建立本地服务。
- **端点鉴权不是进程隔离**。能连上这个端点的调用方就能驱动
  实例上的全部工具，包括 `shell.exec`。默认只绑回环，且绑非回环地址时**必须**配
  `api_key`，否则 `setup()` 直接以 `CONFIG_INVALID` 拒绝。
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
    """注册 Channel。配置在这里校验一次，不拖到第一次请求（`D18` 的先例）。"""
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
