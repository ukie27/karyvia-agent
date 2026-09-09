"""`tools_shell` 的注册：唯一的 `setup` 入口。

职责：实现 manifest 声明的 `setup`——解析配置、构造 `ShellExecutor`、调 `api.register_tool`。
不负责：构造 `PluginManifest`（那在 `builtins/registry.py`，是内建能力唯一的发现来源）、
判定冲突（`kernel.registry`）。

**内建不享受特权**：这里的注册调用与外部插件写的那段代码是同一个形状——
没有一个「只有内建能用」的 API，也没有一条「内建优先级默认为 0」的特殊分支。基准优先级
是装配根在翻译 manifest 时给上去的（`runtime/wiring.py`），本函数看不见也改不了它。
"""

from __future__ import annotations

from karyvia.sdk import KaryviaAPI

from .executor import EXEC_SPEC, ShellExecutor
from .paths import CwdGuard
from .settings import resolve_settings

__all__ = ["setup"]


def setup(api: KaryviaAPI) -> None:
    """内建注册入口，`BUILTIN_MANIFESTS` 里那条 manifest 的 `setup` 指向它。

    配置在这里校验一次并固化进 handler：一份写错的 workspace 路径或一个拼错的工具名应当
    让加载当场失败，而不是等模型第一次调 `shell.exec` 才炸。

    被 `disable` 关掉时**一个都不注册**，因此 `shell.exec` 不在模型可见的工具列表里——
     要的「可见列表与可执行集合同源」不是靠两处保持一致维持的，而是因为只有一处。
    装配根必须用**同一份配置**过滤 manifest 声明（`runtime/wiring.py` 的 `keep` 参数 +
    `settings.enabled_tool_names()`），否则 `CapabilityHost.finish()` 会以
    `PLUGIN_LOAD_FAILED` 拒绝加载——manifest 声明了一个而这里注册了零个。那个报错是对的。

    **这里不做任何 IO**：workspace 目录不在此创建，cwd 不存在时由 `execute()` 折成结果，
    因此 `karyvia capabilities` 这类只读命令不会在磁盘上留下痕迹（与 `tools_fs.setup` 同）。
    """
    settings = resolve_settings(api.ctx)
    if not settings.enabled:
        return
    api.register_tool(
        EXEC_SPEC, ShellExecutor(CwdGuard(settings.workspace, settings.skill_roots), settings)
    )
