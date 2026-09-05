"""默认 Turn Context Compactor 内建插件。

它与第三方插件走同一条 manifest、Host、Registry 和选择路径；唯一默认性来自 Runtime 配置
默认选中能力名 `basic`，没有 Kernel 私有注册或压缩兜底。
"""

from __future__ import annotations

from karyvia.sdk import KaryviaAPI

from .compactor import BasicTurnContextCompactor

__all__ = ["BasicTurnContextCompactor", "setup"]


def setup(api: KaryviaAPI) -> None:
    """注册默认 Turn 内压缩策略。"""
    api.register_turn_compactor("basic", BasicTurnContextCompactor())
