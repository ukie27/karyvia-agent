"""Turn 内上下文压缩机制的单元验收。"""

from __future__ import annotations

import asyncio

import pytest

from nucleamind.builtins.context_compact_basic import BasicTurnContextCompactor
from nucleamind.contracts import (
    Builtin,
    CancelSignal,
    ChunkKind,
    CompactionModel,
    ErrorCode,
    EventName,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    NucleaError,
    RiskLevel,
    Role,
    StopReason,
    TokenUsage,
    ToolCall,
    ToolSpec,
    TurnCompactionRequest,
    TurnCompactionResult,
    TurnContextUnitKind,
)
from nucleamind.kernel.observability import EventBus, MemoryRingSink
from nucleamind.kernel.turn import BudgetLedger, TokenAccounting, TurnLimits
from nucleamind.kernel.turn.request_size import estimate_request_tokens
from nucleamind.kernel.turn.turn_compaction import (
    TurnCompactingModel,
    TurnCompactionPolicy,
    project_units,
)
from nucleamind.sdk.testing import (
    FakeModelProvider,
    ManualCancel,
    StaticTurnContextCompactor,
    make_correlation,
    text_response,
)


def _request(*messages: ModelMessage, tools: tuple[ToolSpec, ...] = ()) -> ModelRequest:
    return ModelRequest(
        model_id="fake-model",
        messages=messages,
        correlation=make_correlation(),
        tools=tools,
        timeout_ms=10_000,
    )


def _model(
    request: ModelRequest,
    provider: FakeModelProvider,
    compactor: object,
    *,
    budget: int,
    timeout_ms: int = 120_000,
) -> TurnCompactingModel:
    limits = TurnLimits(context_max_tokens=budget)
    accounting = TokenAccounting()
    return TurnCompactingModel(
        provider,
        TurnCompactionPolicy(compactor, "test", Builtin(), timeout_ms),  # type: ignore[arg-type]
        EventBus(request.correlation.instance_id),
        BudgetLedger(limits),
        budget=limits.resolve_context_budget(),
        accounting=accounting,
        protected_user=next(m for m in reversed(request.messages) if m.role is Role.USER),
        model_info=provider.describe(request.model_id),
    )


def test_tool_calls_and_results_form_one_indivisible_unit() -> None:
    user = ModelMessage(Role.USER, "执行")
    calls = (
        ToolCall("call-1", "test.echo", {"text": "a"}),
        ToolCall("call-2", "test.echo", {"text": "b"}),
    )
    messages = (
        ModelMessage(Role.SYSTEM, "system"),
        ModelMessage(Role.ASSISTANT, "history"),
        user,
        ModelMessage(Role.ASSISTANT, tool_calls=calls),
        ModelMessage(Role.TOOL, "A", tool_call_id="call-1"),
        ModelMessage(Role.TOOL, "B", tool_call_id="call-2"),
    )

    units = project_units(messages, user)

    assert [unit.kind for unit in units] == [
        TurnContextUnitKind.BASE,
        TurnContextUnitKind.TOOL_EXCHANGE,
    ]
    assert units[1].messages == messages[3:]


def test_full_request_estimate_includes_tools_and_arguments() -> None:
    user = ModelMessage(Role.USER, "go")
    plain = _request(user)
    call = ToolCall("call-1", "test.echo", {"text": "x" * 300})
    with_call = _request(user, ModelMessage(Role.ASSISTANT, tool_calls=(call,)))
    spec = ToolSpec(
        name="test.echo",
        description="d" * 300,
        parameters={"type": "object", "properties": {"text": {"type": "string"}}},
        read_only=True,
        risk=RiskLevel.SAFE,
    )
    with_tool = _request(user, tools=(spec,))

    assert estimate_request_tokens(with_call) > estimate_request_tokens(plain)
    assert estimate_request_tokens(with_tool) > estimate_request_tokens(plain)


