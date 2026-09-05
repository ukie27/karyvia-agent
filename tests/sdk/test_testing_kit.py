"""`sdk/testing/` 自测：Fake 符合契约，5 个契约基类真的会跑（`D05`、`NFR-702`）。

契约基类如果只是空壳，继承它的实现会「全绿地」通过一组什么都没断言的用例——那比没有
契约测试更危险。这里做两件事：用 Fake 把 5 个基类各跑一遍（证明骨架可用），再喂一个
**故意违约**的实现（证明骨架会拦）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from nucleamind.contracts import (
    CancelSignal,
    Channel,
    ContextFragment,
    ContextProvider,
    Correlation,
    ErrorCode,
    FragmentKind,
    FragmentScope,
    HookAction,
    HookContext,
    HookName,
    HookOutcome,
    InboundMessage,
    InstanceId,
    JsonValue,
    ModelCapability,
    ModelChunk,
    ModelInfo,
    ModelMessage,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    NucleaError,
    OutboundMessage,
    RiskLevel,
    Role,
    Sender,
    SessionSnapshot,
    SessionStore,
    SideEffect,
    StopReason,
    ToolCall,
    ToolHandler,
    ToolInvocation,
    ToolResult,
    ToolSpec,
    TrustLevel,
    TurnOutcome,
    TurnStatus,
)
from nucleamind.sdk.testing import (
    FAKE_MODEL_ID,
    ChannelContract,
    ContextProviderContract,
    FakeModelProvider,
    InMemorySessionStore,
    ManualCancel,
    ModelProviderContract,
    RecordingHook,
    SessionStoreContract,
    ToolContract,
    make_correlation,
    text_response,
    tool_call_response,
)

NOW = datetime(2026, 8, 11, tzinfo=UTC)


def _request() -> ModelRequest:
    return ModelRequest(
        model_id=FAKE_MODEL_ID,
        messages=(ModelMessage(role=Role.USER, content="ping"),),
        correlation=make_correlation(),
    )


def _inbound() -> InboundMessage:
    return InboundMessage(
        message_id="m1",
        instance_id=InstanceId("test"),
        channel_id="cli",
        conversation_id="local",
        sender=Sender(user_id="u1"),
        content="hi",
        timestamp=NOW,
    )


def _turn_end_context(correlation: Correlation) -> HookContext:
    return HookContext(
        hook=HookName.TURN_END,
        correlation=correlation,
        outcome=TurnOutcome(
            correlation=correlation,
            status=TurnStatus.COMPLETED,
            started_at=NOW,
            finished_at=NOW,
        ),
    )


# --------------------------------------------------------------- Fake 满足对应 Protocol


def test_fakes_satisfy_their_protocols() -> None:
    """结构化子类型：Fake 不继承任何宿主基类（`PLG-002`）。"""
    assert isinstance(FakeModelProvider(), ModelProvider)
    assert isinstance(InMemorySessionStore(), SessionStore)
    assert isinstance(ManualCancel(), CancelSignal)
    assert not isinstance(object(), ModelProvider)


def test_manual_cancel_is_a_switch_not_a_token() -> None:
    cancel = ManualCancel()
    assert cancel.requested is False
    cancel.raise_if_requested()

    cancel.request()
    cancel.request()  # 幂等
    assert cancel.requested is True
    with pytest.raises(NucleaError) as excinfo:
        cancel.raise_if_requested()
    assert excinfo.value.code is ErrorCode.CANCELLED_BY_USER


async def test_fake_model_provider_replays_its_script_in_order() -> None:
    call = ToolCall(call_id="c1", name="fs.read", arguments={"path": "a.txt"})
    provider = FakeModelProvider([tool_call_response(call), text_response("done")])

    first = await provider.complete(_request(), ManualCancel())
    second = await provider.complete(_request(), ManualCancel())

    assert first.stop_reason is StopReason.TOOL_CALLS
    assert first.tool_calls[0].call_id == "c1"
    assert second.content == "done"
    assert len(provider.requests) == 2, "收到的请求要能被断言，否则测不了送进模型的是什么"


async def test_fake_model_provider_derives_stream_chunks_from_the_same_script() -> None:
    call = ToolCall(call_id="c1", name="fs.read", arguments={})
    provider = FakeModelProvider([tool_call_response(call)])
    chunks = [chunk async for chunk in provider.stream(_request(), ManualCancel())]
    kinds = [chunk.kind.value for chunk in chunks]
    assert kinds == ["tool_call", "usage", "done"]
    assert chunks[-1].stop_reason is StopReason.TOOL_CALLS


async def test_fake_model_provider_reports_an_exhausted_script() -> None:
    """脚本用尽是测试代码的错，不是模型的错——必须响亮地失败而不是返回空响应。"""
    with pytest.raises(NucleaError) as excinfo:
        await FakeModelProvider().complete(_request(), ManualCancel())
    assert excinfo.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


async def test_recording_hook_records_order_and_outcome() -> None:
    correlation = make_correlation()
    hook = RecordingHook()
    await hook.handle(HookContext(HookName.TURN_START, correlation, message=_inbound()))
    await hook.handle(_turn_end_context(correlation))

    assert hook.hooks_seen() == ("turn_start", "turn_end")
    assert hook.call_count == 2

    outcome = HookOutcome(action=HookAction.CONTINUE)
    configured = RecordingHook(outcome)
    returned = await configured.handle(_turn_end_context(correlation))
    assert returned is outcome


# ------------------------------------------------------------------ 5 个契约基类跑起来


class TestFakeModelProviderContract(ModelProviderContract):
    def make_provider(self) -> ModelProvider:
        # 契约基类每个用例都重新构造 provider，因此三条脚本足够。
        return FakeModelProvider([text_response("ok") for _ in range(3)])


class TestNonStreamingProviderContract(ModelProviderContract):
    """未声明流式的 provider 必须报缺失而不是降级——同一套契约覆盖两种声明（`MOD-005`）。"""

    def make_provider(self) -> ModelProvider:
        return FakeModelProvider(
            [text_response("ok") for _ in range(3)],
            capabilities=frozenset({ModelCapability.TOOL_CALLS}),
        )


class TestInMemorySessionStoreContract(SessionStoreContract):
    def make_store(self) -> SessionStore:
        return InMemorySessionStore()


class _StaticContextProvider:
    """最小合规 `ContextProvider`：永远贡献同一段系统指令。"""

    async def provide(
        self, snapshot: SessionSnapshot, correlation: Correlation, cancel: CancelSignal
    ) -> tuple[ContextFragment, ...]:
        return (
            ContextFragment(
                source="builtin:test",
                kind=FragmentKind.SYSTEM,
                content="你是一个测试助手。",
                priority=0,
                estimated_tokens=8,
                scope=FragmentScope.SESSION,
                trust=TrustLevel.SYSTEM,
            ),
        )


class TestStaticContextProviderContract(ContextProviderContract):
    def make_provider(self) -> ContextProvider:
        return _StaticContextProvider()


ECHO_SPEC = ToolSpec(
    name="test.echo",
    description="原样返回 text 参数。",
    parameters={"type": "object", "properties": {"text": {"type": "string"}}},
    read_only=True,
    risk=RiskLevel.SAFE,
)


class _EchoTool:
    """最小合规 `ToolHandler`：坏参数返回失败结果而不是抛异常。"""

    async def execute(self, invocation: ToolInvocation, cancel: CancelSignal) -> ToolResult:
        text = invocation.call.arguments.get("text")
        if not isinstance(text, str):
            return ToolResult(
                call_id=invocation.call.call_id,
                ok=False,
                content="text 必须是字符串。",
                truncated=False,
                side_effect=SideEffect.NONE,
                error=NucleaError(ErrorCode.INPUT_MALFORMED, "text 必须是字符串。"),
            )
        return ToolResult(
            call_id=invocation.call.call_id,
            ok=True,
            content=text,
            truncated=False,
            side_effect=SideEffect.NONE,
        )


class TestEchoToolContract(ToolContract):
    def make_tool(self) -> tuple[ToolSpec, ToolHandler]:
        return ECHO_SPEC, _EchoTool()

    def valid_arguments(self) -> dict[str, JsonValue]:
        return {"text": "hello"}

    def invalid_arguments(self) -> dict[str, JsonValue] | None:
        return {"text": 42}


class _NullChannel:
    """最小合规 `Channel`：不收不发，但 `stop()` 可重复调用。"""

    @property
    def channel_id(self) -> str:
        return "fake"

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def receive(self) -> AsyncIterator[InboundMessage]:
        return self._receive()

    async def _receive(self) -> AsyncIterator[InboundMessage]:
        return
        yield  # pragma: no cover - 让本方法成为异步生成器

    async def deliver(self, message: OutboundMessage) -> None:
        return None


class TestNullChannelContract(ChannelContract):
    def make_channel(self) -> Channel:
        return _NullChannel()


# --------------------------------------------------------------- 契约基类必须会「拦」


class _IgnoresCancellation:
    """违约实现：已请求取消仍照常返回（`ModelProvider.complete` 要求抛 `CANCELLED`）。"""

    def describe(self, model_id: str) -> ModelInfo:
        return ModelInfo(model_id=model_id, provider="bad", context_window_tokens=1024)

    async def complete(self, request: ModelRequest, cancel: CancelSignal) -> ModelResponse:
        return ModelResponse(request.model_id, StopReason.END_TURN, content="ignored cancel")

    def stream(self, request: ModelRequest, cancel: CancelSignal) -> AsyncIterator[ModelChunk]:
        raise NotImplementedError


class _CancellationSuite(ModelProviderContract):
    """本类**不以 Test 开头**，因此不会被 pytest 收集——它只被下面那条用例手动调用。"""

    def make_provider(self) -> ModelProvider:
        return _IgnoresCancellation()


async def test_contract_rejects_a_provider_that_ignores_cancellation() -> None:
    """证明骨架不是空壳：违约实现必须被现有用例拦下。"""
    with pytest.raises(AssertionError):
        await _CancellationSuite().test_complete_honours_an_already_requested_cancellation()


def test_contract_rejects_a_missing_fixture() -> None:
    """没提供夹具时给出可读的失败，而不是 `AttributeError`。"""
    with pytest.raises(NotImplementedError, match="make_store"):
        SessionStoreContract().make_store()
