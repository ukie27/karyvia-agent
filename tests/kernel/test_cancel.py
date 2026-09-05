"""取消令牌与 6 个命名检查点的测试（`D08` 验收：幂等、父子传播、取消后数据可保存）。

四条主线：

| 验收项 | 测试 |
| --- | --- |
| `request()` 幂等（`EDG-206`） | `test_request_is_idempotent*` |
| `child()` 父取消传播到子 | `test_child_*` |
| 取消后已产生内容仍可保存（`KER-007`） | `test_partial_output_survives_cancellation` |
| 6 个检查点各自可测 | `test_checkpoint_reports_where_it_stopped`、`test_checkpoint_owner_table` |

对照表（`CHECKPOINT_OWNERS`、`CANCEL_REASON_CODES`）以**字面量**写在测试里再与实现比对：
从实现反推的表格只能证明代码没改，证明不了它和技术方案 §6.4 一致。
"""

from __future__ import annotations

import asyncio
import gc

import pytest

from karyvia.contracts import (
    CancelReason,
    CancelSignal,
    ErrorCategory,
    ErrorCode,
    KaryviaError,
)
from karyvia.kernel.turn import (
    CANCEL_REASON_CODES,
    CHECKPOINT_OWNERS,
    DEFAULT_TOOL_CANCEL_GRACE_MS,
    CancelToken,
    Checkpoint,
    CheckpointOwner,
)

# --------------------------------------------------------------------------------------
# 基本状态
# --------------------------------------------------------------------------------------


def test_fresh_token_is_not_requested() -> None:
    token = CancelToken()
    assert token.requested is False
    assert token.reason is None
    token.raise_if_requested()
    token.checkpoint(Checkpoint.BEFORE_MODEL_REQUEST)


def test_request_sets_reason_and_flag() -> None:
    token = CancelToken()
    token.request(CancelReason.USER)
    assert token.requested is True
    assert token.reason is CancelReason.USER


def test_request_defaults_to_user_reason() -> None:
    """CLI 的 Ctrl-C 是最常见的调用点，默认值省掉的是它每次都要重复的那个参数。"""
    token = CancelToken()
    token.request()
    assert token.reason is CancelReason.USER


def test_repr_shows_state() -> None:
    token = CancelToken()
    assert repr(token) == "CancelToken(pending)"
    token.request(CancelReason.SHUTDOWN)
    assert repr(token) == "CancelToken(shutdown)"


def test_token_satisfies_cancel_signal_protocol() -> None:
    """能力实现拿到的是同一个对象，但只看得见 `CancelSignal` 这个只读面。"""
    assert isinstance(CancelToken(), CancelSignal)


# --------------------------------------------------------------------------------------
# 幂等（EDG-206）
# --------------------------------------------------------------------------------------


def test_request_is_idempotent_keeps_first_reason() -> None:
    token = CancelToken()
    token.request(CancelReason.USER)
    token.request(CancelReason.SHUTDOWN)
    token.request(CancelReason.BUDGET)
    assert token.reason is CancelReason.USER


def test_request_is_idempotent_for_children() -> None:
    """重复中断不得把已经取消的子令牌改写成另一个原因，否则工具侧的诊断会前后矛盾。"""
    parent = CancelToken()
    child = parent.child()
    parent.request(CancelReason.USER)
    parent.request(CancelReason.TIMEOUT)
    assert child.reason is CancelReason.USER


def test_repeated_request_does_not_disturb_waiters() -> None:
    """幂等的可观测形态：重复请求不产生第二次唤醒，也不改变已解析的结果。"""

    async def scenario() -> CancelReason:
        token = CancelToken()
        waiter = asyncio.ensure_future(token.wait())
        await asyncio.sleep(0)
        token.request(CancelReason.USER)
        token.request(CancelReason.BUDGET)
        return await waiter

    assert asyncio.run(scenario()) is CancelReason.USER


# --------------------------------------------------------------------------------------
# 抛出的错误
# --------------------------------------------------------------------------------------


