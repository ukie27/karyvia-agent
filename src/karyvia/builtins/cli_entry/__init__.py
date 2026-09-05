"""内建 CLI 入口：`CLI_ENTRY:stdio` 与 `CHANNEL:cli` 两条能力（技术方案 §8.1、`BAS-009`）。

职责：解析本内建的配置块，装出一个共享的 `CliConsole`，并把入口与 Channel 两条能力注册
进 Host。
不负责：进程参数解析与实例组装（`runtime/cli/`）、把入站消息喂给 orchestrator（装配根的
Channel 泵）、信号处理（进程归 `runtime/`）。

**为什么是两条能力**：`CliEntry` 拥有进程（决定 `karyvia` 什么时候返回、返回什么退出码），
`Channel` 拥有消息路径（`MSG-007`：输入输出不得绕过 `InboundMessage` / `OutboundMessage`）。
合成一条就得让其中一件事走近路。两者共用同一个 `CliConsole`，那是它们唯一的耦合点。

stdin/stdout 是进程自己的 IO，不经过 Workspace 资源服务。要读写文件请用 `tools_fs`，
那里有路径守卫。

**`instance_id` 经配置块交下来**（`D17` 的 `dir`、`D20` 的 `workspace` 是同一条先例）：
`R4` 禁止 `builtins/` 够到 `kernel/`，内建不可能自己知道实例标识。装配根不填时退回
`default`——`InboundMessage.instance_id` 只是一个可追溯的标签，编排用的是
`OrchestratorDeps.instance_id`，因此填错不会让消息投错实例，只会让诊断难读。
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from typing import Final

from karyvia.contracts import ErrorCode, InstanceId, JsonValue, KaryviaError
from karyvia.sdk import KaryviaAPI, PluginContext

from .channel import CliChannel
from .console import DROPPED_ATTACHMENTS_KEY, TERMINAL_MARKERS, CliConsole, attachment_lines
from .entry import QUIT_WORDS, USAGE, StdioCliEntry

__all__ = [
    "CHANNEL_NAME",
    "CLI_ENTRY_NAME",
    "CONFIG_CHANNEL_ID_KEY",
    "CONFIG_CONVERSATION_ID_KEY",
    "CONFIG_INSTANCE_ID_KEY",
    "CONFIG_PROMPT_KEY",
    "CONFIG_REASONING_KEY",
    "CONFIG_USER_ID_KEY",
    "DEFAULT_CHANNEL_ID",
    "DEFAULT_CONVERSATION_ID",
    "DEFAULT_PROMPT",
    "DEFAULT_USER_ID",
    "DROPPED_ATTACHMENTS_KEY",
    "QUIT_WORDS",
    "TERMINAL_MARKERS",
    "USAGE",
    "CliChannel",
    "CliConsole",
    "CliSettings",
    "StdioCliEntry",
    "attachment_lines",
    "resolve_settings",
    "setup",
]

#: 能力名。`CLI_ENTRY` 是 SINGLETON，插件覆盖它要在 manifest 里写
#: `overrides: ["builtin:stdio"]`（`EDG-108` 的回落由装配根兑现）。
CLI_ENTRY_NAME: Final = "stdio"
CHANNEL_NAME: Final = "cli"

CONFIG_INSTANCE_ID_KEY: Final = "instance_id"
CONFIG_CHANNEL_ID_KEY: Final = "channel_id"
CONFIG_CONVERSATION_ID_KEY: Final = "conversation_id"
CONFIG_USER_ID_KEY: Final = "user_id"
CONFIG_PROMPT_KEY: Final = "prompt"
CONFIG_REASONING_KEY: Final = "show_reasoning"

DEFAULT_CHANNEL_ID: Final = "cli"
DEFAULT_CONVERSATION_ID: Final = "local"
DEFAULT_USER_ID: Final = "local"
#: 提示符只用 ASCII：Windows 中文控制台是 GBK，`»` 这类字符编不出来
#: （`console._write` 有降级，但提示符每行都印，不该每行都降级一次）。
DEFAULT_PROMPT: Final = "> "


class CliSettings:
    """本内建的生效配置。`__slots__` 而不是 dataclass，与其余内建的 settings 同型。"""

    __slots__ = (
        "channel_id",
        "conversation_id",
        "instance_id",
        "prompt",
        "show_reasoning",
        "user_id",
    )

    def __init__(
        self,
        *,
        instance_id: str,
        channel_id: str,
        conversation_id: str,
        user_id: str,
        prompt: str,
        show_reasoning: bool,
    ) -> None:
        self.instance_id = instance_id
        self.channel_id = channel_id
        self.conversation_id = conversation_id
        self.user_id = user_id
        self.prompt = prompt
        self.show_reasoning = show_reasoning


def _text(config: Mapping[str, JsonValue], key: str, fallback: str) -> str:
    value = config.get(key)
    if value is None:
        return fallback
    if not isinstance(value, str) or not value.strip():
        raise KaryviaError(
            ErrorCode.CONFIG_INVALID,
            "该字段必须是非空字符串。",
            detail={"pointer": f"/plugins/cli-entry/config/{key}"},
        )
    return value


def resolve_settings(ctx: PluginContext) -> CliSettings:
    """解析配置块。**在 `setup()` 时校验一次**，不拖到第一次输入（`D18` 的先例）。"""
    config: dict[str, JsonValue] = dict(ctx.config)
    reasoning = config.get(CONFIG_REASONING_KEY, False)
    if not isinstance(reasoning, bool):
        raise KaryviaError(
            ErrorCode.CONFIG_INVALID,
            "show_reasoning 必须是布尔值。",
            detail={"pointer": f"/plugins/cli-entry/config/{CONFIG_REASONING_KEY}"},
        )
    return CliSettings(
        instance_id=_text(config, CONFIG_INSTANCE_ID_KEY, "default"),
        channel_id=_text(config, CONFIG_CHANNEL_ID_KEY, DEFAULT_CHANNEL_ID),
        conversation_id=_text(config, CONFIG_CONVERSATION_ID_KEY, DEFAULT_CONVERSATION_ID),
        user_id=_text(config, CONFIG_USER_ID_KEY, DEFAULT_USER_ID),
        # 提示符允许是空串，因此不走 `_text`（它拒绝空白）。
        prompt=str(config.get(CONFIG_PROMPT_KEY, DEFAULT_PROMPT)),
        show_reasoning=reasoning,
    )


def build_console(settings: CliSettings) -> CliConsole:
    """按配置装一个控制台。输出恒为 `sys.stdout`——进程的标准输出就是 CLI 的界面。"""
    return CliConsole(
        instance_id=InstanceId(settings.instance_id),
        channel_id=settings.channel_id,
        conversation_id=settings.conversation_id,
        user_id=settings.user_id,
        out=sys.stdout,
        show_reasoning=settings.show_reasoning,
    )


def setup(api: KaryviaAPI) -> None:
    """内建注册入口。两条能力共用一个 `CliConsole`，见模块 docstring。"""
    settings = resolve_settings(api.ctx)
    console = build_console(settings)
    api.register_cli_entry(CLI_ENTRY_NAME, StdioCliEntry(console, prompt=settings.prompt))
    api.register_channel(CHANNEL_NAME, CliChannel(console))
