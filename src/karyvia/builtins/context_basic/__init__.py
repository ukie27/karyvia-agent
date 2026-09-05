"""内建 Context Provider `context_basic`：系统指令 + 运行时事实（技术方案 §8.1）。

职责：作为本内建能力的公开门面，导出 `setup`（注册入口）、`BasicContextProvider`（实现）
与文本/估算辅助。
不负责：实现细节（在 `provider.py` 与 `instructions.py`）、声明自己（manifest 在
`builtins/registry.py`，那是内建能力唯一的发现来源）、重放历史与预算裁剪（组装器的事）。

**这是「无 Memory、无检索插件也能用」的那一份上下文**（`CTX-006`、`EDG-307`）：整个实现
不做任何 IO，因此它不可能因为缺少某个可选插件而失败。
"""

from __future__ import annotations

from .instructions import (
    BASELINE_INSTRUCTIONS,
    estimate_tokens,
    normalize_instructions,
    render_runtime_facts,
)
from .provider import (
    CAPABILITY_NAME,
    CONFIG_INSTRUCTIONS_KEY,
    CONFIG_RUNTIME_FACTS_KEY,
    CONFIG_USE_BASELINE_KEY,
    FRAGMENT_SOURCE,
    OPERATOR_PRIORITY,
    BasicContextProvider,
    BasicContextSettings,
    resolve_settings,
    setup,
)

__all__ = [
    "BASELINE_INSTRUCTIONS",
    "CAPABILITY_NAME",
    "CONFIG_INSTRUCTIONS_KEY",
    "CONFIG_RUNTIME_FACTS_KEY",
    "CONFIG_USE_BASELINE_KEY",
    "FRAGMENT_SOURCE",
    "OPERATOR_PRIORITY",
    "BasicContextProvider",
    "BasicContextSettings",
    "estimate_tokens",
    "normalize_instructions",
    "render_runtime_facts",
    "resolve_settings",
    "setup",
]