async def test_large_tool_result_is_compacted_before_the_provider_call() -> None:
    user = ModelMessage(Role.USER, "执行")
    call = ToolCall("call-1", "test.echo", {"text": "x"})
    request = _request(
        ModelMessage(Role.SYSTEM, "system"),
        user,
        ModelMessage(Role.ASSISTANT, tool_calls=(call,)),
        ModelMessage(Role.TOOL, "result" * 300, tool_call_id="call-1"),
    )
    provider = FakeModelProvider([text_response("done")])
    model = _model(request, provider, BasicTurnContextCompactor(), budget=120)

    response = await model.complete(request, ManualCancel())

    assert response.content == "done"
    sent = provider.requests[0]
    assert estimate_request_tokens(sent) <= 120
    assert all("result" * 20 not in message.content for message in sent.messages)
    assert any("untrusted-data" in message.content for message in sent.messages)


async def test_previous_summary_is_carried_into_the_next_iteration() -> None:
    user = ModelMessage(Role.USER, "执行")
    first_call = ToolCall("call-1", "test.echo", {"text": "x"})
    first = _request(
        user,
        ModelMessage(Role.ASSISTANT, tool_calls=(first_call,)),
        ModelMessage(Role.TOOL, "old" * 300, tool_call_id="call-1"),
    )
    provider = FakeModelProvider([text_response("one"), text_response("two")])
    model = _model(first, provider, BasicTurnContextCompactor(), budget=100)
    await model.complete(first, ManualCancel())

    second_call = ToolCall("call-2", "test.echo", {"text": "y"})
    second = ModelRequest(
        model_id=first.model_id,
        messages=(
            *first.messages,
            ModelMessage(Role.ASSISTANT, tool_calls=(second_call,)),
            ModelMessage(Role.TOOL, "new" * 300, tool_call_id="call-2"),
        ),
        correlation=first.correlation,
        timeout_ms=first.timeout_ms,
    )
    await model.complete(second, ManualCancel())

    latest = provider.requests[-1]
    assert estimate_request_tokens(latest) <= 100
    assert all("old" * 20 not in message.content for message in latest.messages)
    assert all("new" * 20 not in message.content for message in latest.messages)


class _ModelUsingCompactor:
    def __init__(self) -> None:
        self.model_id = ""

    async def compact(
        self,
        request: TurnCompactionRequest,
        model: CompactionModel,
        cancel: CancelSignal,
    ) -> TurnCompactionResult:
        self.model_id = model.info.model_id
        response = await model.complete(
            (ModelMessage(Role.USER, "请摘要"),),
            cancel,
            max_output_tokens=999_999,
        )
        return TurnCompactionResult(len(request.units), response.content)


async def test_compactor_can_use_the_current_model_without_recursing() -> None:
    user = ModelMessage(Role.USER, "继续")
    request = _request(ModelMessage(Role.ASSISTANT, "history" * 300), user)
    provider = FakeModelProvider([text_response("短摘要"), text_response("answer")])
    compactor = _ModelUsingCompactor()
    bus = EventBus(request.correlation.instance_id)
    ring = MemoryRingSink()
    bus.subscribe(ring)
    limits = TurnLimits(context_max_tokens=80)
    model = TurnCompactingModel(
        provider,
        TurnCompactionPolicy(compactor, "model", Builtin()),
        bus,
        BudgetLedger(limits),
        budget=limits.resolve_context_budget(),
        accounting=TokenAccounting(),
        protected_user=user,
        model_info=provider.describe(request.model_id),
    )

    response = await model.complete(request, ManualCancel())

    assert response.content == "answer"
    assert compactor.model_id == request.model_id
    assert len(provider.requests) == 2
    helper = provider.requests[0]
    assert helper.tools == () and helper.stream is False
    assert helper.correlation == request.correlation
    assert helper.params.max_output_tokens == provider.describe(request.model_id).max_output_tokens
    purposes = [
        event.payload.get("purpose")
        for event in ring.events()
        if event.name is EventName.MODEL_REQUEST_STARTED
    ]
    assert purposes == ["turn_compaction"]


class _BrokenCompactor:
    async def compact(self, request: object, model: object, cancel: object) -> object:
        raise RuntimeError("secret prompt content")


async def test_bare_plugin_exception_is_sanitized_without_fallback() -> None:
    user = ModelMessage(Role.USER, "继续")
    request = _request(ModelMessage(Role.ASSISTANT, "history" * 300), user)
    provider = FakeModelProvider([text_response("must not run")])
    model = _model(request, provider, _BrokenCompactor(), budget=80)

    with pytest.raises(NucleaError) as excinfo:
        await model.complete(request, ManualCancel())

    assert excinfo.value.code is ErrorCode.PLUGIN_TURN_COMPACTION_FAILED
    assert excinfo.value.detail["exception"] == "RuntimeError"
    assert "secret prompt content" not in str(excinfo.value)
    assert provider.requests == []


