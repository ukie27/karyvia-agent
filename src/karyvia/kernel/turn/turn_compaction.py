"""模型请求边界上的统一上下文压缩机制。

职责：把最终请求投影为不可拆分单元、调用选中的插件、校验连续前缀结果，并以状态化
`ModelProvider` 包装器把摘要延续到同一 Turn 的后续迭代；可映射的 Session 前缀交给
`SessionCompactionTracker` 登记。
不负责：提供压缩算法、读写 Session、改变 Engine 的迭代/工具预算或增加 Engine 依赖槽。

Kernel 没有备用压缩策略。插件超时、抛异常、返回非法结果或压缩后仍超预算都会终止当前
Turn；默认可用性由 Runtime 必选的内建 `basic` 能力保证。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, replace

from karyvia.contracts import (
    CancelSignal,
    CapabilityKind,
    CapabilityRef,
    ChunkKind,
    CompactionModel,
    ErrorCode,
    EventName,
    KaryviaError,
    ModelChunk,
    ModelInfo,
    ModelMessage,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ProviderId,
    Role,
    SamplingParams,
    TurnCompactionRequest,
    TurnCompactionResult,
    TurnContextCompactor,
    wrap_untrusted,
)
from karyvia.kernel.observability import EventBus

from .compaction import SessionCompactionTracker
from .limits import BudgetLedger
from .request_size import ContextBudget, TokenAccounting
from .turn_compaction_projection import (
    Projection,
    project,
    project_units,
)

__all__ = [
    "DEFAULT_TURN_COMPACTOR_TIMEOUT_MS",
    "TurnCompactionPolicy",
    "TurnCompactingModel",
    "project_units",
]

DEFAULT_TURN_COMPACTOR_TIMEOUT_MS = 120_000
_SUMMARY_SOURCE = "turn-compactor"
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
        "_session_compaction",
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
        session_compaction: SessionCompactionTracker | None = None,
    ) -> None:
        self._inner = inner
        self._accounting = accounting
        self._policy = policy
        self._bus = bus
        self._ledger = ledger
        self._budget = budget
        self._protected_user = protected_user
        self._info = model_info
        self._session_compaction = session_compaction
        self._source: tuple[ModelMessage, ...] = ()
        self._prepared: tuple[ModelMessage, ...] = ()

    def describe(self, model_id: str) -> ModelInfo:
        return self._inner.describe(model_id)

    async def complete(self, request: ModelRequest, cancel: CancelSignal) -> ModelResponse:
        prepared = await self._prepare(request, cancel)
        try:
            response = await self._inner.complete(prepared, cancel)
        except KaryviaError as error:
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
        except KaryviaError as error:
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

        projection = project(
            current.messages,
            self._protected_user,
            self._accounting,
        )
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
        self._record_session_compaction(projection, result)
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
            raise KaryviaError(
                ErrorCode.TIMEOUT_TURN_COMPACTION,
                "Turn Context Compactor 超时。",
                detail={"timeout_ms": self._policy.timeout_ms},
                capability=self._policy.ref,
            ) from error
        except KaryviaError:
            raise
        except Exception as error:
            raise KaryviaError(
                ErrorCode.PLUGIN_TURN_COMPACTION_FAILED,
                "Turn Context Compactor 抛出了异常。",
                detail={"exception": type(error).__name__},
                capability=self._policy.ref,
            ) from error

    def _rebuild(
        self, projection: Projection, result: TurnCompactionResult
    ) -> tuple[ModelMessage, ...]:
        through = result.through_units
        summary = result.summary.strip()
        if through < 1 or through > len(projection.spans) or not summary:
            raise KaryviaError(
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

    def _record_session_compaction(
        self,
        projection: Projection,
        result: TurnCompactionResult,
    ) -> None:
        if self._session_compaction is None:
            return
        covered = tuple(
            message
            for span in projection.spans[: result.through_units]
            for message in span.unit.messages
        )
        summary = ModelMessage(
            role=Role.USER,
            content=wrap_untrusted(result.summary.strip(), source=_SUMMARY_SOURCE),
        )
        self._session_compaction.record(covered, summary)

    def _too_large(self, estimated: int) -> KaryviaError:
        return KaryviaError(
            ErrorCode.INPUT_TOO_LARGE,
            "压缩后的模型请求仍超过上下文预算。",
            detail={
                "estimated_tokens": estimated,
                "trigger_tokens": self._budget.trigger_limit,
                "target_tokens": self._budget.target_limit,
            },
            capability=self._policy.ref,
        )
