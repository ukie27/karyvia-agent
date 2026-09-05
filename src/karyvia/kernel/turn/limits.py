"""预算：`TurnLimits` 六项上限与 `BudgetLedger` 账本（技术方案 §6.4、需求 `KER-009`、`TOL-002`、`TOL-007`）。

职责：定义一次 turn 的六项预算及其保守默认值、每项触发时的终态语义（`LIMIT_OUTCOMES`），
以及在 turn 执行期间记账并判定是否越界的 `BudgetLedger`。
不负责：执行循环、发布事件、超时地取消工具、读配置——循环在 `engine.py`（`D09`），
配置在 `kernel/config/`（`D10`）；本模块不含 IO，也不自己读时钟以外的任何外部状态。

**「缺省配置下不存在无界执行路径」（`KER-009`）落在哪里**：`max_iterations` 与
`max_tool_calls_per_turn` 都有非空默认值且必须为正，`BudgetLedger.check()` 在每次迭代前
被调用并在耗尽时返回 `LimitBreach`。这条性质由 `tests/kernel/test_limits.py` 用一个永远
返回 tool_call 的假模型驱动的最小循环断言——不是靠「引擎会记得检查」这种口头约定。

**为什么账本与上限分成两个类**：`TurnLimits` 是配置（不可变、可共享、可序列化），
`BudgetLedger` 是一次 turn 的状态（可变、per-turn）。把计数器塞进配置对象，
就等于让两个并发 turn 共享同一份计数——而 `KER-008` 允许同实例多 session 并发。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from karyvia.contracts import (
    CancelReason,
    ErrorCode,
    KaryviaError,
    ModelInfo,
    TurnStatus,
)

from .request_size import ContextBudget

__all__ = [
    "DEFAULT_MAX_ITERATIONS",
    "DEFAULT_MAX_TOOL_CALLS_PER_TURN",
    "DEFAULT_TOOL_RESULT_MAX_BYTES",
    "DEFAULT_TOOL_TIMEOUT_MS",
    "DEFAULT_TURN_TIMEOUT_MS",
    "FALLBACK_CONTEXT_MAX_TOKENS",
    "LIMIT_OUTCOMES",
    "BudgetLedger",
    "LimitBreach",
    "LimitKind",
    "TurnLimits",
]

#: 六项默认值，全部取自技术方案 §6.4 的表格。改动默认值等于改动「开箱即用」的行为，
#: 必须同步技术方案——测试以字面量断言这六个数字，正是为了让这种改动无法悄悄发生。
DEFAULT_MAX_ITERATIONS: Final = 16
DEFAULT_MAX_TOOL_CALLS_PER_TURN: Final = 48
DEFAULT_TOOL_TIMEOUT_MS: Final = 120_000
DEFAULT_TOOL_RESULT_MAX_BYTES: Final = 65_536
DEFAULT_TURN_TIMEOUT_MS: Final = 900_000

#: `context_max_tokens` 的兜底值：技术方案写的是「由模型能力推导」，但 `ModelInfo` 允许
#: Provider 不声明窗口（`context_window_tokens=0`）。此时既不能假设窗口很大，也不能让
#: 请求预算不能无上限——取一个保守值，宁可提前压缩也不生成超限请求
#: （`CTX-003`）。
FALLBACK_CONTEXT_MAX_TOKENS: Final = 8_192


class LimitKind(StrEnum):
    """六项预算的标识。取值与 `TurnLimits` 的字段名逐一相等（有对照测试）。

    相等不是巧合而是约束：越界报告要能直接指回「改哪个配置项」，中间隔一层翻译表
    就会出现「报的名字和配置里的名字对不上」这种只在用户那边才发现的问题。
    """

    MAX_ITERATIONS = "max_iterations"
    MAX_TOOL_CALLS_PER_TURN = "max_tool_calls_per_turn"
    TOOL_TIMEOUT_MS = "tool_timeout_ms"
    TOOL_RESULT_MAX_BYTES = "tool_result_max_bytes"
    TURN_TIMEOUT_MS = "turn_timeout_ms"
    CONTEXT_MAX_TOKENS = "context_max_tokens"


#: 每项越界后的 turn 终态（技术方案 §6.4 表格「触发行为」列）。
#: `None` 表示**这项越界不终止 turn**：单工具超时只让那一次调用失败，结果截断只置
#: `truncated=True`，上下文超限触发压缩——三者都不该直接决定整轮终态。
#: 六个 kind 全部登记，缺项直接 KeyError：终态未定的预算项不该有触发路径。
LIMIT_OUTCOMES: Final[Mapping[LimitKind, tuple[TurnStatus | None, CancelReason | None]]] = (
    MappingProxyType(
        {
            LimitKind.MAX_ITERATIONS: (TurnStatus.STOPPED_BY_LIMIT, None),
            LimitKind.MAX_TOOL_CALLS_PER_TURN: (TurnStatus.STOPPED_BY_LIMIT, None),
            LimitKind.TOOL_TIMEOUT_MS: (None, None),
            LimitKind.TOOL_RESULT_MAX_BYTES: (None, None),
            # turn 总超时走取消而不是 STOPPED_BY_LIMIT：用户看到的是「这轮被时间掐了」，
            # 与「模型自己绕了 16 圈」是两种不同的事故，善后动作也不同（前者要保存
            # 已产生的内容并标记 interrupted）。
            LimitKind.TURN_TIMEOUT_MS: (TurnStatus.CANCELLED, CancelReason.TIMEOUT),
            LimitKind.CONTEXT_MAX_TOKENS: (None, None),
        }
    )
)


@dataclass(frozen=True, slots=True)
class LimitBreach:
    """一次越界的完整记录（`KER-005`：达到上限必须正常终止并返回**可诊断**结果）。

    带上 `limit` 与 `observed` 两个数字，用户才知道是「差一点」还是「离谱地超了」，
    也才知道把配置调到多少能过。只报一个 kind 等于让用户自己去翻默认值。
    """

    kind: LimitKind
    limit: int
    observed: int

    def __post_init__(self) -> None:
        if self.kind not in LIMIT_OUTCOMES:  # pragma: no cover - 枚举已穷举，防御性分支。
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "未登记终态的预算项不得产生越界记录。",
                detail={"kind": self.kind.value},
            )

    @property
    def terminal_status(self) -> TurnStatus | None:
        """越界导致的 turn 终态；`None` 表示 turn 继续。"""
        return LIMIT_OUTCOMES[self.kind][0]

    @property
    def cancel_reason(self) -> CancelReason | None:
        """终态为 `CANCELLED` 时的取消原因，否则 `None`。"""
        return LIMIT_OUTCOMES[self.kind][1]

    def describe(self) -> str:
        """给用户看的一句话说明。不含内部路径与堆栈，可直接进 `OutboundMessage`。"""
        return f"已达到预算上限 {self.kind.value}（上限 {self.limit}，实际 {self.observed}）。"

    def to_detail(self) -> dict[str, str | int]:
        """事件与错误 `detail` 用的扁平字典。"""
        return {"limit_kind": self.kind.value, "limit": self.limit, "observed": self.observed}


@dataclass(frozen=True, slots=True)
class TurnLimits:
    """一次 turn 的六项预算（技术方案 §6.4、`KER-009`、`TOL-002`、`TOL-007`）。

    恰好六项，与 `LimitKind` 一一对应。**不要往这里加第七项**而不同时更新 `LimitKind`、
    `LIMIT_OUTCOMES` 与对照测试：没有终态语义的上限只是一个没人知道该怎么处理的数字。
    取消宽限期（`DEFAULT_TOOL_CANCEL_GRACE_MS`）不在此列——它是取消语义的参数，
    不是「一次 turn 能用掉多少」。

    `context_max_tokens` 允许为 `None`，含义是「由模型能力推导」而不是「无限制」：
    推导逻辑在 `resolve_context_max_tokens()` 里，任何取值路径都会得到一个有限的正数。
    """

    max_iterations: int = DEFAULT_MAX_ITERATIONS
    max_tool_calls_per_turn: int = DEFAULT_MAX_TOOL_CALLS_PER_TURN
    tool_timeout_ms: int = DEFAULT_TOOL_TIMEOUT_MS
    tool_result_max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES
    turn_timeout_ms: int = DEFAULT_TURN_TIMEOUT_MS
    context_max_tokens: int | None = None

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if value is None:
                continue
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise KaryviaError(
                    ErrorCode.CONFIG_INVALID,
                    "turn 预算必须是正整数。",
                    detail={"field": field.name, "value": repr(value)},
                )

    def limit_for(self, kind: LimitKind) -> int | None:
        """按 `LimitKind` 取值。字段名与枚举值相等，这里因此不需要翻译表。"""
        value = getattr(self, kind.value)
        return value if value is None else int(value)

    def resolve_context_max_tokens(self, model: ModelInfo | None = None) -> int:
        """推导本次 turn 的上下文 token 预算（`CTX-003`）。

        优先级：显式配置 > 模型声明的窗口减去最大输出 > `FALLBACK_CONTEXT_MAX_TOKENS`。
        减去 `max_output_tokens` 是因为输出也占窗口——把整个窗口都填成输入，
        请求本身合法但模型没有地方写回答。

        **异常约定**：模型声明的最大输出不小于窗口时抛 `CONFIG_INVALID`。那份声明自相
        矛盾，静默按窗口处理只会把错误推迟到一次 400 响应上（`MOD-005`：不得静默降级）。
        """
        model_budget: int | None = None
        if model is not None and model.context_window_tokens > 0:
            model_budget = model.context_window_tokens - model.max_output_tokens
        if model is not None and model_budget is not None and model_budget <= 0:
            raise KaryviaError(
                ErrorCode.CONFIG_INVALID,
                "模型声明的最大输出不小于上下文窗口，无法推导上下文预算。",
                detail={
                    "model_id": model.model_id,
                    "context_window_tokens": model.context_window_tokens,
                    "max_output_tokens": model.max_output_tokens,
                },
            )
        configured = self.context_max_tokens
        if configured is not None and model_budget is not None:
            return min(configured, model_budget)
        if configured is not None:
            return configured
        if model_budget is not None:
            return model_budget
        return FALLBACK_CONTEXT_MAX_TOKENS

    def resolve_context_budget(self, model: ModelInfo | None = None) -> ContextBudget:
        """返回 Context、持久化压缩与 Turn 压缩共用的预算事实。"""
        return ContextBudget.from_hard_limit(self.resolve_context_max_tokens(model))

    def truncate_tool_result(self, content: str) -> tuple[str, bool]:
        """按 `tool_result_max_bytes` 截断工具结果，返回 `(content, truncated)`（`EDG-403`）。

        按 **UTF-8 字节**而不是字符数计——上限约束的是进上下文的实际体积，
        而一个中文字符是三个字节。截断在字符边界上完成，不产生半个字符。
        不追加「已截断」提示：那会让结果反过来超出上限，标记由 `ToolResult.truncated`
        承载，渲染是调用方的事。
        """
        raw = content.encode("utf-8")
        if len(raw) <= self.tool_result_max_bytes:
            return content, False
        return raw[: self.tool_result_max_bytes].decode("utf-8", errors="ignore"), True


class BudgetLedger:
    """一次 turn 的预算账本：记账 + 判定越界（`KER-009`）。

    时钟由构造参数注入，默认 `time.monotonic`。注入不是为了「方便测试」而已——turn 总超时
    必须用单调时钟，墙钟回拨会让一个正常的 turn 突然超时或者永远不超时。让调用方能换掉
    它，测试才不必真的睡 900 秒。
    """

    __slots__ = ("_clock", "_iterations", "_limits", "_started_at", "_tool_calls")

    def __init__(
        self,
        limits: TurnLimits | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limits = limits if limits is not None else TurnLimits()
        self._clock = clock
        self._started_at = clock()
        self._iterations = 0
        self._tool_calls = 0

    @property
    def limits(self) -> TurnLimits:
        return self._limits

    @property
    def iterations(self) -> int:
        return self._iterations

    @property
    def tool_calls(self) -> int:
        return self._tool_calls

    @property
    def elapsed_ms(self) -> int:
        return int((self._clock() - self._started_at) * 1000)

    def remaining_ms(self) -> int:
        """距 turn 总超时还剩多少毫秒，最小为 0。`D09` 用它给模型请求设上限。"""
        return max(0, self._limits.turn_timeout_ms - self.elapsed_ms)

    def begin_iteration(self) -> None:
        """记一次模型迭代。调用方应先 `check()` 再 begin，见类型注释与测试。"""
        self._iterations += 1

    def record_tool_calls(self, count: int = 1) -> None:
        """记 `count` 次工具调用。"""
        if count < 0:
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "工具调用计数不得为负。",
                detail={"count": count},
            )
        self._tool_calls += count

    def check(self, *, pending_tool_calls: int = 0) -> LimitBreach | None:
        """判定是否已经越界；未越界返回 `None`。

        `pending_tool_calls` 是**即将**发起的这一批调用数：先记账再判定会让最后一批工具
        真的执行完才发现超了，副作用已经发生，上限也就失去了意义。

        两类计数的比较符号不同，是因为「还要不要再做一次」的信息来源不同：迭代数用
        `>=`（调用方在每次迭代**前**调用，等于恒定 `pending=1`），工具调用数用
        `projected > limit`（这一批到底几个由调用方传入）。因此「配额刚好用满且模型没再
        要工具」不算越界——那一轮本就该正常收尾，为它判 `STOPPED_BY_LIMIT` 只会把一次
        成功的对话记成撞墙。

        判定顺序是总超时 -> 迭代 -> 工具调用：时间用尽时无论还剩多少配额都该停下，
        而且它对应的终态（`CANCELLED`）与另两项不同，先判才不会被计数类越界遮住。
        """
        limits = self._limits
        elapsed = self.elapsed_ms
        if elapsed >= limits.turn_timeout_ms:
            return LimitBreach(LimitKind.TURN_TIMEOUT_MS, limits.turn_timeout_ms, elapsed)
        if self._iterations >= limits.max_iterations:
            return LimitBreach(LimitKind.MAX_ITERATIONS, limits.max_iterations, self._iterations)
        projected = self._tool_calls + pending_tool_calls
        if projected > limits.max_tool_calls_per_turn:
            return LimitBreach(
                LimitKind.MAX_TOOL_CALLS_PER_TURN, limits.max_tool_calls_per_turn, projected
            )
        return None

    def snapshot(self) -> dict[str, int]:
        """记账快照，供 `TurnOutcome` 与 turn 事件使用（`OBS-002`）。"""
        return {
            "iterations": self._iterations,
            "tool_calls": self._tool_calls,
            "elapsed_ms": self.elapsed_ms,
        }

    def __repr__(self) -> str:
        return (
            f"BudgetLedger(iterations={self._iterations}/{self._limits.max_iterations}, "
            f"tool_calls={self._tool_calls}/{self._limits.max_tool_calls_per_turn})"
        )
