"""内建注册入口：把启用的工具交给 Host。

职责：`setup(api)`、工具名到 `(spec, handler)` 的装配表。
不负责：声明自己（manifest 在 `builtins/registry.py`）、决定 workspace 在哪
（`settings.py`）、实现工具（`readers.py` / `writers.py` / `search.py`）。

**只注册启用的工具**。被 `disable` 关掉的工具在这里根本不会被 `register_tool`，因此它
不在 registry 里，也就不在模型可见的工具列表里—— 要的「可见列表与可执行集合
同源」不是靠两处保持一致维持的，而是因为只有一处。

装配根必须用**同一份配置**过滤 manifest 声明（`runtime/wiring.py` 的 `keep` 参数 +
`settings.enabled_tool_names`），否则的 `CapabilityHost.finish` 会以
`PLUGIN_LOAD_FAILED` 拒绝加载：manifest 声明了五个而这里只注册了四个。那个报错是对的，
它说的正是「你的声明和你的行为对不上」。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final

from karyvia.contracts import ToolSpec
from karyvia.sdk import KaryviaAPI

from .base import FsTool
from .paths import WorkspaceGuard
from .readers import LIST_SPEC, READ_SPEC, ListTool, ReadTool
from .search import GREP_SPEC, GrepTool
from .settings import FsToolSettings, resolve_settings
from .writers import EDIT_SPEC, WRITE_SPEC, EditTool, WriteTool

__all__ = ["TOOL_FACTORIES", "setup"]

#: 工具名 → `(声明, 实现的构造器)`。**键集必须恰好是 `TOOL_NAMES`**，由测试对照——
#: 少一项就是「声明了但造不出来」，多一项就是「造得出来但没人声明」。
TOOL_FACTORIES: Final[
    Mapping[str, tuple[ToolSpec, Callable[[WorkspaceGuard, FsToolSettings], FsTool]]]
] = {
    "fs.read": (READ_SPEC, ReadTool),
    "fs.write": (WRITE_SPEC, WriteTool),
    "fs.edit": (EDIT_SPEC, EditTool),
    "fs.list": (LIST_SPEC, ListTool),
    "fs.grep": (GREP_SPEC, GrepTool),
}


def setup(api: KaryviaAPI) -> None:
    """内建注册入口，`BUILTIN_MANIFESTS` 里那条 manifest 的 `setup` 指向它。

    配置在这里校验一次并固化进每个 handler：一份写错的 workspace 路径或一个拼错的工具名
    应当让加载当场失败，而不是等模型第一次调 `fs.read` 才炸。

    **这里不做任何 IO**：workspace 目录在第一次写入时才被创建（`fs.write` 的
    `mkdir(parents=True)`），因此 `karyvia capabilities` 这类只读命令不会在磁盘上留下痕迹。
    """
    settings = resolve_settings(api.ctx)
    guard = WorkspaceGuard(settings.workspace)
    for name in settings.enabled:
        spec, factory = TOOL_FACTORIES[name]
        api.register_tool(spec, factory(guard, settings))
