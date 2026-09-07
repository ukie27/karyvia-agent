"""配置解析与一次性校验（插件只看得见自己那一块）。

职责：把 `plugins.mcp.config` 变成一组不可变的 server 设置。
不负责：连接（`client.py`）、命名（`naming.py`）。

**三种传输各有各的必填项**，因此校验是按 `type` 分支的：`stdio` 要 `command`，
两种 HTTP 传输要 `url`。少一项即 `CONFIG_INVALID` 并指向那个键——一份连不上的配置应当
在 `karyvia plugins list` 里看得见，而不是变成启动时一条「连接超时」。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from karyvia.contracts import ErrorCode, JsonValue, KaryviaError

from .naming import DEFAULT_PREFIX, normalise_segment

__all__ = [
    "CREDENTIAL_PLACEHOLDER",
    "DEFAULT_CALL_TIMEOUT_MS",
    "DEFAULT_CONNECT_TIMEOUT_MS",
    "HTTP_TRANSPORTS",
    "SECRET_NAME",
    "TRANSPORTS",
    "McpSettings",
    "ServerSettings",
    "needs_credential",
    "resolve_settings",
    "with_credential",
]

#: 唯一的凭据名。远端 server 的鉴权 header 里写 `{api_key}` 即被替换（`web` 插件
#: `custom` 后端采用的相同规则约定）。做成固定常量是因为 manifest 的
#: 固定名字使配置路径与 `ctx.secret()` 调用保持同源。
SECRET_NAME: Final = "api_key"

#: header 值里被替换的占位符。让用户既能写 `Bearer {api_key}` 也能写裸 `{api_key}`，
#: 而不必为每种鉴权风格再加一个配置项。
CREDENTIAL_PLACEHOLDER: Final = "{api_key}"

TRANSPORTS: Final[tuple[str, ...]] = ("stdio", "sse", "streamable_http")

#: 走 URL 的两种。它们的必填项相同，因此在校验里合并成一支。
HTTP_TRANSPORTS: Final[frozenset[str]] = frozenset({"sse", "streamable_http"})

DEFAULT_CONNECT_TIMEOUT_MS: Final = 15_000
DEFAULT_CALL_TIMEOUT_MS: Final = 60_000
_DEFAULT_MAX_RESULT_CHARS: Final = 30_000

_NOT_AN_OBJECT: Final = "这个配置项必须是对象。"
_NOT_A_STRING: Final = "这个配置项必须是字符串。"
_NOT_A_STRING_LIST: Final = "这个配置项必须是字符串数组。"
_NOT_A_POSITIVE_INT: Final = "这个配置项必须是正整数。"
_NOT_A_BOOL: Final = "这个配置项必须是布尔值。"
_UNKNOWN_TRANSPORT: Final = "未知的 MCP 传输类型。"
_STDIO_NEEDS_COMMAND: Final = "stdio 传输必须配置 command。"
_HTTP_NEEDS_URL: Final = "这种传输必须配置 url。"
_BAD_SERVER_NAME: Final = "server 名字必须能归一成合法的能力名段（小写字母、数字、下划线）。"
_BAD_PREFIX: Final = "prefix 必须能归一成合法的能力名段。"


@dataclass(frozen=True, slots=True)
class ServerSettings:
    """一个 MCP server 的连接参数。"""

    name: str
    transport: str = "stdio"
    enabled: bool = True
    command: str = ""
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict[str, str])
    cwd: str = ""
    url: str = ""
    headers: Mapping[str, str] = field(default_factory=dict[str, str])


@dataclass(frozen=True, slots=True)
class McpSettings:
    """本插件的全部设置。"""

    prefix: str = DEFAULT_PREFIX
    connect_timeout_ms: int = DEFAULT_CONNECT_TIMEOUT_MS
    call_timeout_ms: int = DEFAULT_CALL_TIMEOUT_MS
    max_result_chars: int = _DEFAULT_MAX_RESULT_CHARS
    servers: tuple[ServerSettings, ...] = ()

    @property
    def enabled_servers(self) -> tuple[ServerSettings, ...]:
        return tuple(server for server in self.servers if server.enabled)


def resolve_settings(config: Mapping[str, JsonValue]) -> McpSettings:
    """解析 `plugins.mcp.config`。**异常约定**：任何问题一律 `CONFIG_INVALID` 并带键路径。"""
    prefix = _string(config, "prefix", DEFAULT_PREFIX).strip() or DEFAULT_PREFIX
    if normalise_segment(prefix) != prefix:
        raise _invalid(_BAD_PREFIX, "prefix", prefix=prefix)
    return McpSettings(
        prefix=prefix,
        connect_timeout_ms=_positive_int(
            config, "connect_timeout_ms", DEFAULT_CONNECT_TIMEOUT_MS
        ),
        call_timeout_ms=_positive_int(config, "call_timeout_ms", DEFAULT_CALL_TIMEOUT_MS),
        max_result_chars=_positive_int(
            config, "max_result_chars", _DEFAULT_MAX_RESULT_CHARS
        ),
        servers=_servers(config),
    )


def needs_credential(settings: McpSettings) -> bool:
    """有没有哪台启用的 server 的 header 里写了占位符。

    **只在真的用得到时才去取凭据**：一台都不需要鉴权的配置不该因为没导出
    `MCP_TOKEN` 而连不上（`web` 插件的 `needs_credential` 是同一条判据）。
    """
    return any(
        CREDENTIAL_PLACEHOLDER in value
        for server in settings.enabled_servers
        for value in server.headers.values()
    )


def with_credential(settings: McpSettings, api_key: str) -> McpSettings:
    """把 header 里的 `{api_key}` 换成真正的凭据。

    **只替换 header**：URL 与命令行参数刻意不做替换——凭据出现在进程命令行上会被
    `ps` 看到，出现在 URL 里会进代理日志。要那么用的人可以自己写进 `env`。
    """
    if not api_key:
        return settings
    replaced = tuple(
        ServerSettings(
            name=server.name,
            transport=server.transport,
            enabled=server.enabled,
            command=server.command,
            args=server.args,
            env=server.env,
            cwd=server.cwd,
            url=server.url,
            headers={
                key: value.replace(CREDENTIAL_PLACEHOLDER, api_key)
                for key, value in server.headers.items()
            },
        )
        for server in settings.servers
    )
    return McpSettings(
        prefix=settings.prefix,
        connect_timeout_ms=settings.connect_timeout_ms,
        call_timeout_ms=settings.call_timeout_ms,
        max_result_chars=settings.max_result_chars,
        servers=replaced,
    )


def _servers(config: Mapping[str, JsonValue]) -> tuple[ServerSettings, ...]:
    block = config.get("servers")
    if block is None:
        return ()
    if not isinstance(block, Mapping):
        raise _invalid(_NOT_AN_OBJECT, "servers")
    # 按名字排序：加载顺序因此与配置文件里的书写顺序无关，两次启动的注册顺序恒相同。
    return tuple(_server(name, block[name]) for name in sorted(block))


def _server(name: str, raw: JsonValue) -> ServerSettings:
    where = f"servers.{name}"
    if normalise_segment(name) != name:
        raise _invalid(_BAD_SERVER_NAME, where, server=name)
    if not isinstance(raw, Mapping):
        raise _invalid(_NOT_AN_OBJECT, where)
    transport = _string(raw, "type", "stdio", prefix=where).strip().lower()
    if transport not in TRANSPORTS:
        raise _invalid(
            _UNKNOWN_TRANSPORT,
            f"{where}.type",
            transport=transport,
            choices=list(TRANSPORTS),
        )
    command = _string(raw, "command", "", prefix=where).strip()
    url = _string(raw, "url", "", prefix=where).strip()
    if transport == "stdio" and not command:
        raise _invalid(_STDIO_NEEDS_COMMAND, f"{where}.command")
    if transport in HTTP_TRANSPORTS and not url:
        raise _invalid(_HTTP_NEEDS_URL, f"{where}.url", transport=transport)
    return ServerSettings(
        name=name,
        transport=transport,
        enabled=_bool(raw, "enabled", True, prefix=where),
        command=command,
        args=_string_list(raw, "args", prefix=where),
        env=_string_map(raw, "env", prefix=where),
        cwd=_string(raw, "cwd", "", prefix=where).strip(),
        url=url,
        headers=_string_map(raw, "headers", prefix=where),
    )


def _string(
    block: Mapping[str, JsonValue], key: str, default: str, *, prefix: str = ""
) -> str:
    value = block.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise _invalid(_NOT_A_STRING, _path(prefix, key))
    return value


def _bool(
    block: Mapping[str, JsonValue], key: str, default: bool, *, prefix: str = ""
) -> bool:
    value = block.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise _invalid(_NOT_A_BOOL, _path(prefix, key))
    return value


def _positive_int(block: Mapping[str, JsonValue], key: str, default: int) -> int:
    value = block.get(key)
    if value is None:
        return default
    # `True` 是 `int` 的实例，放行它会让 `call_timeout_ms: true` 变成 1 毫秒。
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise _invalid(_NOT_A_POSITIVE_INT, key)
    return value


def _string_list(
    block: Mapping[str, JsonValue], key: str, *, prefix: str
) -> tuple[str, ...]:
    value = block.get(key)
    if value is None:
        return ()
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or not all(isinstance(item, str) for item in value)
    ):
        raise _invalid(_NOT_A_STRING_LIST, _path(prefix, key))
    return tuple(str(item) for item in value)


def _string_map(
    block: Mapping[str, JsonValue], key: str, *, prefix: str
) -> Mapping[str, str]:
    value = block.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise _invalid(_NOT_AN_OBJECT, _path(prefix, key))
    result: dict[str, str] = {}
    for item_key, item in value.items():
        if not isinstance(item, str):
            raise _invalid(_NOT_A_STRING, _path(prefix, f"{key}.{item_key}"))
        result[item_key] = item
    return result


def _path(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else key


def _invalid(message: str, key: str, **detail: object) -> KaryviaError:
    return KaryviaError(
        ErrorCode.CONFIG_INVALID, message, detail={"key": f"plugins.mcp.config.{key}", **detail}
    )
