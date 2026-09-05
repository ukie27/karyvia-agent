"""注册入口：`setup(api)`。

职责：把本内建的六个命令注册进能力表。
不负责：实现命令（`commands.py`）、声明自己（manifest 在 `builtins/registry.py`）。

**这是内建能力唯一的落地形态**：一份 manifest + 一个 `setup(api)`。`builtins/` 里不写任何
注册辅助函数——`R4` 拦得住 import，拦不住自建通道，
`tests/architecture/test_builtin_no_privilege.py` 的符号扫描是为此存在的。
"""

from __future__ import annotations

from karyvia.sdk import KaryviaAPI

from .commands import build_handlers
from .settings import resolve_settings

__all__ = ["setup"]


def setup(api: KaryviaAPI) -> None:
    """注册命令。

    **配置在这里校验一次**，不拖到第一次敲命令：一份写错的 `disable` 应当在启动时被指出来。
    失败会让 `commands-core` 单独落榜并留下诊断，实例仍然起得来——只是没有斜杠命令。
    """
    settings = resolve_settings(api.ctx.config)
    for spec, handler in build_handlers(api.ctx, settings).values():
        api.register_command(spec, handler)
