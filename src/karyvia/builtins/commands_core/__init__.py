"""内建命令集 `commands_core`：`/help` `/config` `/session` `/plugins` `/capabilities`
`/cancel`（技术方案 §8.1）。

职责：作为本内建能力的公开门面，导出 `setup`（注册入口）、六个 `CommandSpec`、配置解析与
渲染函数。
不负责：实现细节（在子模块）、声明自己（manifest 在 `builtins/registry.py`，那是内建能力
唯一的发现来源）、提供数据——数据全部来自 `ctx.instance` 与 `ctx.turns`。

**本内建是 `D22` 扩 `PluginContext` 那个决定的第一个消费者**。六个命令里有五个要的数据在
`kernel/registry` 与 `kernel/observability` 里，而 `R4` 禁止 `builtins/` import `kernel/`。
四条路（扩 `PluginContext` / 由 `runtime/` 特权注册 / 快照塞进 `ctx.config` / 改注册载荷
形状）里取的是第一条：`/plugins`、`/capabilities` 这类命令**本来就该是第三方插件能写的
东西**，而其余三条都在给「内建是特殊的」找借口（`BAS-005`）。代价是动了冻结的 SDK 表面，
两处快照因此同步更新过（`tests/contracts/test_protocols.py`、
`tests/sdk/test_public_surface.py`）。

**`enabled_command_names()` 是给装配根的**：与 `tools_fs` / `tools_shell` 同一条机制
（`TOL-006`），装配根用它过滤 manifest 声明，`setup()` 用同一份配置决定注册谁。
"""

from __future__ import annotations

from .commands import SPECS, build_handlers
from .registration import setup
from .render import (
    render_capabilities,
    render_config,
    render_help,
    render_plugins,
    render_session,
    render_turns,
    truncate,
)
from .settings import (
    COMMAND_NAMES,
    CONFIG_DISABLE_KEY,
    CONFIG_MAX_OUTPUT_CHARS_KEY,
    CONFIG_PREFIX_KEY,
    DEFAULT_MAX_OUTPUT_CHARS,
    DEFAULT_PREFIX,
    CommandsSettings,
    enabled_command_names,
    resolve_settings,
)

__all__ = [
    "COMMAND_NAMES",
    "CONFIG_DISABLE_KEY",
    "CONFIG_MAX_OUTPUT_CHARS_KEY",
    "CONFIG_PREFIX_KEY",
    "DEFAULT_MAX_OUTPUT_CHARS",
    "DEFAULT_PREFIX",
    "SPECS",
    "CommandsSettings",
    "build_handlers",
    "enabled_command_names",
    "render_capabilities",
    "render_config",
    "render_help",
    "render_plugins",
    "render_session",
    "render_turns",
    "resolve_settings",
    "setup",
    "truncate",
]
