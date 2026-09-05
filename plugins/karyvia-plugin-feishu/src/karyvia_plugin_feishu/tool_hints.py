"""工具进度提示的渲染（开发方案 `D34`）。

职责：把一批工具名渲染成用户看得懂的提示块，连续同名折叠成 `名 × N`。
不负责：拿到工具名（`channel.py` 订阅 `tool.call_started`）、把它送进卡片
（`stream.py` 的 `note()` / `flush()`）、决定什么时候显示（`channel.py` 的泵）。

**只有工具名，没有参数——这是相对 legacy 的一处如实回退。** legacy 读的是
`ToolCallRequest.arguments`，能渲染出 `$ ls -la`、`read foo.py` 这样带参数的行；而新层里
Channel 拿得到的工具信息只有 `tool.call_started` 的载荷 `{"tool", "call_id"}`
（`kernel/turn/orchestrator.py` 那唯一的发布点）。要恢复参数级细节就得往那个事件的载荷里
加工具参数，而工具参数里装着文件内容、绝对路径与 shell 命令——把它们塞进一条会被全部
订阅者看到、会落进事件日志、还会被发到聊天平台上的载荷里，是一次要单独评审的脱敏决定，
不是一个 Channel 插件可以顺手做的事。

**折叠规则逐字沿用 legacy 的 `format_tool_hints`**：只折叠**相邻**的同名调用，不做全局
计数。一次并行发起的五个 `fs.read` 因此显示成一行 `🔧 fs.read × 5`，而「读—写—读」
仍然是三行——顺序是用户判断 agent 在干什么的主要线索，排序或去重会把它抹掉。
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = ["DEFAULT_PREFIX", "render"]

#: 提示行的默认前缀。legacy 的 `tool_hint_prefix` 默认值，一个字符都没改。
DEFAULT_PREFIX = "🔧"

#: 折叠计数的连接符。legacy 用的是全角乘号而不是字母 x。
_TIMES = "×"


def render(prefix: str, names: Sequence[str]) -> str:
    """把一批工具名渲染成提示块（多行）。

    `prefix` 为空串即**关闭**提示，恒返回空串——运维不想要这个东西时不该还得忍受一个
    没有前缀的裸工具名。空白名字丢弃：畸形的工具调用不该让整块提示消失。
    """
    if not prefix:
        return ""
    lines: list[tuple[str, int]] = []
    for name in names:
        cleaned = name.strip()
        if not cleaned:
            continue
        if lines and lines[-1][0] == cleaned:
            lines[-1] = (cleaned, lines[-1][1] + 1)
            continue
        lines.append((cleaned, 1))
    return "\n".join(
        f"{prefix} {name} {_TIMES} {count}" if count > 1 else f"{prefix} {name}"
        for name, count in lines
    )