def test_cancel_reason_codes_table() -> None:
    assert dict(CANCEL_REASON_CODES) == {
        CancelReason.USER: ErrorCode.CANCELLED_BY_USER,
        CancelReason.TIMEOUT: ErrorCode.CANCELLED_BY_BUDGET,
        CancelReason.BUDGET: ErrorCode.CANCELLED_BY_BUDGET,
        CancelReason.SHUTDOWN: ErrorCode.CANCELLED_BY_SHUTDOWN,
    }
    assert set(CANCEL_REASON_CODES) == set(CancelReason)


@pytest.mark.parametrize("reason", list(CancelReason))
def test_raise_if_requested_uses_cancelled_category(reason: CancelReason) -> None:
    token = CancelToken()
    token.request(reason)
    with pytest.raises(KaryviaError) as excinfo:
        token.raise_if_requested()
    error = excinfo.value
    assert error.code is CANCEL_REASON_CODES[reason]
    assert error.category is ErrorCategory.CANCELLED
    assert error.detail["reason"] == reason.value
    assert "checkpoint" not in error.detail


def test_shutdown_is_not_retryable() -> None:
    """进程正在退出，重试无处可去；用户中断则允许再来一次。"""
    shutdown = CancelToken()
    shutdown.request(CancelReason.SHUTDOWN)
    user = CancelToken()
    user.request(CancelReason.USER)
    with pytest.raises(KaryviaError) as shutdown_err:
        shutdown.raise_if_requested()
    with pytest.raises(KaryviaError) as user_err:
        user.raise_if_requested()
    assert shutdown_err.value.retryable is False
    assert user_err.value.retryable is True


# --------------------------------------------------------------------------------------
# 检查点
# --------------------------------------------------------------------------------------


def test_checkpoint_owner_table() -> None:
    assert dict(CHECKPOINT_OWNERS) == {
        Checkpoint.BEFORE_CONTEXT: CheckpointOwner.ORCHESTRATOR,
        Checkpoint.BEFORE_MODEL_REQUEST: CheckpointOwner.ENGINE,
        Checkpoint.BETWEEN_STREAM_CHUNKS: CheckpointOwner.ENGINE,
        Checkpoint.AFTER_MODEL_RESPONSE: CheckpointOwner.ORCHESTRATOR,
        Checkpoint.BEFORE_TOOL_CALL: CheckpointOwner.ENGINE,
        Checkpoint.AFTER_TOOL_RESULT: CheckpointOwner.ENGINE,
    }


def test_checkpoints_are_exactly_six() -> None:
    """技术方案 §6.4 的表格是 6 行。加一个检查点就是加一种善后语义，必须走评审。"""
    assert len(Checkpoint) == 6
    assert set(CHECKPOINT_OWNERS) == set(Checkpoint)


def test_engine_owns_four_checkpoints() -> None:
    """`D09` 的 engine 实现 2/3/5/6，1/4 在 `D14` 的 orchestrator 边界上。"""
    engine = {cp for cp, owner in CHECKPOINT_OWNERS.items() if owner is CheckpointOwner.ENGINE}
    assert engine == {
        Checkpoint.BEFORE_MODEL_REQUEST,
        Checkpoint.BETWEEN_STREAM_CHUNKS,
        Checkpoint.BEFORE_TOOL_CALL,
        Checkpoint.AFTER_TOOL_RESULT,
    }


@pytest.mark.parametrize("where", list(Checkpoint))
def test_checkpoint_reports_where_it_stopped(where: Checkpoint) -> None:
    token = CancelToken()
    token.request(CancelReason.USER)
    with pytest.raises(KaryviaError) as excinfo:
        token.checkpoint(where)
    detail = excinfo.value.detail
    assert detail["checkpoint"] == where.value
    assert detail["owner"] == CHECKPOINT_OWNERS[where].value


@pytest.mark.parametrize("where", list(Checkpoint))
def test_checkpoint_passes_when_not_cancelled(where: Checkpoint) -> None:
    CancelToken().checkpoint(where)


# --------------------------------------------------------------------------------------
# 父子传播
# --------------------------------------------------------------------------------------


def test_child_is_cancelled_by_parent() -> None:
    parent = CancelToken()
    child = parent.child()
    assert child.requested is False
    parent.request(CancelReason.TIMEOUT)
    assert child.requested is True
    assert child.reason is CancelReason.TIMEOUT


