"""`commands_core` 的配置：命令名清单与单命令禁用（`TOL-006` 的同一条机制）。

职责：定义六个命令名常量，解析本内建的配置块，并导出 `enabled_command_names()` 供装配根
过滤 manifest 声明。
不负责：实现命令、渲染输出、读实例布局——本模块不碰 IO，只把一份 `Mapping` 变成设置。

**`enabled_command_names()` 是给装配根的**，与 `tools_fs` / `tools_shell` 的
`enabled_tool_names()` 同型：`CapabilityHost.finish()` 要求 manifest 声明的每一项都真的
被注册，而「按名字关掉一个命令」要求被关掉的那项从 registry 里消失。静态 manifest 无法
按配置少声明一项，因此由 `runtime/wiring.py` 的 `keep` 裁掉声明、由 `setup()` 用**同一份
配置**决定注册谁。两者同源是这条机制成立的全部条件（`D20` 的结论）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from karyvia.contracts import ErrorCode, JsonValue, KaryviaError

__all__ = [
    "COMMAND_NAMES",
    "CONFIG_DISABLE_KEY",
    "CONFIG_MAX_OUTPUT_CHARS_KEY",
    "CONFIG_PREFIX_KEY",
    "DEFAULT_MAX_OUTPUT_CHARS",
    "DEFAULT_PREFIX",
    "CommandsSettings",
    "enabled_command_names",
    "resolve_settings",
]

#: 六个命令名（技术方案 §8.1）。顺序即 `/help` 的默认展示顺序之外的唯一用途是断言，
#: 实际输出按名字排序（`CommandIndex.specs()` 已排好）。
COMMAND_NAMES: Final[tuple[str, ...]] = (
    "help",
    "config",
    "session",
    "plugins",
    "capabilities",
    "cancel",
)

CONFIG_DISABLE_KEY: Final = "disable"
CONFIG_MAX_OUTPUT_CHARS_KEY: Final = "max_output_chars"
CONFIG_PREFIX_KEY: Final = "prefix"

#: `/help` 里印出的命令前缀的兜底值。真值是 `routing.command_prefix`，由装配根经配置块
#: 交下来（`R4` 让内建够不着 `kernel/config`）。**与
#: `kernel/routing/dispatcher.py::DEFAULT_COMMAND_PREFIX` 各写一份**，有对照测试——
#: 与 `estimate_tokens` 在两处各写一份是同一种做法。
DEFAULT_PREFIX: Final = "/"

#: 单条命令输出的字符上限。命令输出直接进终端与聊天窗口，而 `/config` 在一份大配置上
#: 能轻易吐出几十 KB——那对聊天渠道是一次投递失败，对终端是一屏刷不完的噪声。
DEFAULT_MAX_OUTPUT_CHARS: Final = 16_384


@dataclass(frozen=True, slots=True)
class CommandsSettings:
    """本内建的生效设置。"""

    enabled: frozenset[str]
    max_output_chars: int
    #: `/help` 渲染用的命令前缀。它**不参与命令身份**，只影响展示。
    prefix: str = DEFAULT_PREFIX


def _disabled_names(config: Mapping[str, JsonValue]) -> frozenset[str]:
    """读 `disable` 列表。**表外的名字一律拒绝**。

    静默忽略一个拼错的命令名，用户会以为自己关掉了 `/config` 而它其实还在——
    这正是原则 7「不静默修正坏输入」要防的。
    """
    raw = config.get(CONFIG_DISABLE_KEY, ())
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        raise KaryviaError(
            ErrorCode.CONFIG_INVALID,
            "commands-core 的 disable 必须是命令名数组。",
            detail={"key": CONFIG_DISABLE_KEY},
        )
    names: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or item not in COMMAND_NAMES:
            raise KaryviaError(
                ErrorCode.CONFIG_INVALID,
                "commands-core 的 disable 里出现了未知命令名。",
                detail={"name": item if isinstance(item, str) else None,
                        "known": list(COMMAND_NAMES)},
            )
        names.add(item)
    return frozenset(names)


def enabled_command_names(config: Mapping[str, JsonValue]) -> frozenset[str]:
    """本次真正要注册的命令名。装配根用它过滤 manifest 声明（见模块 docstring）。"""
    return frozenset(COMMAND_NAMES) - _disabled_names(config)


def resolve_settings(config: Mapping[str, JsonValue]) -> CommandsSettings:
    """把配置块变成设置。**在 `setup()` 时校验一次**，不拖到第一次敲命令。

    **异常约定**：任何非法取值抛 `KaryviaError(CONFIG_INVALID)`。
    """
    raw = config.get(CONFIG_MAX_OUTPUT_CHARS_KEY, DEFAULT_MAX_OUTPUT_CHARS)
    # `bool` 是 `int` 的子类，`True` 会被当成 1 —— 那不是用户的意思。
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise KaryviaError(
            ErrorCode.CONFIG_INVALID,
            "commands-core 的 max_output_chars 必须是正整数。",
            detail={"key": CONFIG_MAX_OUTPUT_CHARS_KEY},
        )
    prefix = config.get(CONFIG_PREFIX_KEY, DEFAULT_PREFIX)
    if not isinstance(prefix, str) or not prefix:
        raise KaryviaError(
            ErrorCode.CONFIG_INVALID,
            "commands-core 的 prefix 必须是非空字符串。",
            detail={"key": CONFIG_PREFIX_KEY},
        )
    return CommandsSettings(
        enabled=enabled_command_names(config), max_output_chars=raw, prefix=prefix
    )
