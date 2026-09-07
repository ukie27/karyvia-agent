"""插件配置块的解析与校验（插件只看得见自己那一块）。

职责：把 `plugins.openai-api.config` 收窄成一个 `ApiSettings`，非法值在 `setup()` 时就
以 `CONFIG_INVALID` + JSON Pointer 报出来。
不负责：读凭据（`__init__.py`）、起服务（`channel.py`）、决定端点行为（`http.py`）。

**在 `setup()` 校验一次，不拖到第一次请求**：一份写错的配置应当让 `karyvia serve` 当场失败，
而不是在某个客户端连上来时才变成一个 500。
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from typing import Final

from karyvia.contracts import ErrorCode, InstanceId, JsonValue, KaryviaError
from karyvia.sdk import PluginContext

__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "ApiSettings", "resolve_settings"]

#: 默认只绑回环。这个默认值是安全边界的一部分而不是习惯：能连上这个端点的调用方
#: 可以驱动实例上的全部工具（含 `shell.exec`）。
DEFAULT_HOST: Final = "127.0.0.1"
DEFAULT_PORT: Final = 8760
DEFAULT_CONVERSATION: Final = "default"
DEFAULT_CHANNEL_ID: Final = "api"
#: 一个请求最多等多久拿到终态。它比任何 turn 预算都长——真正的上限是
#: `turn.turn_timeout_ms`，这里只是防「Channel 层面出岔子导致请求永远挂着」。
DEFAULT_REQUEST_TIMEOUT_MS: Final = 600_000


class ApiSettings:
    """一次运行的全部配置。不可变，`setup()` 之后不再变化。"""

    __slots__ = (
        "channel_id",
        "default_conversation",
        "host",
        "instance_id",
        "model_id",
        "port",
        "request_timeout_ms",
        "show_reasoning",
    )

    def __init__(
        self,
        *,
        host: str,
        port: int,
        model_id: str,
        instance_id: InstanceId,
        channel_id: str = DEFAULT_CHANNEL_ID,
        default_conversation: str = DEFAULT_CONVERSATION,
        show_reasoning: bool = False,
        request_timeout_ms: int = DEFAULT_REQUEST_TIMEOUT_MS,
    ) -> None:
        self.host = host
        self.port = port
        self.model_id = model_id
        self.instance_id = instance_id
        self.channel_id = channel_id
        self.default_conversation = default_conversation
        self.show_reasoning = show_reasoning
        self.request_timeout_ms = request_timeout_ms

    @property
    def requires_auth(self) -> bool:
        """绑定的地址是否要求必须配 `api_key`。

        判据是「**不是**回环」而不是「等于 0.0.0.0」：`192.168.x.x` 同样把一个能执行
        shell 的端点暴露给整个局域网。主机名（非 IP 字面量）一律按需要鉴权处理——
        解析结果取决于 DNS，猜它等于替用户赌一把。
        """
        try:
            return not ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return self.host.lower() not in {"localhost"}


def resolve_settings(ctx: PluginContext) -> ApiSettings:
    """从 `ctx.config` 解析。缺项取默认值，类型不对即 `CONFIG_INVALID`。"""
    config = ctx.config
    return ApiSettings(
        host=_str(config, "host", DEFAULT_HOST),
        port=_port(config),
        model_id=_str(config, "model", ""),
        instance_id=InstanceId(_str(config, "instance_id", "default")),
        channel_id=_str(config, "channel_id", DEFAULT_CHANNEL_ID),
        default_conversation=_str(config, "conversation", DEFAULT_CONVERSATION),
        show_reasoning=_bool(config, "show_reasoning", False),
        request_timeout_ms=_positive_int(
            config, "request_timeout_ms", DEFAULT_REQUEST_TIMEOUT_MS
        ),
    )


def _str(config: Mapping[str, JsonValue], key: str, default: str) -> str:
    value = config.get(key, default)
    if not isinstance(value, str):
        raise _invalid(key, "应当是字符串")
    return value


def _bool(config: Mapping[str, JsonValue], key: str, default: bool) -> bool:
    value = config.get(key, default)
    # `1` 不是 `True`：布尔项收到非布尔一律拒绝。
    if not isinstance(value, bool):
        raise _invalid(key, "应当是布尔值")
    return value


def _positive_int(config: Mapping[str, JsonValue], key: str, default: int) -> int:
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _invalid(key, "应当是正整数")
    return value


def _port(config: Mapping[str, JsonValue]) -> int:
    """端口。**`0` 是合法的**——测试与「随便给我一个空闲端口」都用它。"""
    value = config.get("port", DEFAULT_PORT)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 65535:
        raise _invalid("port", "应当是 0–65535 的整数")
    return value


def _invalid(key: str, expectation: str) -> KaryviaError:
    """错误里给指针而不是值：配置值可能是凭据的一部分。"""
    return KaryviaError(
        ErrorCode.CONFIG_INVALID,
        f"OpenAI 兼容接口的配置项 `{key}` {expectation}。",
        detail={"pointer": f"/plugins/openai-api/config/{key}"},
    )
