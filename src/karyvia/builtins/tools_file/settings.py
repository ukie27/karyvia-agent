"""`tools_file` 的单一大小配置。

职责：把插件自己的 `max_file_bytes` 配置校验为正整数。
不负责：决定 workspace、读取文件或注册工具；workspace 由宿主现有 `ctx.fs` 提供。
"""

from __future__ import annotations

from typing import Final

from karyvia.contracts import ErrorCode, KaryviaError
from karyvia.sdk import PluginContext

__all__ = [
    "CONFIG_MAX_FILE_BYTES_KEY",
    "DEFAULT_MAX_FILE_BYTES",
    "resolve_max_file_bytes",
]

CONFIG_MAX_FILE_BYTES_KEY: Final = "max_file_bytes"
DEFAULT_MAX_FILE_BYTES: Final = 25 * 1024 * 1024


def resolve_max_file_bytes(ctx: PluginContext) -> int:
    """读取单文件上限；非法配置在插件加载期失败。"""
    value = ctx.config.get(CONFIG_MAX_FILE_BYTES_KEY, DEFAULT_MAX_FILE_BYTES)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise KaryviaError(
            ErrorCode.CONFIG_INVALID,
            "「max_file_bytes」必须是正整数。",
            detail={"plugin": ctx.plugin_id, "key": CONFIG_MAX_FILE_BYTES_KEY},
        )
    return value
