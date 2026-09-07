"""`kernel/turn/retry.py`当前行为：重试判定与 `RetryingModel` 的两条路径。

| 验收项 | 测试 |
| --- | --- |
| 判定纯函数：谁能重试、等多久 | `TestRetryDecision` |
| 策略对象自身的校验 | `TestRetryPolicy` |
| 非流式：重发、用尽、事件 | `TestComplete` |
| 流式：只在放行之前可以重来 | `TestStream` |
| 空回复：判据、用尽、开关 | `TestEmptyResponse` |
| 退避期间的取消 | `TestCancellation` |

**退避是确定的**：每个用例都注入一个常量 `jitter`（或用 `retry_after_ms` 那条不加抖动的
路径），因此没有一条用例依赖 `random`。延迟本身取 1–2 ms，用例不真的睡半秒。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import pytest

from karyvia.contracts import (
    CancelReason,
    ChunkKind,
    ErrorCode,
    EventName,
    KaryviaError,
    ModelChunk,
    ModelRequest,
    ModelResponse,
    OpaqueBlock,
    StopReason,
)
from karyvia.kernel.observability import EventBus
from karyvia.kernel.turn import (
    MAX_HELD_CHUNKS,
    BudgetLedger,
    CancelToken,
    RetryingModel,
    RetryPolicy,
    TurnLimits,
    is_empty_answer,
    retry_delay_ms,
)

from ._engine_support import (
    ScriptedProvider,
    chunks_for,
    make_request,
    text_response,
    tool_call,
    tool_response,
)
from ._orchestrator_support import INSTANCE, EventCollector

# 每次重试等 1 ms：判定与事件是本文件要验的东西，真实退避时长不是。
FAST = RetryPolicy(max_attempts=3, base_delay_ms=1, max_delay_ms=1)
STEADY = 1.0  # 常量 jitter：系数恒为 (0.5 + 0.5*1.0) = 1.0，延迟因此等于计算出的上界。


def flaky(message: str = "上游抖了一下", **detail: int) -> KaryviaError:
    """一个如实标了 `retryable=True` 的外部故障，形状同 `model_openai/faults.py`。"""
    return KaryviaError(
        ErrorCode.EXTERNAL_MODEL_PROVIDER, message, detail=dict(detail), retryable=True
    )


def fatal(message: str = "参数不对") -> KaryviaError:
    return KaryviaError(ErrorCode.INPUT_MALFORMED, message)


def empty_stream(stop: StopReason = StopReason.END_TURN) -> list[ModelChunk]:
    """一条什么都没说的流：只有一个 DONE。"""
    return [ModelChunk(kind=ChunkKind.DONE, stop_reason=stop)]


class Wrapped:
    """一个包好的 `RetryingModel` 与它周围的东西，供用例直接读。"""

    def __init__(
        self,
        script: Sequence[object],
        *,
        policy: RetryPolicy = FAST,
        limits: TurnLimits | None = None,
        tool_calls: int = 0,
    ) -> None:
        self.bus = EventBus(INSTANCE)
        self.events = EventCollector(self.bus)
        self.inner = ScriptedProvider(script)  # type: ignore[arg-type]  # boundary: 脚本条目是联合类型
        self.ledger = BudgetLedger(limits or TurnLimits())
        if tool_calls:
            self.ledger.record_tool_calls(tool_calls)
        self.model = RetryingModel(
            self.inner,  # type: ignore[arg-type]  # boundary: 结构化满足 ModelProvider
            policy,
            self.bus,
            ledger=self.ledger,
            jitter=lambda: STEADY,
        )

    @property
    def failures(self) -> list[dict[str, object]]:
        """全部 `model.request_failed` 的载荷，按发生顺序。"""
        return [dict(event.payload) for event in self.events.of(EventName.MODEL_REQUEST_FAILED)]

    async def drain(self, request: ModelRequest, cancel: CancelToken) -> list[ModelChunk]:
        return [chunk async for chunk in self.model.stream(request, cancel)]


# --------------------------------------------------------------------------------- 判定


class TestRetryDecision:
    def test_a_cancellation_is_never_retried_even_though_it_says_retryable(self) -> None:
        """**这条是本文件最重要的一条。**

        `cancel.py::_to_error` 写的是 `retryable=reason is not CancelReason.SHUTDOWN`，
        也就是说用户按 Ctrl-C 产生的那个错误**自称可重试**。只看 `retryable` 会去重发一个
        已经被取消的请求，因此判据必须是 `ErrorCategory`（同一条）。
        """
        token = CancelToken()
        token.request(CancelReason.USER)
        try:
            token.raise_if_requested()
        except KaryviaError as cancelled:
            assert cancelled.retryable is True, "前提变了：取消错误不再自称可重试"
            assert retry_delay_ms(cancelled, 1, FAST, jitter=lambda: STEADY) is None

    def test_a_non_retryable_error_is_not_retried(self) -> None:
        assert retry_delay_ms(fatal(), 1, FAST, jitter=lambda: STEADY) is None

    def test_the_last_attempt_does_not_ask_for_another(self) -> None:
        policy = RetryPolicy(max_attempts=2, base_delay_ms=1, max_delay_ms=1)
        assert retry_delay_ms(flaky(), 1, policy, jitter=lambda: STEADY) is not None
        assert retry_delay_ms(flaky(), 2, policy, jitter=lambda: STEADY) is None

    def test_max_attempts_of_one_means_no_retry_at_all(self) -> None:
        """`1` 是「只试一次」，因此不需要第二个 `enabled` 开关。"""
        policy = RetryPolicy(max_attempts=1)
        assert retry_delay_ms(flaky(), 1, policy, jitter=lambda: STEADY) is None

    def test_the_backoff_doubles_and_stops_at_the_ceiling(self) -> None:
        policy = RetryPolicy(max_attempts=9, base_delay_ms=100, max_delay_ms=250)
        delays = [retry_delay_ms(flaky(), n, policy, jitter=lambda: STEADY) for n in (1, 2, 3, 4)]
        assert delays == [100, 200, 250, 250]

    def test_jitter_only_shortens_never_lengthens(self) -> None:
        """系数落在 [0.5, 1.0]：抖动是为了错开一起撞墙的 conversation，不是加时。"""
        policy = RetryPolicy(max_attempts=2, base_delay_ms=100, max_delay_ms=100)
        assert retry_delay_ms(flaky(), 1, policy, jitter=lambda: 0.0) == 50
        assert retry_delay_ms(flaky(), 1, policy, jitter=lambda: 1.0) == 100

    def test_the_server_hint_wins_and_is_not_jittered(self) -> None:
        """供应商发的 `Retry-After` 原样用。抖动只会把它往小了调，那意味着再吃一次 429。"""
        policy = RetryPolicy(max_attempts=2, base_delay_ms=100, max_delay_ms=100)
        hinted = flaky(retry_after_ms=7_000)
        assert retry_delay_ms(hinted, 1, policy, jitter=lambda: 0.0) == 7_000

    def test_a_malformed_hint_falls_back_to_the_computed_backoff(self) -> None:
        """`detail` 是自由载荷，形状不对时不能让整条判定崩掉。"""
        policy = RetryPolicy(max_attempts=2, base_delay_ms=100, max_delay_ms=100)
        broken = KaryviaError(
            ErrorCode.EXTERNAL_MODEL_PROVIDER, "限流", detail={"retry_after_ms": "soon"}, retryable=True
        )
        assert retry_delay_ms(broken, 1, policy, jitter=lambda: STEADY) == 100


class TestRetryPolicy:
    def test_the_three_numbers_must_be_positive(self) -> None:
        for field in ("max_attempts", "base_delay_ms", "max_delay_ms"):
            with pytest.raises(KaryviaError) as excinfo:
                RetryPolicy(**{field: 0})  # type: ignore[arg-type]  # boundary: 故意的坏值
            assert excinfo.value.code is ErrorCode.CONFIG_INVALID
            assert excinfo.value.detail["field"] == field

    def test_a_bool_is_not_an_int_here(self) -> None:
        """`True == 1` 在 Python 里成立，而「重试 True 次」不是一个配置。"""
        with pytest.raises(KaryviaError):
            RetryPolicy(max_attempts=True)  # type: ignore[arg-type]  # boundary: 故意的坏值


class TestEmptyAnswer:
    def test_content_or_tool_calls_mean_it_is_not_empty(self) -> None:
        assert is_empty_answer(StopReason.END_TURN, content="答") is False
        assert is_empty_answer(StopReason.END_TURN, tool_calls=1) is False

    def test_whitespace_only_is_still_empty(self) -> None:
        assert is_empty_answer(StopReason.END_TURN, content="  \n ") is True

    def test_a_missing_done_chunk_counts_as_empty(self) -> None:
        assert is_empty_answer(None) is True

    def test_a_deliberate_refusal_is_not_an_empty_answer(self) -> None:
        """内容过滤与撞上 `max_tokens` 都是模型的决定，重发只会再来一次。"""
        assert is_empty_answer(StopReason.CONTENT_FILTER) is False
        assert is_empty_answer(StopReason.MAX_TOKENS) is False
        assert is_empty_answer(StopReason.STOP_SEQUENCE) is False


# --------------------------------------------------------------------------------- 非流式


class TestComplete:
    async def test_a_transient_failure_is_retried_and_then_succeeds(self) -> None:
        """ 之前这条 turn 直接 `FAILED`，用户要自己重发。"""
        wrapped = Wrapped([flaky(), text_response("答")])

        response = await wrapped.model.complete(make_request(), CancelToken())

        assert response.content == "答"
        assert wrapped.inner.call_count == 2
        assert wrapped.failures == [
            {
                "model_id": "fake-model",
                "attempt": 1,
                "reason": "error",
                "retrying": True,
                "delay_ms": 1,
            }
        ]

    async def test_a_successful_retry_is_visible_in_the_event_stream(self) -> None:
        """悄悄好了也是一种隐瞒：两次失败之后成功，事件里就该有两条。"""
        wrapped = Wrapped([flaky(), flaky(), text_response("答")])

        await wrapped.model.complete(make_request(), CancelToken())

        assert [item["attempt"] for item in wrapped.failures] == [1, 2]
        assert all(item["retrying"] is True for item in wrapped.failures)

    async def test_exhausting_the_attempts_raises_the_original_error(self) -> None:
        """抛的是供应商那条错误而不是一条「重试失败了」——用户要看到真正的原因。"""
        wrapped = Wrapped([flaky("模型供应商限流。"), flaky("模型供应商限流。"), flaky("模型供应商限流。")])

        with pytest.raises(KaryviaError) as excinfo:
            await wrapped.model.complete(make_request(), CancelToken())

        assert excinfo.value.code is ErrorCode.EXTERNAL_MODEL_PROVIDER
        assert excinfo.value.user_message == "模型供应商限流。"
        assert wrapped.inner.call_count == 3
        assert [item["retrying"] for item in wrapped.failures] == [True, True, False]

    async def test_a_non_retryable_error_is_raised_on_the_first_try(self) -> None:
        wrapped = Wrapped([fatal()])

        with pytest.raises(KaryviaError):
            await wrapped.model.complete(make_request(), CancelToken())

        assert wrapped.inner.call_count == 1
        assert [item["retrying"] for item in wrapped.failures] == [False]

    async def test_a_retry_that_would_sleep_past_the_deadline_is_skipped(self) -> None:
        """看门狗本来也会掐断，但那样终态会变成一条超时取消，用户看不到真正的 429。"""
        wrapped = Wrapped(
            [flaky(retry_after_ms=60_000), text_response("答")],
            limits=TurnLimits(turn_timeout_ms=1_000),
        )

        with pytest.raises(KaryviaError):
            await wrapped.model.complete(make_request(), CancelToken())

        assert wrapped.inner.call_count == 1
        assert wrapped.failures[0]["retrying"] is False

    async def test_describe_is_delegated_untouched(self) -> None:
        wrapped = Wrapped([])
        assert wrapped.model.describe("fake-model").provider == "fake"


# --------------------------------------------------------------------------------- 流式


class _HalfStream:
    """先吐一片正文，再失败。**放行之后的失败不能重来**，这个替身专门造那一刻。"""

    def __init__(self) -> None:
        self.calls = 0

    def describe(self, model_id: str) -> object:  # pragma: no cover - 本用例不查能力
        raise AssertionError("这条路径不该被走到")

    async def complete(self, request: ModelRequest, cancel: object) -> ModelResponse:
        raise AssertionError("这条路径不该被走到")  # pragma: no cover

    def stream(self, request: ModelRequest, cancel: object) -> AsyncIterator[ModelChunk]:
        del request, cancel
        self.calls += 1
        return self._stream()

    async def _stream(self) -> AsyncIterator[ModelChunk]:
        yield ModelChunk(kind=ChunkKind.TEXT, text="半句")
        raise flaky("流到一半断了")


class TestStream:
    async def test_a_failure_before_any_output_is_retried(self) -> None:
        wrapped = Wrapped([flaky(), chunks_for(text_response("答"))])

        chunks = await wrapped.drain(make_request(stream=True), CancelToken())

        assert [chunk.text for chunk in chunks if chunk.kind is ChunkKind.TEXT] == ["答"]
        assert wrapped.inner.call_count == 2

    async def test_a_failure_after_the_first_chunk_is_not_retried(self) -> None:
        """那片正文已经变成 `ModelTextDelta` 发给用户了，重发会让答案出现两次。"""
        inner = _HalfStream()
        bus = EventBus(INSTANCE)
        model = RetryingModel(
            inner,  # type: ignore[arg-type]  # boundary: 结构化满足 ModelProvider
            FAST,
            bus,
            ledger=BudgetLedger(TurnLimits()),
            jitter=lambda: STEADY,
        )

        seen: list[ModelChunk] = []
        with pytest.raises(KaryviaError):
            async for chunk in model.stream(make_request(stream=True), CancelToken()):
                seen.append(chunk)

        assert inner.calls == 1, "放行之后不得重发"
        assert [chunk.text for chunk in seen] == ["半句"], "下游只该收到一份"

    async def test_a_done_error_chunk_before_any_output_is_retried(self) -> None:
        """契约允许 Provider 先 yield 一个 `DONE(ERROR)` 再抛。还没出输出时那也能重来。"""
        wrapped = Wrapped(
            [
                [ModelChunk(kind=ChunkKind.DONE, stop_reason=StopReason.ERROR)],
                chunks_for(text_response("答")),
            ]
        )

        chunks = await wrapped.drain(make_request(stream=True), CancelToken())

        assert [chunk.text for chunk in chunks if chunk.kind is ChunkKind.TEXT] == ["答"]
        assert wrapped.failures[0]["reason"] == "error"

    async def test_a_reasoning_chunk_counts_as_output(self) -> None:
        """它同样会被 orchestrator 翻成一条出站消息（`_emit(reasoning=True)`）。

        放行之后那个 `DONE(ERROR)` **原样交给 folder**（`failed_early` 只在放行之前成立），
        由 `folding.finish()` 去抛——这里因此看到两片分片、零重发、零异常。
        """
        wrapped = Wrapped(
            [
                [
                    ModelChunk(kind=ChunkKind.REASONING, text="想一想"),
                    ModelChunk(kind=ChunkKind.DONE, stop_reason=StopReason.ERROR),
                ]
            ]
        )

        chunks = await wrapped.drain(make_request(stream=True), CancelToken())

        assert [chunk.kind for chunk in chunks] == [ChunkKind.REASONING, ChunkKind.DONE]
        assert wrapped.inner.call_count == 1
        assert wrapped.failures == []

    async def test_held_chunks_are_released_in_order(self) -> None:
        """暂存不改变顺序：folder 看到的分片流与供应商发的一模一样。"""
        wrapped = Wrapped([chunks_for(tool_response(tool_call("fs.read")))])

        chunks = await wrapped.drain(make_request(stream=True), CancelToken())

        assert [chunk.kind for chunk in chunks] == [ChunkKind.TOOL_CALL, ChunkKind.DONE]

    async def test_the_hold_buffer_has_an_upper_bound(self) -> None:
        """撞上 `MAX_HELD_CHUNKS` 就放弃判定、整批放行——不为乱发分片的供应商无界缓冲。"""
        noise = [
            ModelChunk(
                kind=ChunkKind.OPAQUE,
                block=OpaqueBlock(provider="fake", kind="noise", payload={"i": index}),
            )
            for index in range(MAX_HELD_CHUNKS + 4)
        ]
        wrapped = Wrapped([noise])

        chunks = await wrapped.drain(make_request(stream=True), CancelToken())

        assert len(chunks) == len(noise)
        assert wrapped.inner.call_count == 1, "放行之后即便没有正文也不再重发"


# --------------------------------------------------------------------------------- 空回复


class TestEmptyResponse:
    async def test_an_empty_answer_is_retried(self) -> None:
        """ 之前：终帧空正文被 `emit_outbound` 丢掉，用户**什么都收不到**。"""
        wrapped = Wrapped([text_response(""), text_response("答")])

        response = await wrapped.model.complete(make_request(), CancelToken())

        assert response.content == "答"
        assert wrapped.failures[0]["reason"] == "empty_response"

    async def test_an_empty_stream_is_retried_too(self) -> None:
        wrapped = Wrapped([empty_stream(), chunks_for(text_response("答"))])

        chunks = await wrapped.drain(make_request(stream=True), CancelToken())

        assert [chunk.text for chunk in chunks if chunk.kind is ChunkKind.TEXT] == ["答"]
        assert wrapped.failures[0]["reason"] == "empty_response"

    async def test_persistent_silence_becomes_a_failure_with_a_sentence(self) -> None:
        """三次都空 → turn `FAILED`，而 `_finish` 会把这句话当正文发出去。"""
        wrapped = Wrapped([text_response(""), text_response(""), text_response("")])

        with pytest.raises(KaryviaError) as excinfo:
            await wrapped.model.complete(make_request(), CancelToken())

        assert excinfo.value.code is ErrorCode.EXTERNAL_MODEL_PROVIDER
        assert excinfo.value.user_message == "模型连续 3 次返回空回答。"
        # 抛出去的这条**不可重试**：重试期间那条 `retryable=True` 只是给判定看的，
        # 抛给上层会让它以为还能再来。
        assert excinfo.value.retryable is False

    async def test_a_refusal_is_accepted_as_is(self) -> None:
        """内容过滤的空响应是模型的决定，不重发也不判失败。"""
        refusal = ModelResponse(
            model_id="fake-model", stop_reason=StopReason.CONTENT_FILTER, content=""
        )
        wrapped = Wrapped([refusal])

        response = await wrapped.model.complete(make_request(), CancelToken())

        assert response.stop_reason is StopReason.CONTENT_FILTER
        assert wrapped.failures == []

    async def test_a_turn_that_already_ran_tools_keeps_its_empty_answer(self) -> None:
        """**一条已经跑过工具的 turn 产出了真东西**——工具结果、产物、 挂在终帧上的
        出站附件都已经落地。因为模型的收尾句是空的就判它 `FAILED`，会把这些一起否掉。
        """
        wrapped = Wrapped([text_response("")], tool_calls=1)

        response = await wrapped.model.complete(make_request(), CancelToken())

        assert response.content == ""
        assert wrapped.inner.call_count == 1
        assert wrapped.failures == []

    async def test_the_switch_restores_the_old_behaviour(self) -> None:
        """`retry_empty_response=false` = 原样放行，也就是此前的行为。"""
        policy = RetryPolicy(max_attempts=3, base_delay_ms=1, retry_empty_response=False)
        wrapped = Wrapped([text_response("")], policy=policy)

        response = await wrapped.model.complete(make_request(), CancelToken())

        assert response.content == ""
        assert wrapped.inner.call_count == 1
        assert wrapped.failures == []


# --------------------------------------------------------------------------------- 取消


class TestCancellation:
    async def test_a_cancelled_turn_stops_retrying_and_reports_the_cancellation(self) -> None:
        """抛取消而不是抛原来那个 503：用户按了 Ctrl-C，终态该是 `CANCELLED` 不是 `FAILED`。

        `terminal_from_error` 按 `ErrorCategory` 分叉，因此抛错了类别就会分叉错。
        """
        wrapped = Wrapped([flaky(retry_after_ms=50), text_response("答")])
        token = CancelToken()
        token.request(CancelReason.USER)

        with pytest.raises(KaryviaError) as excinfo:
            await wrapped.model.complete(make_request(), token)

        assert excinfo.value.code is ErrorCode.CANCELLED_BY_USER
        assert wrapped.inner.call_count == 1
        # 事件仍然如实记着「本来打算重试」——退避是在那之后才被打断的。
        assert wrapped.failures[0]["retrying"] is True
