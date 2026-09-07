"""模型请求重试：瞬时故障重发与空回复重发。

职责：定义 `RetryPolicy` 与「这次失败该不该再来一次、等多久」的纯判定，并以一个包住
`ModelProvider` 的 `RetryingModel` 兑现它——含「首个实质分片放行之后就不能重来」这条
规则，以及每次失败尝试的 `model.request_failed` 事件。
不负责：执行循环（`engine.py`）、决定 turn 终态（`events.py`）、判断请求内容对不对
（Provider 自己的事）、长度截断后的续写（那不是重试，见下方第 4 条）；本模块除退避等待
之外不含 IO，也不认识 session、工具与配置文件。

重试采用 `ModelProvider` 包装器，因为它恰好拥有安全重试所需的边界：完整的单次请求、是否
已经向下游放行实质分片，以及本次调用是否拿到响应。Orchestrator 不能重跑整个 Engine；
那会丢失 Engine 累积的消息并可能重复执行工具。Engine 也不需要为重试增加新的依赖槽。

重试与续写按“是否拿到响应”区分：请求在首个实质分片之前失败可以重发；收到
`StopReason.MAX_TOKENS` 后的续写由 Engine 在同一消息序列与预算账本上完成。本模块不改写
已经收到的响应，也不重发任何用户已经看见的内容。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Final

from karyvia.contracts import (
    CancelSignal,
    ChunkKind,
    ErrorCategory,
    ErrorCode,
    EventName,
    KaryviaError,
    ModelChunk,
    ModelInfo,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    StopReason,
)
from karyvia.kernel.observability import EventBus

from .limits import BudgetLedger
from .translation import as_karyvia

__all__ = [
    "DEFAULT_RETRY_BASE_DELAY_MS",
    "DEFAULT_RETRY_EMPTY_RESPONSE",
    "DEFAULT_RETRY_MAX_ATTEMPTS",
    "DEFAULT_RETRY_MAX_DELAY_MS",
    "MAX_HELD_CHUNKS",
    "RETRY_POLL_MS",
    "RetryPolicy",
    "RetryingModel",
    "is_empty_answer",
    "retry_delay_ms",
    "wait_before_retry",
]

#: 四项默认值。**`kernel/config/defaults.py` 有一份同名副本**（那一层不得 module-level
#: import `kernel.turn`，既定冷启动预算 300 ms），由
#: `test_retry_defaults_match_the_turn_package` 逐项钉住。
#:
#: `max_attempts` 是**总尝试次数含第一次**，因此 `1` 的含义是「不重试」——不需要再加
#: 一个 `enabled` 开关，两个旋钮表达同一件事只会让它们有机会互相矛盾。
DEFAULT_RETRY_MAX_ATTEMPTS: Final = 3
DEFAULT_RETRY_BASE_DELAY_MS: Final = 500
DEFAULT_RETRY_MAX_DELAY_MS: Final = 8_000
DEFAULT_RETRY_EMPTY_RESPONSE: Final = True

#: 退避等待期间检查取消的间隔。这里拿到的是只读 `CancelSignal`，不能依赖 Kernel 专有的
#: `CancelToken.wait()`；如果等完整个延迟才检查，退避期间就无法及时取消。
RETRY_POLL_MS: Final = 50

#: 首个实质分片出现之前最多暂存多少片。撞上它就放弃判空、整批放行、从此直通——
#: 不为一个乱发 `USAGE` 的供应商无界缓冲。
MAX_HELD_CHUNKS: Final = 8

#: 「实质分片」：一旦放行，这一轮就不能再重来了（它已经变成用户看得见的输出）。
#: `REASONING` 也算——`orchestrator._on_event` 把它翻成 `ModelReasoningDelta` 并
#: `_emit(reasoning=True)`，同样出得去。
_SUBSTANTIVE: Final = frozenset({ChunkKind.TEXT, ChunkKind.REASONING, ChunkKind.TOOL_CALL})

#: 「空回复」只在这些停止原因下成立（`None` = 流里没有 DONE 分片，同样算）。
#: `CONTENT_FILTER` 是刻意的拒答、`MAX_TOKENS` 是预算耗尽、`STOP_SEQUENCE` 是撞上了停止
#: 序列——三者重发只会再来一次，而 `ERROR` / `CANCELLED` 走的是失败路径不是空回复路径。
_EMPTY_RETRY_STOPS: Final = frozenset({StopReason.END_TURN})

_REASON_ERROR: Final = "error"
_REASON_EMPTY: Final = "empty_response"

_EMPTY_ANSWER: Final = "模型返回了空回答。"
_EMPTY_EXHAUSTED: Final = "模型连续 {attempts} 次返回空回答。"
_STREAM_FAILED: Final = "模型流式响应在产生任何输出之前失败。"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """一次模型请求最多重发几次、每次等多久。

    它**不是** `TurnLimits` 的第七项：那六项说的是「一次 turn 能用掉多少」，而重试说的是
    「一次失败之后怎么办」。`TurnLimits` 的 docstring 明写着不要往里加第七项。
    """

    max_attempts: int = DEFAULT_RETRY_MAX_ATTEMPTS
    base_delay_ms: int = DEFAULT_RETRY_BASE_DELAY_MS
    max_delay_ms: int = DEFAULT_RETRY_MAX_DELAY_MS
    #: 空回复（既无正文也无工具调用）算不算故障。`False` 表示原样放行：
    #: `emit_outbound` 对空正文终帧返回 `None`，用户什么都收不到而 turn 记
    #: `COMPLETED`。
    retry_empty_response: bool = DEFAULT_RETRY_EMPTY_RESPONSE

    def __post_init__(self) -> None:
        for name in ("max_attempts", "base_delay_ms", "max_delay_ms"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise KaryviaError(
                    ErrorCode.CONFIG_INVALID,
                    "重试策略的三项数值必须是正整数。",
                    detail={"field": name, "value": repr(value)},
                )


def retry_delay_ms(
    error: KaryviaError,
    attempt: int,
    policy: RetryPolicy,
    *,
    jitter: Callable[[], float] = random.random,
) -> int | None:
    """这次失败之后要不要再来一次；要就返回等待毫秒数，不要返回 `None`。纯函数。

    判定顺序里第一条是硬的：**取消类一律不重试**。`cancel.py` 写的是
    `retryable=reason is not CancelReason.SHUTDOWN`，也就是说**取消错误自称可重试**——
    重发一个已经被用户按了 Ctrl-C 的请求是最不该发生的事。判据取 `ErrorCategory`，避免
    新增取消错误码时漏改这里的枚举清单。

    其余全看 `error.retryable`。这是唯一依据：Provider 已经如实标过了
    （`model_openai/faults.py` 连 429 里的「限速」与「欠费」都分开标），kernel 再按状态码
    猜一遍就会出现两处判断不一致。

    **服务端说的话压过我们算的退避**：`detail["retry_after_ms"]` 有值就原样用，
    **且不加抖动**——抖动只会把它往小了调，而那意味着再吃一次 429。计算出来的退避才乘
    `[0.5, 1.0]` 的系数，防的是多个 conversation 同时撞墙后一起回来。
    """
    if error.category is ErrorCategory.CANCELLED:
        return None
    if not error.retryable or attempt >= policy.max_attempts:
        return None
    hinted = error.detail.get("retry_after_ms")
    if isinstance(hinted, int) and not isinstance(hinted, bool) and hinted >= 0:
        return hinted
    ceiling = min(policy.max_delay_ms, policy.base_delay_ms * 2 ** (attempt - 1))
    return max(1, int(ceiling * (0.5 + 0.5 * jitter())))


def is_empty_answer(stop: StopReason | None, *, content: str = "", tool_calls: int = 0) -> bool:
    """这次响应是不是「模型什么都没说」。

    有正文或有工具调用都不算空。停止原因必须在 `_EMPTY_RETRY_STOPS` 里（或缺失）——
    一次内容过滤的空响应是模型的**决定**，重发它既没用也不该。
    """
    if content.strip() or tool_calls:
        return False
    return stop is None or stop in _EMPTY_RETRY_STOPS


async def wait_before_retry(delay_ms: int, cancel: CancelSignal) -> None:
    """退避等待。**被取消时抛取消错误**而不是静默返回。

    抛而不是返回一个布尔：静默返回会让调用方把原来那个 503 抛出去，于是
    `terminal_from_error` 按 `ErrorCategory.EXTERNAL_SERVICE` 判成 `TurnFailed`——
    而用户明明是按了 Ctrl-C，终态该是 `CANCELLED`。
    """
    remaining = delay_ms
    while True:
        cancel.raise_if_requested()
        if remaining <= 0:
            return
        slice_ms = min(RETRY_POLL_MS, remaining)
        await asyncio.sleep(slice_ms / 1000)
        remaining -= slice_ms


class RetryingModel:
    """包住一个 `ModelProvider`，把可重试的失败与空回复变成重发。

    结构化满足 `contracts.ModelProvider`；装配在 `EngineDeps.model` 上，engine 因此
    不知道重试存在。`ledger` 与 engine 是同一本账，用来回答两个问题：

    - **还剩多久**（`remaining_ms()`）：睡过 turn 死线的重试不如不重试。看门狗本来也会
      掐断，但那样终态错误会变成一条超时取消，用户看不到真正的 429。
    - **这条 turn 跑过工具没有**（`tool_calls`）：见 `_treats_as_empty` 那一条。
    """

    __slots__ = ("_bus", "_inner", "_jitter", "_ledger", "_policy")

    def __init__(
        self,
        inner: ModelProvider,
        policy: RetryPolicy,
        bus: EventBus,
        *,
        ledger: BudgetLedger,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._inner = inner
        self._policy = policy
        self._bus = bus
        self._ledger = ledger
        self._jitter = jitter

    def describe(self, model_id: str) -> ModelInfo:
        """纯策略查询，不涉及重试。"""
        return self._inner.describe(model_id)

    async def complete(self, request: ModelRequest, cancel: CancelSignal) -> ModelResponse:
        """一次非流式请求，失败或空回复时重发。"""
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._inner.complete(request, cancel)
            except Exception as raw:
                error = as_karyvia(raw)
                if not await self._again(error, attempt, request, cancel, reason=_REASON_ERROR):
                    raise
                continue
            if not self._treats_as_empty(
                response.stop_reason,
                content=response.content,
                tool_calls=len(response.tool_calls),
            ):
                return response
            error = self._empty_error(retryable=True)
            if not await self._again(error, attempt, request, cancel, reason=_REASON_EMPTY):
                raise self._give_up(error, _REASON_EMPTY, attempt)

    async def stream(
        self, request: ModelRequest, cancel: CancelSignal
    ) -> AsyncIterator[ModelChunk]:
        """一次流式请求。**只在首个实质分片放行之前可以重来。**

        放行之后的失败原样抛出：那些分片已经变成 `ModelTextDelta` 发给用户了，重发会让
        同一段答案出现两次。`StopReason.ERROR` 造成的 `folding.finish()` 失败因此在这一层
        看不见也不该看见——它发生在有输出之后。
        """
        attempt = 0
        while True:
            attempt += 1
            gate = _StreamGate()
            failure: KaryviaError | None = None
            try:
                async for chunk in self._inner.stream(request, cancel):
                    if gate.failed_early(chunk):
                        failure = self._stream_error(request)
                        break
                    for item in gate.admit(chunk):
                        yield item
            # 捕 Exception 而不是 BaseException：消费方中止会以 `GeneratorExit` 到达
            # 这个 yield 点，那必须穿透（engine 基于相同理由）。
            except Exception as raw:
                if gate.opened:
                    raise
                failure = as_karyvia(raw)
            if gate.opened:
                return
            verdict = self._after_stream(gate, failure)
            if verdict is None:
                for item in gate.held:  # 不判空 / 不该重试的停止原因：交给 folder。
                    yield item
                return
            failure, reason = verdict
            if not await self._again(failure, attempt, request, cancel, reason=reason):
                raise self._give_up(failure, reason, attempt)

    # ------------------------------------------------------------------ 内部

    def _after_stream(
        self, gate: _StreamGate, failure: KaryviaError | None
    ) -> tuple[KaryviaError, str] | None:
        """流结束之后：这一轮算失败吗？`None` = 不算，把暂存交给 folder 正常收尾。"""
        if failure is not None:
            return failure, _REASON_ERROR
        if not self._treats_as_empty(gate.stop):
            return None
        return self._empty_error(retryable=True), _REASON_EMPTY

    def _give_up(self, failure: KaryviaError, reason: str, attempt: int) -> KaryviaError:
        """不再重试时该抛哪个错误。

        空回复抛的是**重新构造**的那一个：`retryable=False` 且话里带上试了几次。重试期间
        那条 `retryable=True` 是给 `retry_delay_ms` 看的，把它抛给用户会让上层以为还能再来。
        """
        if reason == _REASON_EMPTY:
            return self._empty_error(retryable=False, attempts=attempt)
        return failure

    def _stream_error(self, request: ModelRequest) -> KaryviaError:
        """「出任何输出之前就失败了」。折的码与 `folding.finish()` 相同，区别只在这里还
        来得及重来。"""
        return KaryviaError(
            ErrorCode.EXTERNAL_MODEL_PROVIDER,
            _STREAM_FAILED,
            detail={"model_id": request.model_id},
            retryable=True,
        )

    def _treats_as_empty(
        self, stop: StopReason | None, *, content: str = "", tool_calls: int = 0
    ) -> bool:
        """空回复算不算故障。开关关掉时恒为假——那正是「原样放行」。

        **还有一条比开关更硬的**：只在这条 turn **还没执行过工具**时才算。一条已经跑过
        工具的 turn 产出了真东西——工具结果、产物和终帧附件都已经落地；
        因为模型的收尾句是空的就把它判成 `FAILED`，会把这些真做完了的事一起否掉。
        而首轮就空回复的那条 turn 什么都没有，那才是「用户彻底看不到东西」的情形，
        也正是本模块要修的那一个。
        """
        if self._ledger.tool_calls:
            return False
        return self._policy.retry_empty_response and is_empty_answer(
            stop, content=content, tool_calls=tool_calls
        )

    def _empty_error(self, *, retryable: bool, attempts: int = 0) -> KaryviaError:
        """空回复的错误对象。重试期间与用尽之后是同一个码，只有 `retryable` 与话不同。"""
        return KaryviaError(
            ErrorCode.EXTERNAL_MODEL_PROVIDER,
            _EMPTY_ANSWER if retryable else _EMPTY_EXHAUSTED.format(attempts=attempts),
            detail={"attempts": attempts} if attempts else {},
            retryable=retryable,
        )

    async def _again(
        self,
        error: KaryviaError,
        attempt: int,
        request: ModelRequest,
        cancel: CancelSignal,
        *,
        reason: str,
    ) -> bool:
        """把这次失败发出去，并在该重试时等完退避。返回是否可以重发。

        **每次失败的尝试都发一条 `model.request_failed`**：一次成功的重试因此在事件流里是
        N 条 `model.request_failed` 加一条 `model.response_received`，而不是悄悄恢复。
        """
        delay = retry_delay_ms(error, attempt, self._policy, jitter=self._jitter)
        if delay is not None and delay >= self._ledger.remaining_ms():
            delay = None  # 睡过 turn 死线的重试不如不重试，见类 docstring。
        self._bus.publish(
            EventName.MODEL_REQUEST_FAILED,
            correlation=request.correlation,
            payload={
                "model_id": request.model_id,
                "attempt": attempt,
                "reason": reason,
                "retrying": delay is not None,
                "delay_ms": delay or 0,
            },
            error=error,
        )
        if delay is None:
            return False
        await wait_before_retry(delay, cancel)
        return True


class _StreamGate:
    """首个实质分片放行之前的闸门：暂存、判定、放行。

    **`opened` 一旦为真就永远为真**——放行过的分片已经变成用户看得见的输出，这一轮就不能
    再重来了。做成一个对象而不是 `stream()` 里的两个局部变量，是因为那个方法本来就要同时
    管重试循环、异常分类与事件，ruff 的 `C901` 当场报了复杂度。
    """

    __slots__ = ("held", "opened")

    def __init__(self) -> None:
        self.held: list[ModelChunk] = []
        self.opened = False

    def admit(self, chunk: ModelChunk) -> tuple[ModelChunk, ...]:
        """收下一片，返回**这一刻该放行**的分片（还在暂存时是空的）。"""
        if self.opened:
            return (chunk,)
        # 撞上暂存上界就放弃判定、整批放行、从此直通——不为一个乱发 `USAGE` 的供应商
        # 无界缓冲。
        if chunk.kind in _SUBSTANTIVE or len(self.held) + 1 >= MAX_HELD_CHUNKS:
            self.opened = True
            flushed = (*self.held, chunk)
            self.held.clear()
            return flushed
        self.held.append(chunk)
        return ()

    def failed_early(self, chunk: ModelChunk) -> bool:
        """这一片是不是「还没出任何输出就失败了」的信号。

        契约允许 Provider 在流中途失败时先 yield 一个 `DONE(stop_reason=ERROR)` 再抛
        （`protocols.py::stream` 的异常约定）。**放行之后的同一个分片不算**——那时
        `folding.finish()` 会去抛，而已经流出去的文本不能重来。
        """
        return (
            not self.opened
            and chunk.kind is ChunkKind.DONE
            and chunk.stop_reason is StopReason.ERROR
        )

    @property
    def stop(self) -> StopReason | None:
        """暂存里那个 DONE 报的停止原因。没有 DONE 就是 `None`（同样按空回复判）。"""
        for chunk in self.held:
            if chunk.kind is ChunkKind.DONE:
                return chunk.stop_reason
        return None
