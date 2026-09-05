"""内建 shell 工具 `tools_shell`：`shell.exec`（技术方案 §8.2 的第 6 个）。

职责：作为本内建能力的公开门面，导出 `setup`（注册入口）、`ToolSpec` 与实现，
以及配置与环境变量处理。
不负责：实现细节（在子模块）、声明自己（manifest 在 `builtins/registry.py`，那是内建
能力唯一的发现来源）、决定 workspace 根在哪（装配根经 `ctx.config["workspace"]` 交下来）。

**取消宽限期用尽时副作用是 `UNKNOWN`**：这是与 `tools_fs` 唯一一处语义差异——文件工具的
失败全部发生在落盘之前（临时文件 + `os.replace`），因此它们一次 `UNKNOWN` 都不产出；
而 `shell.exec` 的进程可能写了一半文件、改了一半配置，宽限期用尽后 Kernel 确实不知道
外部世界变成什么样了（`EDG-407`）。

**`enabled_tool_names()` 是给装配根的**：与 `tools_fs` 同一条机制（`TOL-006`），只是这里
只有一个工具名。装配根用它过滤 manifest 声明，`setup()` 用同一份设置决定注册谁。
"""

from __future__ import annotations

from .executor import EXEC_SPEC, ShellExecutor
from .registration import setup
from .settings import (
    CONFIG_DISABLE_KEY,
    CONFIG_ENV_KEY,
    CONFIG_MAX_OUTPUT_CHARS_KEY,
    CONFIG_PASS_ENV_KEY,
    CONFIG_SHELL_KEY,
    CONFIG_TIMEOUT_KEY,
    CONFIG_WORKSPACE_KEY,
    DEFAULT_MAX_OUTPUT_CHARS,
    DEFAULT_TIMEOUT_MS,
    TOOL_NAME,
    ShellToolSettings,
    enabled_tool_names,
    resolve_settings,
)

__all__ = [
    "CONFIG_DISABLE_KEY",
    "CONFIG_ENV_KEY",
    "CONFIG_MAX_OUTPUT_CHARS_KEY",
    "CONFIG_PASS_ENV_KEY",
    "CONFIG_SHELL_KEY",
    "CONFIG_TIMEOUT_KEY",
    "CONFIG_WORKSPACE_KEY",
    "DEFAULT_MAX_OUTPUT_CHARS",
    "DEFAULT_TIMEOUT_MS",
    "EXEC_SPEC",
    "TOOL_NAME",
    "ShellExecutor",
    "ShellToolSettings",
    "enabled_tool_names",
    "resolve_settings",
    "setup",
]
