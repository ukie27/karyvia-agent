"""默认文件投递插件：向 Agent 提供 `file.send`。

职责：注册一个把 workspace 文件附加到当前回复的普通工具。
不负责：文件生成、Channel 上传、主动跨会话发消息或权限控制。它与外部插件使用同一份
manifest、`PluginContext` 和 Host 注册路径，不享有 Kernel 私有能力。
"""

from __future__ import annotations

from karyvia.sdk import KaryviaAPI

from .settings import (
    CONFIG_MAX_FILE_BYTES_KEY,
    DEFAULT_MAX_FILE_BYTES,
    resolve_max_file_bytes,
)
from .tool import FILE_SEND_SPEC, TOOL_NAME, FileSendTool

__all__ = [
    "CONFIG_MAX_FILE_BYTES_KEY",
    "DEFAULT_MAX_FILE_BYTES",
    "FILE_SEND_SPEC",
    "TOOL_NAME",
    "FileSendTool",
    "resolve_max_file_bytes",
    "setup",
]


def setup(api: KaryviaAPI) -> None:
    """注册 `file.send`；加载期只校验配置，不读取或创建任何文件。"""
    api.register_tool(
        FILE_SEND_SPEC,
        FileSendTool(api.ctx, max_file_bytes=resolve_max_file_bytes(api.ctx)),
    )
