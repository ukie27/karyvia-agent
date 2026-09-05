"""最终模型请求的 token 估算与实际 usage 锚点。"""

from __future__ import annotations

from karyvia.contracts import (
    ModelMessage,
    ModelRequest,
    RiskLevel,
    Role,
    SamplingParams,
    SessionKey,
    TokenUsage,
    ToolSpec,
)
from karyvia.kernel.turn.request_size import (
    TokenAccounting,
    estimate_request_tokens,
)
from karyvia.sdk.testing import make_correlation


def _request(
    *messages: ModelMessage,
    session: str = "one",
    tools: tuple[ToolSpec, ...] = (),
    params: SamplingParams = SamplingParams(),
) -> ModelRequest:
    return ModelRequest(
        model_id="fake-model",
        messages=messages,
        correlation=make_correlation(
            session_key=SessionKey("cli", session),
            turn_id=f"turn-{session}",
        ),
        tools=tools,
        params=params,
    )


def _tool() -> ToolSpec:
    return ToolSpec(
        name="test.echo",
        description="echo text",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}},
        read_only=True,
        risk=RiskLevel.SAFE,
    )


def test_actual_usage_anchors_matching_request_prefix() -> None:
    first = _request(ModelMessage(Role.SYSTEM, "rules"), ModelMessage(Role.USER, "hello"))
    suffix = ModelMessage(Role.ASSISTANT, "answer")
    following = _request(*first.messages, suffix)
    accounting = TokenAccounting()

    accounting.observe(first, TokenUsage(input_tokens=7))

    assert accounting.estimate_request(first) == 7
    assert accounting.estimate_request(following) == 7 + accounting.estimate_messages((suffix,))


def test_changed_prefix_invalidates_actual_usage_anchor() -> None:
    first = _request(ModelMessage(Role.SYSTEM, "rules"), ModelMessage(Role.USER, "hello"))
    changed = _request(ModelMessage(Role.SYSTEM, "new rules"), *first.messages[1:])
    accounting = TokenAccounting()
    accounting.observe(first, TokenUsage(input_tokens=7))

    assert accounting.estimate_request(changed) == estimate_request_tokens(changed)


def test_changed_request_shape_invalidates_actual_usage_anchor() -> None:
    first = _request(ModelMessage(Role.USER, "hello"))
    changed = _request(*first.messages, tools=(_tool(),))
    accounting = TokenAccounting()
    accounting.observe(first, TokenUsage(input_tokens=7))

    assert accounting.estimate_request(changed) == estimate_request_tokens(changed)


def test_changed_sampling_params_invalidate_actual_usage_anchor() -> None:
    first = _request(ModelMessage(Role.USER, "hello"))
    changed = _request(*first.messages, params=SamplingParams(temperature=0.5))
    accounting = TokenAccounting()
    accounting.observe(first, TokenUsage(input_tokens=7))

    assert accounting.estimate_request(changed) == estimate_request_tokens(changed)


def test_actual_usage_anchors_are_isolated_by_session() -> None:
    first = _request(ModelMessage(Role.USER, "hello"), session="one")
    other = _request(*first.messages, ModelMessage(Role.ASSISTANT, "answer"), session="two")
    accounting = TokenAccounting()
    accounting.observe(first, TokenUsage(input_tokens=7))

    assert accounting.estimate_request(other) == estimate_request_tokens(other)


def test_missing_usage_keeps_previous_matching_anchor() -> None:
    first = _request(ModelMessage(Role.USER, "hello"))
    following = _request(*first.messages, ModelMessage(Role.ASSISTANT, "answer"))
    accounting = TokenAccounting()
    accounting.observe(first, TokenUsage(input_tokens=7))

    accounting.observe(following, TokenUsage())

    assert accounting.estimate_request(following) == 7 + accounting.estimate_messages(
        following.messages[len(first.messages) :]
    )
