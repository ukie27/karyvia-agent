"""内建文件工具 `tools_fs`：`fs.read` / `fs.write` / `fs.edit` / `fs.list` / `fs.grep`
（技术方案 §8.2 的冻结清单，`shell.exec` 是 `D21`）。

职责：作为本内建能力的公开门面，导出 `setup`（注册入口）、五个 `ToolSpec` 与实现，
以及配置与路径守卫。
不负责：实现细节（在各子模块）、声明自己（manifest 在 `builtins/registry.py`，那是内建
能力唯一的发现来源）、决定 workspace 根在哪（装配根经 `ctx.config["workspace"]` 交下来）。

**这是 `NFR-302` 的唯一防线**：全部路径判定收在 `paths.WorkspaceGuard` 一处，逻辑校验 +
realpath 校验缺一不可。它是应用级守卫而不是 OS 沙箱——TOCTOU 窗口如实写在那里。

**`enabled_tool_names()` 是给装配根的**：`D16` 要求 manifest 声明的每一项都真的被注册，
而 `TOL-006` 要求被禁用的工具从 registry 里消失。两者靠「声明与注册同源于同一份配置」
调和，装配根用它过滤 manifest 声明，`setup()` 用同一份设置决定注册谁。
"""

from __future__ import annotations

from .base import FsTool
from .content import (
    BINARY_SNIFF_BYTES,
    REPLACEMENT_CHAR,
    decode_text,
    looks_binary,
    normalize_newlines,
    truncate,
)
from .paths import RESERVED_DEVICE_NAMES, WorkspaceGuard
from .readers import LIST_SPEC, READ_SPEC, ListTool, ReadTool
from .registration import TOOL_FACTORIES, setup
from .search import GREP_SPEC, MAX_PATTERN_LENGTH, GrepTool
from .settings import (
    CONFIG_DISABLE_KEY,
    CONFIG_MAX_ENTRIES_KEY,
    CONFIG_MAX_MATCHES_KEY,
    CONFIG_MAX_READ_BYTES_KEY,
    CONFIG_MAX_RESULT_CHARS_KEY,
    CONFIG_WORKSPACE_KEY,
    DEFAULT_MAX_ENTRIES,
    DEFAULT_MAX_MATCHES,
    DEFAULT_MAX_READ_BYTES,
    DEFAULT_MAX_RESULT_CHARS,
    TOOL_NAMES,
    FsToolSettings,
    enabled_tool_names,
    resolve_settings,
)
from .writers import EDIT_SPEC, WRITE_SPEC, EditTool, WriteTool

__all__ = [
    "BINARY_SNIFF_BYTES",
    "CONFIG_DISABLE_KEY",
    "CONFIG_MAX_ENTRIES_KEY",
    "CONFIG_MAX_MATCHES_KEY",
    "CONFIG_MAX_READ_BYTES_KEY",
    "CONFIG_MAX_RESULT_CHARS_KEY",
    "CONFIG_WORKSPACE_KEY",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_MAX_MATCHES",
    "DEFAULT_MAX_READ_BYTES",
    "DEFAULT_MAX_RESULT_CHARS",
    "EDIT_SPEC",
    "GREP_SPEC",
    "LIST_SPEC",
    "MAX_PATTERN_LENGTH",
    "READ_SPEC",
    "REPLACEMENT_CHAR",
    "RESERVED_DEVICE_NAMES",
    "TOOL_FACTORIES",
    "TOOL_NAMES",
    "WRITE_SPEC",
    "EditTool",
    "FsTool",
    "FsToolSettings",
    "GrepTool",
    "ListTool",
    "ReadTool",
    "WorkspaceGuard",
    "WriteTool",
    "decode_text",
    "enabled_tool_names",
    "looks_binary",
    "normalize_newlines",
    "resolve_settings",
    "setup",
    "truncate",
]