def test_cancellation_reaches_grandchildren() -> None:
    root = CancelToken()
    child = root.child()
    grandchild = child.child()
    root.request(CancelReason.SHUTDOWN)
    assert grandchild.reason is CancelReason.SHUTDOWN


def test_child_created_after_cancellation_is_born_cancelled() -> None:
    """否则「取消后又发起一次工具调用」会拿到一个看起来正常的令牌。"""
    parent = CancelToken()
    parent.request(CancelReason.BUDGET)
    child = parent.child()
    assert child.requested is True
    assert child.reason is CancelReason.BUDGET


def test_child_cancellation_does_not_reach_parent_or_sibling() -> None:
    """工具超时取消一个子 turn，不该顺手停掉外层对话。"""
    parent = CancelToken()
    first = parent.child()
    second = parent.child()
    first.request(CancelReason.TIMEOUT)
    assert parent.requested is False
    assert second.requested is False


def test_collected_child_does_not_break_parent_cancellation() -> None:
    """子令牌用弱引用登记：工具结束后没人再持有它，不该把它一直挂在 turn 上。"""
    parent = CancelToken()
    parent.child()
    gc.collect()
    parent.request(CancelReason.USER)
    assert parent.requested is True


# --------------------------------------------------------------------------------------
# 可等待
# --------------------------------------------------------------------------------------


def test_wait_returns_immediately_when_already_cancelled() -> None:
    async def scenario() -> CancelReason:
        token = CancelToken()
        token.request(CancelReason.BUDGET)
        return await asyncio.wait_for(token.wait(), timeout=1)

    assert asyncio.run(scenario()) is CancelReason.BUDGET


def test_wait_wakes_up_on_parent_cancellation() -> None:
    """`EDG-407` 的宽限期要与取消赛跑，只有轮询的话 Kernel 只能指望工具自觉。"""

    async def scenario() -> CancelReason:
        parent = CancelToken()
        child = parent.child()
        waiter = asyncio.ensure_future(child.wait())
        await asyncio.sleep(0)
        assert not waiter.done()
        parent.request(CancelReason.USER)
        return await asyncio.wait_for(waiter, timeout=1)

    assert asyncio.run(scenario()) is CancelReason.USER


def test_grace_default_is_two_seconds() -> None:
    """技术方案 §15 决策表第 7 行。它是取消参数而不是预算项，不在 `TurnLimits` 六项里。"""
    assert DEFAULT_TOOL_CANCEL_GRACE_MS == 2000


# --------------------------------------------------------------------------------------
# 取消后数据仍可保存（KER-007，本模块存在的理由）
# --------------------------------------------------------------------------------------


def test_partial_output_survives_cancellation() -> None:
    """显式令牌而非 `CancelledError` 的核心收益：退出点由我们选，善后代码一定跑得到。

    模拟检查点 3（流式分片之间）：中断发生时已产生的文本必须完整留在手里，
    并可标记 `interrupted=True` 落库（`EDG-304`）。
    """
    token = CancelToken()
    chunks = ["第一段", "第二段", "第三段", "第四段"]
    produced: list[str] = []
    interrupted = False

    try:
        for index, chunk in enumerate(chunks):
            token.checkpoint(Checkpoint.BETWEEN_STREAM_CHUNKS)
            produced.append(chunk)
            if index == 1:  # 第二段之后用户按下 Ctrl-C。
                token.request(CancelReason.USER)
    except KaryviaError as error:
        interrupted = True
        assert error.code is ErrorCode.CANCELLED_BY_USER

    assert interrupted is True
    assert "".join(produced) == "第一段第二段"


def test_executed_tool_results_are_kept_on_cancellation() -> None:
    """检查点 5/6 的语义：已执行的工具保留真实结果，未执行的不留痕迹。"""
    token = CancelToken()
    calls = ["a", "b", "c"]
    executed: list[str] = []
    skipped: list[str] = []

    for call in calls:
        if token.requested:
            skipped.append(call)
            continue
        executed.append(call)
        if call == "a":
            token.request(CancelReason.USER)

    assert executed == ["a"]
    assert skipped == ["b", "c"]
