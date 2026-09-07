"""六项预算与账本的测试。

| 验收项 | 测试 |
| --- | --- |
| 6 项预算逐项：默认值存在性 | `test_default_values_are_the_documented_six` |
| 6 项预算逐项：达到上限的行为 | `test_*_breach_*` 五节 + `LIMIT_OUTCOMES` 对照表 |
| 6 项预算逐项：可配置性 | `test_every_limit_is_configurable` |
| 缺省配置下不存在无界执行路径 | `test_default_limits_terminate_a_runaway_model` |

默认值与 `LIMIT_OUTCOMES` 以**字面量**断言，防止开箱行为和终态映射被意外改动。

`test_default_limits_terminate_a_runaway_model` 里的循环是  engine 主循环的**骨架**
（check -> begin_iteration -> 模型 -> 记工具调用），engine 尚未存在，因此这里证明的是
「按这个骨架用账本，缺省配置必然有限步终止」。 落地后由 `tests/kernel/test_engine.py`
在真正的引擎上重跑同一条性质。
"""

from __future__ import annotations

from dataclasses import fields

import pytest

from karyvia.contracts import (
    CancelReason,
    ErrorCategory,
    ErrorCode,
    KaryviaError,
    ModelInfo,
    TurnStatus,
)
from karyvia.kernel.turn import (
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_MAX_TOOL_CALLS_PER_TURN,
    DEFAULT_TOOL_RESULT_MAX_BYTES,
    DEFAULT_TOOL_TIMEOUT_MS,
    DEFAULT_TURN_TIMEOUT_MS,
    FALLBACK_CONTEXT_MAX_TOKENS,
    LIMIT_OUTCOMES,
    BudgetLedger,
    LimitBreach,
    LimitKind,
    TurnLimits,
)


