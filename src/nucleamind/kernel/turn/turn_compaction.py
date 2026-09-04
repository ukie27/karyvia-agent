"""Turn 内模型请求压缩的通用机制。

职责：把最终请求投影为不可拆分单元、调用选中的插件、校验连续前缀结果，并以状态化
`ModelProvider` 包装器把摘要延续到同一 Turn 的后续迭代。
不负责：提供压缩算法、读写 Session、改变 Engine 的迭代/工具预算或增加 Engine 依赖槽。

Kernel 没有备用压缩策略。插件超时、抛异常、返回非法结果或压缩后仍超预算都会终止当前
Turn；默认可用性由 Runtime 必选的内建 `basic` 能力保证。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, replace

from nucleamind.contracts import (
    CancelSignal,
    CapabilityKind,
    CapabilityRef,
    ChunkKind,
    CompactionModel,
    ErrorCode,
    EventName,
    ModelChunk,
    ModelInfo,
    ModelMessage,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    NucleaError,
    ProviderId,
    Role,
    SamplingParams,
    TurnCompactionRequest,
    TurnCompactionResult,
    TurnContextCompactor,
    TurnContextUnit,
    TurnContextUnitKind,
    wrap_untrusted,
)
from nucleamind.kernel.observability import EventBus

from .limits import BudgetLedger
from .request_size import ContextBudget, TokenAccounting, estimate_messages_tokens

__all__ = [
    "DEFAULT_TURN_COMPACTOR_TIMEOUT_MS",
    "TurnCompactionPolicy",
    "TurnCompactingModel",
    "project_units",
]

DEFAULT_TURN_COMPACTOR_TIMEOUT_MS = 120_000
_SUMMARY_SOURCE = "turn-compactor"
_CURRENT_USER_CHANGED = "before_model_request 删除或改写了当前用户输入。"
_ORPHAN_TOOL_RESULT = "工具结果缺少紧邻的 assistant 调用声明。"
_TOOL_EXCHANGE_MISMATCH = "assistant 工具调用与 tool 结果不完整匹配。"
_SUBSTANTIVE_CHUNKS = frozenset(
    {ChunkKind.TEXT, ChunkKind.REASONING, ChunkKind.TOOL_CALL}
)


@dataclass(frozen=True, slots=True)
class TurnCompactionPolicy:
    """当前实例选中的 Turn Compactor 与单次调用预算。"""

    compactor: TurnContextCompactor
    name: str
    owner: ProviderId
    timeout_ms: int = DEFAULT_TURN_COMPACTOR_TIMEOUT_MS

    @property
    def ref(self) -> CapabilityRef:
        return CapabilityRef(
            kind=CapabilityKind.TURN_COMPACTOR,
            name=self.name,
            provider=self.owner,
        )


@dataclass(frozen=True, slots=True)
class _Span:
    unit: TurnContextUnit
    start: int
    stop: int


@dataclass(frozen=True, slots=True)
class _Projection:
    messages: tuple[ModelMessage, ...]
    spans: tuple[_Span, ...]

    @property
    def units(self) -> tuple[TurnContextUnit, ...]:
        return tuple(span.unit for span in self.spans)


def project_units(
    messages: Sequence[ModelMessage], protected_user: ModelMessage
) -> tuple[TurnContextUnit, ...]:
    """公开纯投影入口；结构非法时立即失败，不把修复责任交给插件。"""
    return _project(tuple(messages), protected_user, None).units


def _project(
    messages: tuple[ModelMessage, ...],
    protected_user: ModelMessage,
    accounting: TokenAccounting | None,
) -> _Projection:
    protected_index = _protected_user_index(messages, protected_user)
    spans: list[_Span] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.role is Role.SYSTEM or index == protected_index:
            index += 1
            continue
        if message.tool_calls:
            stop = index + 1 + len(message.tool_calls)
            exchange = messages[index:stop]
            _validate_exchange(exchange)
            _append_span(
                spans, exchange, index, stop, TurnContextUnitKind.TOOL_EXCHANGE, accounting
            )
            index = stop
            continue
        if message.role is Role.TOOL:
            raise _structure_error(_ORPHAN_TOOL_RESULT)
        kind = (
            TurnContextUnitKind.CONTINUATION
            if index > protected_index and message.role is Role.ASSISTANT
            else TurnContextUnitKind.BASE
        )
        _append_span(spans, (message,), index, index + 1, kind, accounting)
        index += 1
    return _Projection(messages, tuple(spans))


def _protected_user_index(
    messages: tuple[ModelMessage, ...], protected_user: ModelMessage
) -> int:
    matches = [index for index, message in enumerate(messages) if message == protected_user]
    if not matches:
        raise _structure_error(_CURRENT_USER_CHANGED)
    return matches[-1]


def _validate_exchange(messages: tuple[ModelMessage, ...]) -> None:
    assistant = messages[0]
    results = messages[1:]
    expected = [call.call_id for call in assistant.tool_calls]
    actual = [message.tool_call_id for message in results if message.role is Role.TOOL]
    if len(results) != len(expected) or len(actual) != len(expected) or set(actual) != set(expected):
        raise _structure_error(_TOOL_EXCHANGE_MISMATCH)


def _append_span(
    spans: list[_Span],
    messages: tuple[ModelMessage, ...],
    start: int,
    stop: int,
    kind: TurnContextUnitKind,
    accounting: TokenAccounting | None,
) -> None:
    unit = TurnContextUnit(
        unit_id=f"unit-{len(spans) + 1}",
        kind=kind,
        messages=messages,
        estimated_tokens=(
            accounting.estimate_messages(messages)
            if accounting is not None
            else estimate_messages_tokens(messages)
        ),
    )
    spans.append(_Span(unit, start, stop))


def _structure_error(message: str) -> NucleaError:
    return NucleaError(ErrorCode.KERNEL_INVARIANT_VIOLATED, message)


class _BoundCompactionModel:
    """把当前 Provider、模型标识与 Turn 关联收窄成 `CompactionModel`。"""

    __slots__ = (
        "_accounting",
        "_bus",
        "_info",
        "_inner",
        "_ledger",
        "_request",
        "_timeout_ms",
    )

    def __init__(
        self,
        inner: ModelProvider,
        request: ModelRequest,
        info: ModelInfo,
        bus: EventBus,
        ledger: BudgetLedger,
        accounting: TokenAccounting,
        timeout_ms: int,
    ) -> None:
        self._inner = inner
        self._accounting = accounting
        self._request = request
        self._info = info
        self._bus = bus
        self._ledger = ledger
        self._timeout_ms = timeout_ms

    @property
    def info(self) -> ModelInfo:
        return self._info

    async def complete(
        self,
        messages: Sequence[ModelMessage],
        cancel: CancelSignal,
        *,
        max_output_tokens: int | None = None,
    ) -> ModelResponse:
        declared = self._info.max_output_tokens
        output_limit = max_output_tokens
        if declared > 0 and (output_limit is None or output_limit > declared):
            output_limit = declared
        timeout_ms = max(
            1,
            min(self._timeout_ms, self._request.timeout_ms, self._ledger.remaining_ms()),
        )
        request = ModelRequest(
            model_id=self._request.model_id,
            messages=tuple(messages),
            correlation=self._request.correlation,
            params=SamplingParams(max_output_tokens=output_limit),
            stream=False,
            timeout_ms=timeout_ms,
        )
        self._bus.publish(
            EventName.MODEL_REQUEST_STARTED,
            correlation=request.correlation,
            payload={
                "model_id": request.model_id,
                "messages": len(request.messages),
                "tools": 0,
                "purpose": "turn_compaction",
            },
        )
        response = await self._inner.complete(request, cancel)
        self._accounting.observe(request, response.usage)
        self._bus.publish(
            EventName.MODEL_RESPONSE_RECEIVED,
            correlation=request.correlation,
            payload={
                "stop_reason": response.stop_reason.value,
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "purpose": "turn_compaction",
            },
        )
        return response


class TurnCompactingModel:
    """在每次 Provider 调用前限制请求大小，并延续本 Turn 已产生的摘要。"""

    __slots__ = (
        "_accounting",
        "_budget",
        "_bus",
        "_info",
        "_inner",
        "_ledger",
        "_policy",
        "_prepared",
        "_protected_user",
        "_source",
    )

    def __init__(
        self,
        inner: ModelProvider,
        policy: TurnCompactionPolicy,
        bus: EventBus,
        ledger: BudgetLedger,
        *,
        budget: ContextBudget,
        accounting: TokenAccounting,
        protected_user: ModelMessage,
        model_info: ModelInfo,
    ) -> None:
        self._inner = inner
        self._accounting = accounting
        self._policy = policy
        self._bus = bus
        self._ledger = ledger
        self._budget = budget
        self._protected_user = protected_user
        self._info = model_info
        self._source: tuple[ModelMessage, ...] = ()
        self._prepared: tuple[ModelMessage, ...] = ()

    def describe(self, model_id: str) -> ModelInfo:
        return self._inner.describe(model_id)

    async def complete(self, request: ModelRequest, cancel: CancelSignal) -> ModelResponse:
        prepared = await self._prepare(request, cancel)
        try:
            response = await self._inner.complete(prepared, cancel)
        except NucleaError as error:
            if error.code is not ErrorCode.EXTERNAL_MODEL_CONTEXT_OVERFLOW:
                raise
            prepared = await self._prepare(request, cancel, force=True)
            response = await self._inner.complete(prepared, cancel)
        self._accounting.observe(prepared, response.usage)
        return response

    async def stream(
        self, request: ModelRequest, cancel: CancelSignal
    ) -> AsyncIterator[ModelChunk]:
        prepared = await self._prepare(request, cancel)
        emitted = False
        try:
            async for chunk in self._inner.stream(prepared, cancel):
                emitted = emitted or chunk.kind in _SUBSTANTIVE_CHUNKS
                if chunk.usage is not None:
                    self._accounting.observe(prepared, chunk.usage)
                yield chunk
        except NucleaError as error:
            if error.code is not ErrorCode.EXTERNAL_MODEL_CONTEXT_OVERFLOW or emitted:
                raise
            prepared = await self._prepare(request, cancel, force=True)
            async for chunk in self._inner.stream(prepared, cancel):
                if chunk.usage is not None:
                    self._accounting.observe(prepared, chunk.usage)
                yield chunk

    async def _prepare(
        self,
        request: ModelRequest,
        cancel: CancelSignal,
        *,
        force: bool = False,
    ) -> ModelRequest:
        source = request.messages
        carried = self._carry(source)
        current = replace(request, messages=carried)
        estimated = self._accounting.estimate_request(current)
        if not force and estimated <= self._budget.trigger_limit:
            self._remember(source, current.messages)
            return current

        projection = _project(current.messages, self._protected_user, self._accounting)
        if not projection.spans:
            raise self._too_large(estimated)
        target = self._budget.target_limit
        if force and estimated <= target:
            target = max(1, int(estimated * 0.8))
        compaction_request = TurnCompactionRequest(
            request=current,
            units=projection.units,
            target_tokens=target,
            estimated_tokens=estimated,
            correlation=current.correlation,
        )
        model: CompactionModel = _BoundCompactionModel(
            self._inner,
            current,
            self._info,
            self._bus,
            self._ledger,
            self._accounting,
            self._policy.timeout_ms,
        )
        result = await self._invoke(compaction_request, model, cancel)
        compacted = replace(current, messages=self._rebuild(projection, result))
        final_size = self._accounting.estimate_request(compacted)
        if final_size > target:
            raise self._too_large(final_size)
        self._remember(source, compacted.messages)
        return compacted

    def _carry(self, source: tuple[ModelMessage, ...]) -> tuple[ModelMessage, ...]:
        if self._source and source[: len(self._source)] == self._source:
            return (*self._prepared, *source[len(self._source) :])
        return source

    def _remember(
        self, source: tuple[ModelMessage, ...], prepared: tuple[ModelMessage, ...]
    ) -> None:
        self._source = source
        self._prepared = prepared

    async def _invoke(
        self,
        request: TurnCompactionRequest,
        model: CompactionModel,
        cancel: CancelSignal,
    ) -> TurnCompactionResult:
        try:
            return await asyncio.wait_for(
                self._policy.compactor.compact(request, model, cancel),
                timeout=self._policy.timeout_ms / 1000,
            )
        except TimeoutError as error:
            raise NucleaError(
                ErrorCode.TIMEOUT_TURN_COMPACTION,
                "Turn Context Compactor 超时。",
                detail={"timeout_ms": self._policy.timeout_ms},
                capability=self._policy.ref,
            ) from error
        except NucleaError:
            raise
        except Exception as error:
            raise NucleaError(
                ErrorCode.PLUGIN_TURN_COMPACTION_FAILED,
                "Turn Context Compactor 抛出了异常。",
                detail={"exception": type(error).__name__},
                capability=self._policy.ref,
            ) from error

    def _rebuild(
        self, projection: _Projection, result: TurnCompactionResult
    ) -> tuple[ModelMessage, ...]:
        through = result.through_units
        summary = result.summary.strip()
        if through < 1 or through > len(projection.spans) or not summary:
            raise NucleaError(
                ErrorCode.PLUGIN_TURN_COMPACTION_FAILED,
                "Turn Context Compactor 返回了非法结果。",
                detail={
                    "through_units": through,
                    "units": len(projection.spans),
                    "empty_summary": not bool(summary),
                },
                capability=self._policy.ref,
            )
        covered = projection.spans[:through]
        removed = {
            index for span in covered for index in range(span.start, span.stop)
        }
        insert_at = covered[0].start
        rendered = ModelMessage(
            role=Role.USER,
            content=wrap_untrusted(summary, source=_SUMMARY_SOURCE),
        )
        messages: list[ModelMessage] = []
        for index, message in enumerate(projection.messages):
            if index == insert_at:
                messages.append(rendered)
            if index not in removed:
                messages.append(message)
        return tuple(messages)

    def _too_large(self, estimated: int) -> NucleaError:
        return NucleaError(
            ErrorCode.INPUT_TOO_LARGE,
            "压缩后的模型请求仍超过上下文预算。",
            detail={
                "estimated_tokens": estimated,
                "trigger_tokens": self._budget.trigger_limit,
                "target_tokens": self._budget.target_limit,
            },
            capability=self._policy.ref,
        )