class _SlowCompactor:
    async def compact(self, request: object, model: object, cancel: object) -> object:
        await asyncio.Event().wait()


async def test_compactor_timeout_fails_once_without_fallback() -> None:
    user = ModelMessage(Role.USER, "继续")
    request = _request(ModelMessage(Role.ASSISTANT, "history" * 300), user)
    provider = FakeModelProvider([text_response("must not run")])
    model = _model(request, provider, _SlowCompactor(), budget=80, timeout_ms=1)

    with pytest.raises(NucleaError) as excinfo:
        await model.complete(request, ManualCancel())

    assert excinfo.value.code is ErrorCode.TIMEOUT_TURN_COMPACTION
    assert provider.requests == []


async def test_actual_usage_calibrates_later_estimates_upward() -> None:
    user = ModelMessage(Role.USER, "短请求")
    request = _request(user)
    response = ModelResponse(
        request.model_id,
        StopReason.END_TURN,
        content="answer",
        usage=TokenUsage(input_tokens=200, output_tokens=1),
    )
    provider = FakeModelProvider([response])
    limits = TurnLimits(context_max_tokens=1_000)
    accounting = TokenAccounting()
    model = TurnCompactingModel(
        provider,
        TurnCompactionPolicy(BasicTurnContextCompactor(), "basic", Builtin()),
        EventBus(request.correlation.instance_id),
        BudgetLedger(limits),
        budget=limits.resolve_context_budget(),
        accounting=accounting,
        protected_user=user,
        model_info=provider.describe(request.model_id),
    )

    await model.complete(request, ManualCancel())

    assert accounting.correction_factor > 1
    assert accounting.estimate_request(request) >= 200


class _OverflowOnceProvider(FakeModelProvider):
    def __init__(self) -> None:
        super().__init__([text_response("recovered")])
        self.overflowed = False

    async def complete(self, request, cancel):  # noqa: ANN001, ANN202
        if not self.overflowed:
            self.overflowed = True
            self.requests.append(request)
            raise NucleaError(
                ErrorCode.EXTERNAL_MODEL_CONTEXT_OVERFLOW,
                "模型请求超过供应商窗口。",
            )
        return await super().complete(request, cancel)

    def stream(self, request, cancel):  # noqa: ANN001, ANN201
        return self._overflow_stream(request, cancel)

    async def _overflow_stream(self, request, cancel):  # noqa: ANN001, ANN202
        if not self.overflowed:
            self.overflowed = True
            self.requests.append(request)
            raise NucleaError(
                ErrorCode.EXTERNAL_MODEL_CONTEXT_OVERFLOW,
                "模型请求超过供应商窗口。",
            )
        async for chunk in super().stream(request, cancel):
            yield chunk


async def test_provider_context_overflow_forces_one_compaction_and_retry() -> None:
    user = ModelMessage(Role.USER, "继续")
    request = _request(ModelMessage(Role.ASSISTANT, "history" * 30), user)
    provider = _OverflowOnceProvider()
    model = _model(request, provider, StaticTurnContextCompactor(), budget=1_000)

    response = await model.complete(request, ManualCancel())

    assert response.content == "recovered"
    assert len(provider.requests) == 2
    assert sum(len(message.content) for message in provider.requests[1].messages) < sum(
        len(message.content) for message in provider.requests[0].messages
    )


async def test_stream_context_overflow_recovers_before_substantive_output() -> None:
    user = ModelMessage(Role.USER, "继续")
    request = _request(ModelMessage(Role.ASSISTANT, "history" * 30), user)
    provider = _OverflowOnceProvider()
    model = _model(request, provider, StaticTurnContextCompactor(), budget=1_000)

    chunks = [chunk async for chunk in model.stream(request, ManualCancel())]

    assert any(chunk.kind is ChunkKind.TEXT and chunk.text == "recovered" for chunk in chunks)
    assert len(provider.requests) == 2
