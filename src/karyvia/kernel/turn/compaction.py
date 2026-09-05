"""Turn 压缩结果与 Session 持久化水位之间的映射。

职责：识别最终模型请求中仍保持原样的 Session 历史，并把只覆盖该历史连续前缀的 Turn
摘要登记为待提交项。
不负责：触发或执行压缩、写 Session、处理临时 Context/Memory 片段。

请求级摘要可以立即服务当前 Turn；只有能够映射到既有 Session 记录的摘要才会在 Turn
收口时持久化。映射失败表示 Hook 或临时上下文改变了消息来源，此时不猜测水位。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from karyvia.contracts import ModelMessage

from .context_builder import ReplayedMessage

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["PendingSessionCompaction", "SessionCompactionTracker"]


@dataclass(frozen=True, slots=True)
class PendingSessionCompaction:
    """一个已验证、等待在 Turn 收口时写入 Session 的摘要。"""

    through: int
    summary: ModelMessage


class SessionCompactionTracker:
    """保存本 Turn 初始 Session 投影，并登记可持久化的最远压缩水位。"""

    __slots__ = ("_history", "_pending")

    def __init__(self, history: Sequence[ReplayedMessage]) -> None:
        self._history = tuple(history)
        self._pending: PendingSessionCompaction | None = None

    @property
    def pending(self) -> PendingSessionCompaction | None:
        return self._pending

    def record(
        self,
        covered: Sequence[ModelMessage],
        summary: ModelMessage,
    ) -> None:
        """仅当覆盖范围是初始 Session 投影的连续前缀时登记摘要。"""
        count = len(covered)
        if count == 0 or count > len(self._history):
            return
        prefix = self._history[:count]
        if any(item.message is not message for item, message in zip(prefix, covered, strict=True)):
            return
        through = prefix[-1].through
        if self._pending is None or through >= self._pending.through:
            self._pending = PendingSessionCompaction(through=through, summary=summary)