class FakeClock:
    """手动推进的单调时钟。turn 总超时默认 900 秒，测试不可能真的等。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance_ms(self, milliseconds: int) -> None:
        self.now += milliseconds / 1000


# --------------------------------------------------------------------------------------
# 六项的形状、默认值与终态语义
# --------------------------------------------------------------------------------------


def test_turn_limits_has_exactly_the_six_budget_fields() -> None:
    assert [field.name for field in fields(TurnLimits)] == [
        "max_iterations",
        "max_tool_calls_per_turn",
        "tool_timeout_ms",
        "tool_result_max_bytes",
        "turn_timeout_ms",
        "context_max_tokens",
    ]


def test_limit_kind_values_equal_field_names() -> None:
    """越界报告要能直接指回「改哪个配置项」，中间不许有翻译表。"""
    assert {kind.value for kind in LimitKind} == {field.name for field in fields(TurnLimits)}


def test_default_values_are_the_documented_six() -> None:
    limits = TurnLimits()
    assert (limits.max_iterations, DEFAULT_MAX_ITERATIONS) == (16, 16)
    assert (limits.max_tool_calls_per_turn, DEFAULT_MAX_TOOL_CALLS_PER_TURN) == (48, 48)
    assert (limits.tool_timeout_ms, DEFAULT_TOOL_TIMEOUT_MS) == (120_000, 120_000)
    assert (limits.tool_result_max_bytes, DEFAULT_TOOL_RESULT_MAX_BYTES) == (65_536, 65_536)
    assert (limits.turn_timeout_ms, DEFAULT_TURN_TIMEOUT_MS) == (900_000, 900_000)
    # 唯一允许缺省为 None 的一项：含义是「由模型能力推导」而不是「无限制」。
    assert limits.context_max_tokens is None
    assert limits.resolve_context_max_tokens() == FALLBACK_CONTEXT_MAX_TOKENS


def test_limit_outcomes_table() -> None:
    assert dict(LIMIT_OUTCOMES) == {
        LimitKind.MAX_ITERATIONS: (TurnStatus.STOPPED_BY_LIMIT, None),
        LimitKind.MAX_TOOL_CALLS_PER_TURN: (TurnStatus.STOPPED_BY_LIMIT, None),
        LimitKind.TOOL_TIMEOUT_MS: (None, None),
        LimitKind.TOOL_RESULT_MAX_BYTES: (None, None),
        LimitKind.TURN_TIMEOUT_MS: (TurnStatus.CANCELLED, CancelReason.TIMEOUT),
        LimitKind.CONTEXT_MAX_TOKENS: (None, None),
    }
    assert set(LIMIT_OUTCOMES) == set(LimitKind)


def test_every_limit_is_configurable() -> None:
    limits = TurnLimits(
        max_iterations=3,
        max_tool_calls_per_turn=5,
        tool_timeout_ms=1_000,
        tool_result_max_bytes=64,
        turn_timeout_ms=2_000,
        context_max_tokens=1_234,
    )
    assert [limits.limit_for(kind) for kind in LimitKind] == [3, 5, 1_000, 64, 2_000, 1_234]


@pytest.mark.parametrize("field_name", [field.name for field in fields(TurnLimits)])
@pytest.mark.parametrize("bad", [0, -1])
def test_non_positive_limits_are_rejected(field_name: str, bad: int) -> None:
    """0 不是「不限制」而是「立刻越界」，静默接受它等于把 turn 变成不可用。"""
    with pytest.raises(KaryviaError) as excinfo:
        TurnLimits(**{field_name: bad})
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID
    assert excinfo.value.category is ErrorCategory.CONFIG
    assert excinfo.value.detail["field"] == field_name


def test_bool_is_not_an_acceptable_limit() -> None:
    """`True` 是 `int` 的子类，放行它会让 `max_iterations=True` 变成「只跑一轮」。"""
    with pytest.raises(KaryviaError) as excinfo:
        TurnLimits(max_iterations=True)  # pyright: ignore[reportArgumentType]
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID


# --------------------------------------------------------------------------------------
# 逐项：max_iterations
# --------------------------------------------------------------------------------------


def test_max_iterations_breach_stops_by_limit() -> None:
    ledger = BudgetLedger(TurnLimits(max_iterations=2))
    assert ledger.check() is None
    ledger.begin_iteration()
    assert ledger.check() is None
    ledger.begin_iteration()

    breach = ledger.check()
    assert breach is not None
    assert breach.kind is LimitKind.MAX_ITERATIONS
    assert (breach.limit, breach.observed) == (2, 2)
    assert breach.terminal_status is TurnStatus.STOPPED_BY_LIMIT
    assert breach.cancel_reason is None


# --------------------------------------------------------------------------------------
# 逐项：max_tool_calls_per_turn
# --------------------------------------------------------------------------------------


def test_max_tool_calls_breach_stops_by_limit() -> None:
    ledger = BudgetLedger(TurnLimits(max_tool_calls_per_turn=3))
    ledger.record_tool_calls(3)
    # 配额用满本身不是越界：模型这一轮没再要工具时，turn 应当正常收尾。
    assert ledger.check() is None

    breach = ledger.check(pending_tool_calls=1)
    assert breach is not None
    assert breach.kind is LimitKind.MAX_TOOL_CALLS_PER_TURN
    assert (breach.limit, breach.observed) == (3, 4)
    assert breach.terminal_status is TurnStatus.STOPPED_BY_LIMIT
    assert breach.cancel_reason is None


def test_pending_batch_is_counted_before_execution() -> None:
    """先记账再判定会让最后一批工具真的执行完才发现超了——副作用已经发生，上限就白设了。"""
    ledger = BudgetLedger(TurnLimits(max_tool_calls_per_turn=3))
    ledger.record_tool_calls(2)
    assert ledger.check() is None
    breach = ledger.check(pending_tool_calls=2)
    assert breach is not None
    assert (breach.limit, breach.observed) == (3, 4)
    assert ledger.tool_calls == 2  # 判定不记账。


def test_negative_tool_call_count_is_rejected() -> None:
    with pytest.raises(KaryviaError) as excinfo:
        BudgetLedger().record_tool_calls(-1)
    assert excinfo.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


# --------------------------------------------------------------------------------------
# 逐项：turn_timeout_ms
# --------------------------------------------------------------------------------------


def test_turn_timeout_breach_cancels_with_timeout_reason() -> None:
    clock = FakeClock()
    ledger = BudgetLedger(TurnLimits(turn_timeout_ms=5_000), clock=clock)
    clock.advance_ms(4_999)
    assert ledger.check() is None
    clock.advance_ms(1)

    breach = ledger.check()
    assert breach is not None
    assert breach.kind is LimitKind.TURN_TIMEOUT_MS
    assert breach.terminal_status is TurnStatus.CANCELLED
    assert breach.cancel_reason is CancelReason.TIMEOUT


def test_remaining_ms_never_goes_negative() -> None:
    clock = FakeClock()
    ledger = BudgetLedger(TurnLimits(turn_timeout_ms=1_000), clock=clock)
    assert ledger.remaining_ms() == 1_000
    clock.advance_ms(600)
    assert ledger.remaining_ms() == 400
    clock.advance_ms(10_000)
    assert ledger.remaining_ms() == 0


def test_turn_timeout_is_checked_before_counters() -> None:
    """时间用尽时终态是 `CANCELLED`，被计数类越界遮住就会写成 `STOPPED_BY_LIMIT`。"""
    clock = FakeClock()
    ledger = BudgetLedger(TurnLimits(max_iterations=1, turn_timeout_ms=1_000), clock=clock)
    ledger.begin_iteration()
    clock.advance_ms(1_000)
    breach = ledger.check()
    assert breach is not None
    assert breach.kind is LimitKind.TURN_TIMEOUT_MS


# --------------------------------------------------------------------------------------
# 逐项：tool_result_max_bytes
# --------------------------------------------------------------------------------------


def test_short_result_is_not_truncated() -> None:
    content, truncated = TurnLimits().truncate_tool_result("ok")
    assert (content, truncated) == ("ok", False)


def test_long_result_is_truncated_and_flagged() -> None:
    limits = TurnLimits(tool_result_max_bytes=10)
    content, truncated = limits.truncate_tool_result("x" * 50)
    assert truncated is True
    assert content == "x" * 10


def test_truncation_counts_bytes_and_keeps_characters_whole() -> None:
    """上限约束的是进上下文的实际体积，而一个中文字符是三个字节。"""
    limits = TurnLimits(tool_result_max_bytes=8)
    content, truncated = limits.truncate_tool_result("中文内容很长")
    assert truncated is True
    assert content == "中文"  # 6 字节；第 3 个字符会跨过 8 字节边界，不产生半个字符。
    assert len(content.encode("utf-8")) <= 8


def test_truncation_is_exact_at_the_boundary() -> None:
    limits = TurnLimits(tool_result_max_bytes=6)
    assert limits.truncate_tool_result("中文") == ("中文", False)


def test_tool_result_breach_does_not_end_the_turn() -> None:
    breach = LimitBreach(LimitKind.TOOL_RESULT_MAX_BYTES, 100, 250)
    assert breach.terminal_status is None
    assert breach.cancel_reason is None


# --------------------------------------------------------------------------------------
# 逐项：tool_timeout_ms
# --------------------------------------------------------------------------------------


def test_tool_timeout_breach_does_not_end_the_turn() -> None:
    """单工具超时只让那一次调用失败，turn 继续。"""
    breach = LimitBreach(LimitKind.TOOL_TIMEOUT_MS, 120_000, 130_000)
    assert breach.terminal_status is None


# --------------------------------------------------------------------------------------
# 逐项：context_max_tokens
# --------------------------------------------------------------------------------------


def test_explicit_context_budget_wins() -> None:
    limits = TurnLimits(context_max_tokens=1_000)
    model = ModelInfo("m", "p", context_window_tokens=200_000, max_output_tokens=8_000)
    assert limits.resolve_context_max_tokens(model) == 1_000


def test_explicit_context_budget_cannot_exceed_the_model_input_window() -> None:
    limits = TurnLimits(context_max_tokens=10_000)
    model = ModelInfo("m", "p", context_window_tokens=4_096, max_output_tokens=1_024)
    assert limits.resolve_context_max_tokens(model) == 3_072


def test_context_budget_has_one_shared_trigger_and_target() -> None:
    budget = TurnLimits(context_max_tokens=1_000).resolve_context_budget()
    assert budget.hard_limit == 1_000
    assert budget.trigger_limit == 950
    assert budget.target_limit == 760


def test_context_budget_derived_from_model_window() -> None:
    model = ModelInfo("m", "p", context_window_tokens=200_000, max_output_tokens=8_000)
    assert TurnLimits().resolve_context_max_tokens(model) == 192_000


def test_context_budget_falls_back_when_model_is_silent() -> None:
    model = ModelInfo("m", "p")
    assert TurnLimits().resolve_context_max_tokens(model) == FALLBACK_CONTEXT_MAX_TOKENS


def test_contradictory_model_declaration_raises() -> None:
    """不得静默降级。整个窗口都留给输出，模型就没有地方读输入了。"""
    model = ModelInfo("m", "p", context_window_tokens=4_096, max_output_tokens=4_096)
    with pytest.raises(KaryviaError) as excinfo:
        TurnLimits().resolve_context_max_tokens(model)
    assert excinfo.value.code is ErrorCode.CONFIG_INVALID


def test_context_breach_does_not_end_the_turn() -> None:
    breach = LimitBreach(LimitKind.CONTEXT_MAX_TOKENS, 8_192, 12_000)
    assert breach.terminal_status is None


# --------------------------------------------------------------------------------------
# 越界记录与账本快照
# --------------------------------------------------------------------------------------


def test_breach_describes_itself_without_internals() -> None:
    breach = LimitBreach(LimitKind.MAX_ITERATIONS, 16, 16)
    text = breach.describe()
    assert "max_iterations" in text
    assert "16" in text
    assert breach.to_detail() == {"limit_kind": "max_iterations", "limit": 16, "observed": 16}


def test_ledger_snapshot_and_repr() -> None:
    clock = FakeClock()
    ledger = BudgetLedger(clock=clock)
    ledger.begin_iteration()
    ledger.record_tool_calls(2)
    clock.advance_ms(250)
    assert ledger.snapshot() == {"iterations": 1, "tool_calls": 2, "elapsed_ms": 250}
    assert repr(ledger) == "BudgetLedger(iterations=1/16, tool_calls=2/48)"


def test_ledger_defaults_to_default_limits() -> None:
    assert BudgetLedger().limits == TurnLimits()


def test_two_ledgers_do_not_share_counters() -> None:
    """并发 turn 共享同一份计数是把计数器塞进配置对象的必然后果。"""
    limits = TurnLimits()
    first, second = BudgetLedger(limits), BudgetLedger(limits)
    first.begin_iteration()
    assert (first.iterations, second.iterations) == (1, 0)


# --------------------------------------------------------------------------------------
# 缺省配置下不存在无界执行路径
# --------------------------------------------------------------------------------------


def test_default_limits_terminate_a_runaway_model() -> None:
    """永远返回 tool_call 的模型，必须在有限步内以 `STOPPED_BY_LIMIT` 终止。

    循环体是  engine 主循环的骨架；此处证明的是「按这个骨架用账本，缺省配置必然
    有限步终止」， 落地后在真正的引擎上重跑同一条性质。
    """
    ledger = BudgetLedger()  # 全部缺省。
    status: TurnStatus | None = None
    breach: LimitBreach | None = None
    guard = 0

    while status is None:
        guard += 1
        assert guard <= 1_000, "缺省配置下出现了无界执行路径"
        breach = ledger.check(pending_tool_calls=1)
        if breach is not None:
            status = breach.terminal_status
            break
        ledger.begin_iteration()
        # 假模型：永远回一个 tool_call，永远不给终止信号。
        ledger.record_tool_calls(1)

    assert status is TurnStatus.STOPPED_BY_LIMIT
    assert breach is not None
    assert breach.kind is LimitKind.MAX_ITERATIONS
    assert ledger.iterations == DEFAULT_MAX_ITERATIONS


def test_runaway_model_with_wide_batches_stops_on_tool_calls() -> None:
    """每轮多发工具时先撞上的是工具预算，终态同样是 `STOPPED_BY_LIMIT`。"""
    ledger = BudgetLedger(TurnLimits(max_iterations=100, max_tool_calls_per_turn=10))
    breach: LimitBreach | None = None
    while breach is None:
        breach = ledger.check(pending_tool_calls=4)
        if breach is not None:
            break
        ledger.begin_iteration()
        ledger.record_tool_calls(4)

    assert breach.kind is LimitKind.MAX_TOOL_CALLS_PER_TURN
    assert breach.terminal_status is TurnStatus.STOPPED_BY_LIMIT
    assert ledger.tool_calls == 8  # 第三批 4 次会越界，因此没有发出去。
