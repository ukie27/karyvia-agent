""" 内建 CLI 入口：控制台渲染、Channel 契约与两种执行模式。

职责：验 `CliConsole` 的渲染规则（流式不重复打、 的标记）、`CliChannel` 满足
`ChannelContract`、`StdioCliEntry` 的参数解析与单次/交互两种模式。
不负责：验它接到实例上之后的行为（`tests/runtime/test_bootstrap.py`）。

**这里的「回声」是一个假的 orchestrator**：一个后台任务消费 `channel.receive()`、
立刻投递一条终态出站消息。装配根的 Channel 泵做的是同一件事，只是中间隔着真 turn——
在这一层放真 orchestrator 只会让本文件重复 `tests/runtime/` 的断言。
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator

import pytest

from karyvia.builtins.cli_entry import (
    CONFIG_INSTANCE_ID_KEY,
    CONFIG_PROMPT_KEY,
    DROPPED_ATTACHMENTS_KEY,
    CliChannel,
    CliConsole,
    StdioCliEntry,
    build_console,
    resolve_settings,
    setup,
)
from karyvia.builtins.registry import CLI_ENTRY
from karyvia.contracts import (
    AttachmentRef,
    AttachmentSource,
    Channel,
    ErrorCode,
    InstanceId,
    KaryviaError,
    OutboundMessage,
    SessionKey,
    StreamState,
    TurnId,
)
from karyvia.kernel.turn import CancelToken
from karyvia.sdk.testing import ChannelContract, FakePluginContext

INSTANCE = InstanceId("test-instance")


def make_console(out: io.StringIO | None = None) -> CliConsole:
    return CliConsole(instance_id=INSTANCE, out=out)


def outbound(
    console: CliConsole,
    content: str,
    state: StreamState,
    *,
    reasoning: bool = False,
    attachments: tuple[AttachmentRef, ...] = (),
    dropped: int = 0,
) -> OutboundMessage:
    key = SessionKey(channel_id=console.channel_id, conversation_id=console.conversation_id)
    metadata: dict[str, object] = {"reasoning": True} if reasoning else {}
    if dropped:
        metadata[DROPPED_ATTACHMENTS_KEY] = dropped
    return OutboundMessage(
        session_key=key,
        channel_id=key.channel_id,
        conversation_id=key.conversation_id,
        turn_id=TurnId("turn-1"),
        content=content,
        attachments=attachments,
        stream_state=state,
        metadata=metadata,
    )


# ------------------------------------------------------------------------ 入站


def test_a_line_becomes_a_contract_shaped_inbound_message() -> None:
    """CLI 没有绕过 `InboundMessage` 的专用路径。"""
    console = make_console()
    message = console.submit("你好")
    assert message.content == "你好"
    assert message.channel_id == "cli"
    assert message.instance_id == INSTANCE
    # 本地用户即实例拥有者，否则 `/config` 这类 `operator_only` 命令在 CLI 上不可用。
    assert message.sender.is_operator


async def test_messages_stop_at_close() -> None:
    console = make_console()
    console.submit("一")
    console.close()
    received = [message.content async for message in console.messages()]
    assert received == ["一"]


def test_close_is_idempotent() -> None:
    console = make_console()
    console.close()
    console.close()


# ------------------------------------------------------------------------ 出站渲染


async def test_streaming_deltas_are_not_printed_twice() -> None:
    """终态消息带的是同一段完整正文——再打一遍就是重复。"""
    out = io.StringIO()
    console = make_console(out)
    console.submit("问")
    await console.deliver(outbound(console, "你", StreamState.DELTA))
    await console.deliver(outbound(console, "好", StreamState.DELTA))
    await console.deliver(outbound(console, "你好", StreamState.FINAL))
    assert out.getvalue() == "你好\n"


async def test_a_non_streaming_final_is_printed_once() -> None:
    out = io.StringIO()
    console = make_console(out)
    console.submit("问")
    await console.deliver(outbound(console, "你好", StreamState.FINAL))
    assert out.getvalue() == "你好\n"


@pytest.mark.parametrize(
    ("state", "marker"),
    [(StreamState.CANCELLED, "已中断"), (StreamState.FAILED, "本轮失败")],
)
async def test_incomplete_answers_carry_a_marker(state: StreamState, marker: str) -> None:
    """`is_complete_answer` 为假时必须附加标记，不得呈现成完整回答。"""
    out = io.StringIO()
    console = make_console(out)
    console.submit("问")
    await console.deliver(outbound(console, "说到一半", state))
    assert marker in out.getvalue()


async def test_attachments_are_listed_after_the_answer() -> None:
    """终帧带的附件在正文之后逐行印出来。

    **印路径而不是字节**：终端里字节没有呈现形态，而 workspace 相对路径可以直接喂给
    `fs.read`、也可以在文件管理器里打开。
    """
    out = io.StringIO()
    console = make_console(out)
    console.submit("画一张")
    message = outbound(
        console,
        "画好了",
        StreamState.FINAL,
        attachments=(
            AttachmentRef(
                source=AttachmentSource.WORKSPACE,
                locator="artifacts/images/image-abc.png",
                media_type="image/png",
                size_bytes=1024,
            ),
        ),
    )
    await console.deliver(message)
    assert out.getvalue() == "画好了\n[附件] artifacts/images/image-abc.png（1024 字节）\n"


async def test_dropped_attachments_are_reported_rather_than_silently_lost() -> None:
    """撞上上限时说一句。一个数不对的附件列表比一句说明更糟。"""
    out = io.StringIO()
    console = make_console(out)
    console.submit("画很多张")
    message = outbound(console, "好了", StreamState.FINAL, dropped=3)
    await console.deliver(message)
    assert "另有 3 个未随本轮发出" in out.getvalue()


def test_the_dropped_key_matches_the_kernel_constant() -> None:
    """`R4` 逼得这个键名在两处各写一份（内建够不着 `kernel/`），这里把它们钉在一起。"""
    from karyvia.kernel.turn import orchestration

    assert DROPPED_ATTACHMENTS_KEY == orchestration.DROPPED_ATTACHMENTS_KEY


async def test_reasoning_is_hidden_unless_asked() -> None:
    out = io.StringIO()
    console = make_console(out)
    console.submit("问")
    await console.deliver(outbound(console, "推理", StreamState.DELTA, reasoning=True))
    assert out.getvalue() == ""
    console.show_reasoning = True
    await console.deliver(outbound(console, "推理", StreamState.DELTA, reasoning=True))
    assert out.getvalue() == "推理"


async def test_a_terminal_message_releases_the_reader() -> None:
    console = make_console()
    console.submit("问")
    waiter = asyncio.ensure_future(console.wait_for_turn())
    assert not waiter.done()
    await console.deliver(outbound(console, "答", StreamState.FINAL))
    await asyncio.wait_for(waiter, timeout=1)


def test_a_rejected_turn_also_releases_the_reader() -> None:
    """去重或被队列拒的消息没有终态事件，读循环仍要能继续。"""
    console = make_console()
    console.submit("问")
    console.turn_rejected("[重复投递]")
    assert console.last_state is None


# ------------------------------------------------------------------------ Channel 契约


class TestCliChannelContract(ChannelContract):
    """内建 Channel 必须通过统一的 Channel 契约测试。"""

    def make_channel(self) -> Channel:
        return CliChannel(make_console())


async def test_the_channel_carries_inbound_messages(tmp_path: object) -> None:
    del tmp_path
    console = make_console()
    channel = CliChannel(console)
    await channel.start()
    console.submit("你好")
    stream = channel.receive()
    message = await asyncio.wait_for(anext(stream), timeout=1)
    assert message.content == "你好"
    await channel.stop()


# ------------------------------------------------------------------------ 入口


async def _echo(channel: CliChannel, console: CliConsole) -> None:
    """假 orchestrator：收到什么就投一条终态回去。"""
    async for message in channel.receive():
        await channel.deliver(
            OutboundMessage(
                session_key=message.session_key(),
                channel_id=message.channel_id,
                conversation_id=message.conversation_id,
                turn_id=TurnId("turn-1"),
                content=f"收到：{message.content}",
                stream_state=StreamState.FINAL,
            )
        )
    del console


@pytest.fixture
async def wired() -> AsyncIterator[tuple[CliConsole, CliChannel]]:
    out = io.StringIO()
    console = make_console(out)
    channel = CliChannel(console)
    task = asyncio.create_task(_echo(channel, console))
    yield console, channel
    console.close()
    await asyncio.wait_for(task, timeout=1)


async def test_one_shot_mode_runs_exactly_one_turn(
    wired: tuple[CliConsole, CliChannel],
) -> None:
    console, _ = wired
    entry = StdioCliEntry(console)
    code = await asyncio.wait_for(entry.run(["-p", "你好"], CancelToken()), timeout=2)
    assert code == 0
    assert console.rendered == ["收到：你好"]


async def test_interactive_mode_runs_one_turn_per_line(
    wired: tuple[CliConsole, CliChannel],
) -> None:
    console, _ = wired
    entry = StdioCliEntry(console, stdin=io.StringIO("一\n\n二\n"))
    code = await asyncio.wait_for(entry.run([], CancelToken()), timeout=2)
    assert code == 0
    # 空行不产生 turn——它是回车，不是一句话。
    assert console.rendered == ["收到：一", "收到：二"]


async def test_quit_words_end_the_session(wired: tuple[CliConsole, CliChannel]) -> None:
    console, _ = wired
    entry = StdioCliEntry(console, stdin=io.StringIO("一\n/exit\n二\n"))
    assert await asyncio.wait_for(entry.run([], CancelToken()), timeout=2) == 0
    assert console.rendered == ["收到：一"]


async def test_a_cancelled_run_reports_130(wired: tuple[CliConsole, CliChannel]) -> None:
    console, _ = wired
    cancel = CancelToken()
    cancel.request()
    assert await asyncio.wait_for(entry_of(console).run(["-p", "你好"], cancel), timeout=2) == 130


def entry_of(console: CliConsole) -> StdioCliEntry:
    return StdioCliEntry(console)


async def test_unknown_arguments_do_not_raise() -> None:
    """`CliEntry.run` **约定不抛**：参数错误要变成说明与退出码，不是 traceback。"""
    out = io.StringIO()
    console = make_console(out)
    code = await StdioCliEntry(console).run(["--nope"], CancelToken())
    assert code == 2
    assert "未知参数" in out.getvalue()


async def test_help_returns_zero() -> None:
    out = io.StringIO()
    code = await StdioCliEntry(make_console(out)).run(["--help"], CancelToken())
    assert code == 0
    assert "karyvia run" in out.getvalue()


# ------------------------------------------------------------------------ 配置与注册


def test_output_degrades_instead_of_failing_on_an_unencodable_character() -> None:
    """Windows 中文控制台是 GBK：一个 emoji 不该把一次正常的回答变成 traceback。"""

    class GbkOut(io.StringIO):
        encoding = "gbk"

        def write(self, text: str) -> int:
            text.encode("gbk")  # 复现真实控制台的行为：编不出来就抛
            return super().write(text)

    out = GbkOut()
    console = CliConsole(instance_id=INSTANCE, out=out)
    console.submit("问")
    asyncio.run(console.deliver(outbound(console, "答案 🎉", StreamState.FINAL)))
    assert "答案" in out.getvalue()


def test_settings_come_from_the_config_block() -> None:
    ctx = FakePluginContext(
        "cli-entry", config={CONFIG_INSTANCE_ID_KEY: "work", CONFIG_PROMPT_KEY: "> "}
    )
    settings = resolve_settings(ctx)
    assert settings.instance_id == "work"
    assert settings.prompt == "> "
    assert build_console(settings).instance_id == InstanceId("work")


def test_a_bad_config_fails_at_setup_time() -> None:
    """配置错误在 setup 时暴露，而不是第一次输入时才失败。"""
    ctx = FakePluginContext("cli-entry", config={"show_reasoning": "yes"})
    with pytest.raises(KaryviaError) as caught:
        resolve_settings(ctx)
    assert caught.value.code is ErrorCode.CONFIG_INVALID


def test_the_manifest_declares_exactly_two_capabilities() -> None:
    """入口拥有进程、Channel 拥有消息路径——合成一条就得让其中一件事走近路。"""
    kinds = sorted(decl.kind.value for decl in CLI_ENTRY.capabilities)
    assert kinds == ["channel", "cli_entry"]


def test_setup_registers_both_capabilities() -> None:
    registered: list[str] = []

    class Recorder:
        ctx = FakePluginContext("cli-entry")

        def register_cli_entry(self, name: str, entry: object) -> None:
            registered.append(f"cli_entry:{name}")

        def register_channel(self, name: str, channel: object) -> None:
            registered.append(f"channel:{name}")

    setup(Recorder())  # type: ignore[arg-type]
    assert registered == ["cli_entry:stdio", "channel:cli"]
