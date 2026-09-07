"""命令契约测试。

`CommandResult` 的四条一致性规则各有正反用例：分流结论与载荷对不上的结果一旦放行，
错就会出现在「命令没反应」这种最难查的地方。
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from karyvia.contracts import (
    CommandInvocation,
    CommandParam,
    CommandResult,
    CommandSpec,
    ContextFragment,
    Correlation,
    Disposition,
    ErrorCode,
    FragmentKind,
    FragmentScope,
    InboundMessage,
    InstanceId,
    KaryviaError,
    Sender,
    SessionKey,
    TrustLevel,
    TurnId,
)

CORRELATION = Correlation(InstanceId("default"), SessionKey("cli", "local"), TurnId("t-1"))
MESSAGE = InboundMessage(
    message_id="m-1",
    instance_id=InstanceId("default"),
    channel_id="cli",
    conversation_id="local",
    sender=Sender("u-1"),
    content="/help session",
    timestamp=datetime(2026, 8, 11, tzinfo=UTC),
)
FRAGMENT = ContextFragment(
    source="builtin:commands_core",
    kind=FragmentKind.SKILL,
    content="片段内容",
    priority=10,
    estimated_tokens=4,
    scope=FragmentScope.SESSION,
    trust=TrustLevel.OPERATOR,
)


def param(**overrides: object) -> CommandParam:
    base: dict[str, object] = {"name": "topic", "description": "要查看的主题。"}
    base.update(overrides)
    return CommandParam(**base)  # pyright: ignore[reportArgumentType]


def spec(**overrides: object) -> CommandSpec:
    base: dict[str, object] = {"name": "help", "description": "列出可用命令。"}
    base.update(overrides)
    return CommandSpec(**base)  # pyright: ignore[reportArgumentType]


# ------------------------------------------------------------------------ 声明


def test_spec_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec().name = "x"


def test_spec_declares_names_parameters_and_operator_requirement() -> None:
    command = spec(
        parameters=(param(required=True),),
        operator_only=True,
        aliases=("h", "man"),
    )
    assert command.all_names == ("help", "h", "man")
    assert command.operator_only is True


@pytest.mark.parametrize("name", ["/help", "Help", "help_me", "-help", "1help", ""])
def test_command_name_shape(name: str) -> None:
    """前缀是路由的配置项，不是命令身份的一部分——名字里不许出现它。"""
    with pytest.raises(KaryviaError):
        spec(name=name)


def test_alias_must_not_repeat_the_name() -> None:
    with pytest.raises(KaryviaError) as excinfo:
        spec(aliases=("help",))
    assert excinfo.value.code is ErrorCode.INPUT_MALFORMED


def test_description_is_required() -> None:
    with pytest.raises(KaryviaError):
        spec(description="")
    with pytest.raises(KaryviaError):
        param(description="")


def test_required_parameter_may_not_follow_an_optional_one() -> None:
    with pytest.raises(KaryviaError) as excinfo:
        spec(parameters=(param(name="a"), param(name="b", required=True)))
    assert "必填参数" in excinfo.value.user_message


def test_repeated_parameter_must_be_last() -> None:
    with pytest.raises(KaryviaError):
        spec(parameters=(param(name="a", repeated=True), param(name="b")))
    assert spec(parameters=(param(name="a"), param(name="b", repeated=True))).parameters[1].repeated


def test_parameter_names_are_unique() -> None:
    with pytest.raises(KaryviaError):
        spec(parameters=(param(name="a"), param(name="a")))


# ------------------------------------------------------------------------ 调用


def test_invocation_keeps_the_original_message_and_correlation() -> None:
    """handler 靠 `message.sender.is_operator` 判权限；`correlation` 让命令也可观测。"""
    invocation = CommandInvocation("help", ("session",), "/help session", MESSAGE, CORRELATION)
    assert invocation.message.sender.is_operator is False
    assert invocation.correlation.turn_id == "t-1"


def test_invocation_rejects_a_prefixed_name() -> None:
    with pytest.raises(KaryviaError):
        CommandInvocation("/help", (), "/help", MESSAGE, CORRELATION)


# ------------------------------------------------------------------------ 结果


def test_disposition_has_the_four_values_from_the_design() -> None:
    assert [d.value for d in Disposition] == [
        "command_handled",
        "command_continue",
        "model_turn",
        "rejected",
    ]


def test_handler_may_not_return_model_turn() -> None:
    """`MODEL_TURN` 是「未命中」，那是 dispatcher 的结论，不是 handler 的。"""
    with pytest.raises(KaryviaError) as excinfo:
        CommandResult(Disposition.MODEL_TURN, content="x")
    assert excinfo.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


def test_rejected_requires_an_error_and_others_forbid_it() -> None:
    error = KaryviaError(ErrorCode.INPUT_MALFORMED, "参数不对。")
    assert CommandResult(Disposition.REJECTED, error=error).error is error
    with pytest.raises(KaryviaError):
        CommandResult(Disposition.REJECTED, content="x")
    with pytest.raises(KaryviaError):
        CommandResult(Disposition.COMMAND_HANDLED, content="x", error=error)


def test_continue_requires_rewritten_input_and_others_forbid_it() -> None:
    assert CommandResult(Disposition.COMMAND_CONTINUE, rewritten_input="改写后").content == ""
    with pytest.raises(KaryviaError):
        CommandResult(Disposition.COMMAND_CONTINUE)
    with pytest.raises(KaryviaError):
        CommandResult(Disposition.COMMAND_HANDLED, content="x", rewritten_input="改写后")


def test_handled_must_produce_output_or_fragments() -> None:
    with pytest.raises(KaryviaError) as excinfo:
        CommandResult(Disposition.COMMAND_HANDLED)
    assert "毫无反馈" in excinfo.value.user_message
    assert CommandResult(Disposition.COMMAND_HANDLED, fragments=(FRAGMENT,)).content == ""


def test_metadata_is_normalized_and_snapshotted() -> None:
    """快照语义：调用方事后改自己那份 dict 影响不到已构造的结果。"""
    payload: dict[str, object] = {"source": "builtin"}
    result = CommandResult(Disposition.COMMAND_HANDLED, content="x", metadata=payload)
    payload["source"] = "changed"
    assert result.metadata["source"] == "builtin"
    with pytest.raises(TypeError):
        result.metadata["source"] = "x"  # type: ignore[index]


def test_result_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        CommandResult(Disposition.COMMAND_HANDLED, content="x").content = "y"
