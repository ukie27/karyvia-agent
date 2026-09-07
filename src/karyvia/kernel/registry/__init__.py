"""能力注册表：注册、暂存提交与覆盖解析。

职责：re-export `capability`（登记与冻结）与 `resolution`（覆盖解析与报告）两个模块的
公开表面，使调用方只需要 `from karyvia.kernel.registry import ...` 一条导入路径。
不负责：决定谁被加载、构造 `PluginContext`——那些在 `kernel/plugins/`
；本包不读文件、不访问网络。

两个模块的分工是单向的：`resolution` 依赖 `capability`，反过来不成立。注册表只管
「谁登记了什么」，「谁最终生效」是解析器的结论——把两件事写进一个类，冲突语义就会散落
在每个注册点上，而 「覆盖永不由加载顺序决定」正是那样丢掉的。
"""

from __future__ import annotations

from .capability import (
    BUILTIN_BASE_PRIORITY,
    PLUGIN_BASE_PRIORITY,
    BatchState,
    CapabilityRegistry,
    Registration,
    RegistrationBatch,
    base_priority_for,
)
from .resolution import (
    Resolution,
    ResolutionReport,
    resolve,
    resolve_into,
)

__all__ = [
    "BUILTIN_BASE_PRIORITY",
    "PLUGIN_BASE_PRIORITY",
    "BatchState",
    "CapabilityRegistry",
    "Registration",
    "RegistrationBatch",
    "Resolution",
    "ResolutionReport",
    "base_priority_for",
    "resolve",
    "resolve_into",
]
