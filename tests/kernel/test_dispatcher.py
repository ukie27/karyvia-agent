"""输入分流的测试（`D13` 验收表第 2、3 行；`KER-006`、`CMD-002`、`CMD-003`）。

三条主线：

- **`CMD-002`**：命令名与别名的冲突必须在**建索引时**（启动期）就抛出来，不能留到调用期
  按加载顺序择一。
- **`CMD-003`**：命令 handler 抛任何异常，都要得到可诊断的 `REJECTED` 结果，而且**同一个
  dispatcher 紧接着还能正常分流下一条消息**——「会话保持可用」的可断言形态。
- **四态的边界**：只有以前缀开头才尝试解析；进模型的文本只在会进模型的结论上出现。

命令的执行体在 `builtins/commands_core/`（`D22`），这里用最小的假 handler：本文件测的是
分流，不是任何具体命令。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from karyvia.contracts import (
    AttachmentRef,
    AttachmentSource,
    Builtin,
    CancelSignal,
    CapabilityKind,
    CommandInvocation,
    CommandParam,
    CommandResult,
    CommandSpec,
    Correlation,
    Disposition,
    ErrorCode,
    InboundMessage,
    InstanceId,
    KaryviaError,
    Plugin,
    PluginId,
    Sender,
    SessionKey,
    TurnId,
)
from karyvia.kernel.registry import CapabilityRegistry, resolve_into
from karyvia.kernel.routing import (
    DEFAULT_COMMAND_PREFIX,
    CommandIndex,
    Dispatcher,
    RegisteredCommand,
    build_command_index,
    parse_command,
)
from karyvia.kernel.turn.cancel import CancelToken

CORRELATION = Correlation(
    instance_id=InstanceId("inst"),
    session_key=SessionKey(channel_id="cli", conversation_id="c1"),
    turn_id=TurnId("turn-1"),
)


def message(
    content: str,
    *,
    is_operator: bool = False,
    attachments: tuple[AttachmentRef, ...] = (),
) -> InboundMessage:
    return InboundMessage(
        message_id="m1",
        instance_id=InstanceId("inst"),
        channel_id="cli",
        conversation_id="c1",
        sender=Sender(user_id="u1", is_operator=is_operator),
        content=content,
        timestamp=datetime.now(UTC),
        attachments=attachments,
    )


class StubHandler:
    """按构造时给定的行为返回或抛出。记录被调用了几次。"""

    def __init__(
        self,
        result: CommandResult | None = None,
        *,
        raises: BaseException | None = None,
    ) -> None:
        self.result = result or CommandResult(
            disposition=Disposition.COMMAND_HANDLED, content="ok"
        )
        self.raises = raises
        self.calls = 0

    async def handle(self, invocation: CommandInvocation, cancel: CancelSignal) -> CommandResult:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.result


def index_of(*entries: tuple[CommandSpec, StubHandler]) -> CommandIndex:
    """把若干 (spec, handler) 塞进一个已冻结的 registry 再建索引。"""
    registry = CapabilityRegistry()
    with registry.batch(Builtin()) as batch:
        for spec, handler in entries:
            batch.add(CapabilityKind.COMMAND, spec.name, RegisteredCommand(spec, handler))
    resolve_into(registry)
    return build_command_index(registry)


def dispatcher_for(*entries: tuple[CommandSpec, StubHandler]) -> Dispatcher:
    return Dispatcher(index_of(*entries))


HELP = CommandSpec(name="help", description="显示帮助", aliases=("h",))
ECHO = CommandSpec(
    name="echo",
    description="回显",
    parameters=(CommandParam(name="text", description="要回显的文本", required=True),),
)
ADMIN = CommandSpec(name="shutdown", description="关闭实例", operator_only=True)


# --------------------------------------------------------------------------- 前缀解析


@pytest.mark.parametrize("content", ["你好", "  /help", "", "3/4", "http://x/y"])
def test_text_without_a_leading_prefix_is_not_parsed(content: str) -> None:
    """只有以前缀开头才尝试匹配——普通文本不该因为含有 `/` 就变成命令。"""
    assert parse_command(content) is None


@pytest.mark.parametrize("content", ["/", "/ help"])
def test_bare_prefix_is_not_a_command(content: str) -> None:
    assert parse_command(content) is None


def test_arguments_split_on_whitespace() -> None:
    parsed = parse_command("/echo  hello   world ")

    assert parsed is not None
    assert parsed.name == "echo"
    assert parsed.args == ("hello", "world")
    assert parsed.raw_text == "/echo  hello   world "


def test_command_name_is_case_insensitive() -> None:
    parsed = parse_command("/HELP")

    assert parsed is not None
    assert parsed.name == "help"


def test_prefix_is_configurable() -> None:
    assert parse_command("!help", "!") is not None
    assert parse_command("/help", "!") is None


# --------------------------------------------------------------------------- 索引与冲突


def test_alias_collision_is_caught_at_startup() -> None:
    """`CMD-002`：registry 的 MULTI_UNIQUE 只保证命令名唯一，别名撞车要在这里拦下。"""
    status = CommandSpec(name="status", description="状态", aliases=("st",))
    statistics = CommandSpec(name="statistics", description="统计", aliases=("st",))
    registry = CapabilityRegistry()
    with registry.batch(Builtin()) as batch:
        batch.add(
            CapabilityKind.COMMAND, status.name, RegisteredCommand(status, StubHandler())
        )
    with registry.batch(Plugin(PluginId("acme"))) as batch:
        batch.add(
            CapabilityKind.COMMAND,
            statistics.name,
            RegisteredCommand(statistics, StubHandler()),
        )
    resolve_into(registry)

    with pytest.raises(KaryviaError) as exc:
        build_command_index(registry)

    assert exc.value.code is ErrorCode.PLUGIN_REGISTRATION_CONFLICT
    assert exc.value.detail["name"] == "st"


def test_alias_colliding_with_another_command_name_is_a_conflict() -> None:
    """别名与命令名在同一个命名空间：对敲的人没有区别，判定也不该有区别。"""
    first = CommandSpec(name="help", description="帮助")
    second = CommandSpec(name="manual", description="手册", aliases=("help",))
    registry = CapabilityRegistry()
    with registry.batch(Builtin()) as batch:
        batch.add(CapabilityKind.COMMAND, first.name, RegisteredCommand(first, StubHandler()))
        batch.add(CapabilityKind.COMMAND, second.name, RegisteredCommand(second, StubHandler()))
    resolve_into(registry)

    with pytest.raises(KaryviaError) as exc:
        build_command_index(registry)

    assert exc.value.code is ErrorCode.PLUGIN_REGISTRATION_CONFLICT


def test_wrong_payload_shape_is_a_kernel_invariant_violation() -> None:
    registry = CapabilityRegistry()
    with registry.batch(Builtin()) as batch:
        batch.add(CapabilityKind.COMMAND, "help", "不是 RegisteredCommand")
    resolve_into(registry)

    with pytest.raises(KaryviaError) as exc:
        build_command_index(registry)

    assert exc.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


def test_index_lists_each_command_once_sorted_by_name() -> None:
    index = index_of((HELP, StubHandler()), (ECHO, StubHandler()))

    assert [spec.name for spec in index.specs()] == ["echo", "help"]
    assert index.names() == ("echo", "h", "help")
    assert len(index) == 2


# --------------------------------------------------------------------------- 分流四态


async def test_plain_text_becomes_a_model_turn() -> None:
    outcome = await dispatcher_for((HELP, StubHandler())).dispatch(
        message("今天天气如何"), CORRELATION, CancelToken()
    )

    assert outcome.disposition is Disposition.MODEL_TURN
    assert outcome.model_input == "今天天气如何"
    assert outcome.result is None


async def test_model_turn_keeps_the_message_attachments() -> None:
    attachment = AttachmentRef(
        source=AttachmentSource.URL,
        locator="https://files.example/photo.png",
        media_type="image/png",
    )
    outcome = await dispatcher_for((HELP, StubHandler())).dispatch(
        message("看看图片", attachments=(attachment,)), CORRELATION, CancelToken()
    )
    assert outcome.model_input == "看看图片"
    assert outcome.model_attachments == (attachment,)


async def test_matched_command_is_handled_without_entering_the_model() -> None:
    handler = StubHandler()

    outcome = await dispatcher_for((HELP, handler)).dispatch(
        message("/help"), CORRELATION, CancelToken()
    )

    assert outcome.disposition is Disposition.COMMAND_HANDLED
    assert outcome.model_input is None
    assert outcome.command_name == "help"
    assert handler.calls == 1


async def test_alias_resolves_to_the_canonical_command() -> None:
    handler = StubHandler()

    outcome = await dispatcher_for((HELP, handler)).dispatch(
        message("/h"), CORRELATION, CancelToken()
    )

    assert outcome.command_name == "help"
    assert handler.calls == 1


async def test_command_continue_carries_the_rewritten_input_into_the_model() -> None:
    handler = StubHandler(
        CommandResult(disposition=Disposition.COMMAND_CONTINUE, rewritten_input="请总结昨天的会议")
    )

    outcome = await dispatcher_for((HELP, handler)).dispatch(
        message("/help"), CORRELATION, CancelToken()
    )

    assert outcome.disposition is Disposition.COMMAND_CONTINUE
    assert outcome.model_input == "请总结昨天的会议"


async def test_command_rewrite_does_not_drop_attachments() -> None:
    attachment = AttachmentRef(
        source=AttachmentSource.WORKSPACE,
        locator="notes.txt",
        media_type="text/plain",
    )
    handler = StubHandler(
        CommandResult(disposition=Disposition.COMMAND_CONTINUE, rewritten_input="总结附件")
    )
    outcome = await dispatcher_for((HELP, handler)).dispatch(
        message("/help", attachments=(attachment,)), CORRELATION, CancelToken()
    )
    assert outcome.model_attachments == (attachment,)


async def test_unknown_command_is_rejected_with_a_suggestion() -> None:
    outcome = await dispatcher_for((HELP, StubHandler())).dispatch(
        message("/hepl"), CORRELATION, CancelToken()
    )

    assert outcome.disposition is Disposition.REJECTED
    assert outcome.error is not None
    assert outcome.error.code is ErrorCode.CAPABILITY_MISSING
    assert outcome.error.detail["suggestion"] == "help"


async def test_unknown_command_without_a_close_match_points_at_help() -> None:
    outcome = await dispatcher_for((HELP, StubHandler())).dispatch(
        message("/zzzzz"), CORRELATION, CancelToken()
    )

    assert outcome.error is not None
    assert outcome.error.detail["suggestion"] is None
    assert "/help" in outcome.error.user_message


# --------------------------------------------------------------------------- 前置校验


async def test_operator_only_command_is_refused_for_ordinary_senders() -> None:
    handler = StubHandler()

    outcome = await dispatcher_for((ADMIN, handler)).dispatch(
        message("/shutdown"), CORRELATION, CancelToken()
    )

    assert outcome.disposition is Disposition.REJECTED
    assert outcome.error is not None
    assert outcome.error.code is ErrorCode.PERMISSION_DENIED
    assert handler.calls == 0  # 权限判定在调用之前


async def test_operator_only_command_runs_for_the_operator() -> None:
    handler = StubHandler()

    outcome = await dispatcher_for((ADMIN, handler)).dispatch(
        message("/shutdown", is_operator=True), CORRELATION, CancelToken()
    )

    assert outcome.disposition is Disposition.COMMAND_HANDLED
    assert handler.calls == 1


async def test_missing_required_argument_is_rejected_with_usage() -> None:
    handler = StubHandler()

    outcome = await dispatcher_for((ECHO, handler)).dispatch(
        message("/echo"), CORRELATION, CancelToken()
    )

    assert outcome.error is not None
    assert outcome.error.code is ErrorCode.INPUT_MALFORMED
    assert "/echo <text>" in outcome.error.user_message
    assert handler.calls == 0


async def test_too_many_arguments_are_rejected_rather_than_silently_dropped() -> None:
    outcome = await dispatcher_for((ECHO, StubHandler())).dispatch(
        message("/echo a b"), CORRELATION, CancelToken()
    )

    assert outcome.error is not None
    assert outcome.error.code is ErrorCode.INPUT_MALFORMED


async def test_repeated_parameter_absorbs_the_remaining_arguments() -> None:
    spec = CommandSpec(
        name="say",
        description="说话",
        parameters=(CommandParam(name="words", description="内容", required=True, repeated=True),),
    )
    handler = StubHandler()

    outcome = await dispatcher_for((spec, handler)).dispatch(
        message("/say a b c d"), CORRELATION, CancelToken()
    )

    assert outcome.disposition is Disposition.COMMAND_HANDLED
    assert handler.calls == 1


# --------------------------------------------------------------------------- CMD-003


async def test_handler_exception_becomes_a_diagnosable_rejection() -> None:
    """`CMD-003`：命令处理失败必须返回可诊断错误，不得让异常逸出。"""
    dispatcher = dispatcher_for((HELP, StubHandler(raises=RuntimeError("内部炸了"))))

    outcome = await dispatcher.dispatch(message("/help"), CORRELATION, CancelToken())

    assert outcome.disposition is Disposition.REJECTED
    assert outcome.error is not None
    assert outcome.error.code is ErrorCode.KERNEL_UNEXPECTED
    assert outcome.error.detail["exception"] == "RuntimeError"


async def test_handler_exception_message_is_not_echoed_to_the_user() -> None:
    """第三方命令的异常文本可能带凭据或路径，只保留类型名。"""
    dispatcher = dispatcher_for((HELP, StubHandler(raises=RuntimeError("token=sk-abc123"))))

    outcome = await dispatcher.dispatch(message("/help"), CORRELATION, CancelToken())

    assert outcome.error is not None
    assert "sk-abc123" not in outcome.error.user_message
    assert "sk-abc123" not in str(outcome.error.detail)


async def test_the_session_stays_usable_after_a_command_blows_up() -> None:
    """「会话仍可用、进程不退出」的可断言形态：同一个 dispatcher 紧接着还能正常工作。"""
    boom = StubHandler(raises=RuntimeError("boom"))
    fine = StubHandler()
    dispatcher = dispatcher_for((HELP, boom), (ECHO, fine))

    failed = await dispatcher.dispatch(message("/help"), CORRELATION, CancelToken())
    after_command = await dispatcher.dispatch(message("/echo hi"), CORRELATION, CancelToken())
    after_text = await dispatcher.dispatch(message("普通消息"), CORRELATION, CancelToken())

    assert failed.disposition is Disposition.REJECTED
    assert after_command.disposition is Disposition.COMMAND_HANDLED
    assert after_text.disposition is Disposition.MODEL_TURN


async def test_a_karyvia_error_from_the_handler_is_passed_through_intact() -> None:
    """实现方给出的诊断信息比 Kernel 能编的更准，原样带上。"""
    raised = KaryviaError(ErrorCode.PERMISSION_DENIED, "你没有权限读那个目录。")
    dispatcher = dispatcher_for((HELP, StubHandler(raises=raised)))

    outcome = await dispatcher.dispatch(message("/help"), CORRELATION, CancelToken())

    assert outcome.error is raised


async def test_base_exception_is_not_swallowed() -> None:
    """取消与 Ctrl-C 是进程级信号，吞掉它会让停机需要按两次。"""
    dispatcher = dispatcher_for((HELP, StubHandler(raises=KeyboardInterrupt())))

    with pytest.raises(KeyboardInterrupt):
        await dispatcher.dispatch(message("/help"), CORRELATION, CancelToken())


# --------------------------------------------------------------------------- 结论不变量


def test_empty_prefix_is_rejected() -> None:
    """空前缀会让每条普通消息都被当成命令解析。"""
    for prefix in ("", " ", "/ "):
        with pytest.raises(KaryviaError) as exc:
            Dispatcher(index_of((HELP, StubHandler())), prefix=prefix)
        assert exc.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


def test_default_prefix_matches_the_documented_value() -> None:
    assert DEFAULT_COMMAND_PREFIX == "/"
