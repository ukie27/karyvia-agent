"""Token 用量：`usage` 块的数据来源。

职责：订阅 `model.response_received`，按 turn 累加 token 用量，供 HTTP 处理器在终态
分片里填 OpenAI 的 `usage` 块。
不负责：定义用量本身（`contracts.TokenUsage`）、决定何时发送（`http.py`）。

用量只来自事件总线上的 `model.response_received` 载荷（
`kernel/turn/orchestrator.py` 的那**唯一**发布点补了 `input_tokens` / `output_tokens`
两个键）。订阅事件是只读可观测性，与 `ctx.instance`
同一档。

**语义与 OpenAI 不同，如实写在这里**：报出来的是**整条 turn 全部迭代之和**（含工具
往返），而不是最后一次模型调用。一次带三轮工具调用的对话因此报的是三次请求的总和，
那比只报最后一次诚实——用户付的正是总和。
"""

from __future__ import annotations

from karyvia.contracts import EventName, RuntimeEvent, TurnId
from karyvia.sdk import EventSubscriber

__all__ = ["TurnUsage", "UsageTracker"]


class TurnUsage:
    """一条 turn 的累计用量。"""

    __slots__ = ("input_tokens", "output_tokens")

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class UsageTracker:
    """按 turn 累加用量。有界：一条 turn 结束（被 `take()` 取走）即移除。"""

    #: 未被取走的条目上限。正常路径上每条 turn 都会被 `take()`，这个上限防的是
    #: 「turn 由 CLI 发起、从没有人来取」——那类条目会一直堆着。
    MAX_TRACKED = 256

    def __init__(self) -> None:
        self._turns: dict[TurnId, TurnUsage] = {}

    def subscribe(self, events: EventSubscriber) -> None:
        events.subscribe(EventName.MODEL_RESPONSE_RECEIVED, self.observe)

    def observe(self, event: RuntimeEvent) -> None:
        """事件回调。**同步、绝不抛**（`EventBus` 会因为连续失败把订阅者熔断掉）。"""
        correlation = event.correlation
        if correlation is None:
            return
        payload = event.payload
        usage = self._turns.get(correlation.turn_id)
        if usage is None:
            if len(self._turns) >= self.MAX_TRACKED:
                self._turns.pop(next(iter(self._turns)), None)
            usage = TurnUsage()
            self._turns[correlation.turn_id] = usage
        usage.input_tokens += _int(payload.get("input_tokens"))
        usage.output_tokens += _int(payload.get("output_tokens"))

    def take(self, turn_id: TurnId | None) -> TurnUsage | None:
        """取走并移除一条 turn 的用量。**没有就返回 `None`**——不编零。

        编一个 `0` 会让「这次真的没花 token」与「我们没看到用量」在客户端那里不可区分，
        而后者恰恰是配置或版本不匹配的信号。
        """
        if turn_id is None:
            return None
        return self._turns.pop(turn_id, None)


def _int(value: object) -> int:
    """载荷里的整数。事件载荷是 JSON，形状不对时按 0 处理而不是抛。"""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
