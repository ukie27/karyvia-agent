"""工具进度提示的验收（开发方案 `D34`）。

| 验收项 | 测试 |
| --- | --- |
| 渲染与相邻折叠 | `TestRender` |
| 前缀为空即整条关闭 | `TestRender` / `TestSubscription` |
| 事件按 `channel_id` 与 `turn_id` 过滤 | `TestSubscription` |
| 回调同步不抛、队列满了只丢不堵 | `TestSubscription` |
| 提示进流式卡片、跨 conversation 不串 | `TestDelivery` |

**为什么提示要走事件订阅**：`OutboundMessage` 只有 `content` 与 `metadata`，不带工具调用
信息，因此 Channel 拿得到的唯一来源是 `tool.call_started`。相应地，提示里**只有工具名
没有参数**——`tool_hints.py` 的模块 docstring 记着这条相对 legacy 的回退。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from _feishu_fakes import (
    APP_ID,
    APP_SECRET,
    CHANNEL_ID,
    CHAT_ID,
    FakeClient,
    FakeClock,
    FakeGateway,
    outbound,
    settings,
)
from karyvia_plugin_feishu import FeishuChannel
from karyvia_plugin_feishu.channel import _group
from karyvia_plugin_feishu.tool_hints import DEFAULT_PREFIX, render

from karyvia.contracts import (
    Correlation,
    EventName,
    InstanceId,
    RuntimeEvent,
    SessionKey,
    StreamState,
    TurnId,
)


def make_channel(**kwargs: object) -> tuple[FeishuChannel, FakeClient, FakeClock]:
    """一条装了可控时钟的 Channel。

    **时钟必须可控**：提示走的是与模型增量同一条节流路径（`StreamRelay._pump`），
    用真实时钟意味着每条断言都要真的等 0.5 秒。
    """
    client, clock = FakeClient(), FakeClock()
    channel = FeishuChannel(
        settings(**kwargs),  # type: ignore[arg-type]
        app_id=APP_ID,
        app_secret=APP_SECRET,
        gateway=FakeGateway(),  # type: ignore[arg-type]
        client=client,
    )
    channel._relay.now_ms = clock  # noqa: SLF001
    return channel, client, clock


def tool_event(
    tool: str = "fs.read",
    *,
    channel_id: str = CHANNEL_ID,
    conversation: str = CHAT_ID,
    turn: str = "turn-1",
) -> RuntimeEvent:
    """一条 `tool.call_started`，形状与 `kernel/turn/orchestrator.py` 的发布点一致。"""
    return RuntimeEvent(
        name=EventName.TOOL_CALL_STARTED,
        sequence=1,
        occurred_at=datetime.now(UTC),
        instance_id=InstanceId("test"),
        correlation=Correlation(
            instance_id=InstanceId("test"),
            session_key=SessionKey(channel_id=channel_id, conversation_id=conversation),
            turn_id=TurnId(turn),
        ),
        payload={"tool": tool, "call_id": "call-1"},
    )


# ------------------------------------------------------------------------------ 渲染


class TestRender:
    def test_one_call_is_one_line(self) -> None:
        assert render(DEFAULT_PREFIX, ["fs.read"]) == f"{DEFAULT_PREFIX} fs.read"

    def test_adjacent_duplicates_collapse(self) -> None:
        """并行发起的五个 `fs.read` 该是一行，不是五行。"""
        assert render("🔧", ["fs.read"] * 5) == "🔧 fs.read × 5"

    def test_order_is_preserved_and_non_adjacent_repeats_stay_apart(self) -> None:
        """顺序是用户判断 agent 在干什么的线索——读—写—读不该被折成「读 ×2、写」。"""
        assert render("🔧", ["fs.read", "fs.write", "fs.read"]) == (
            "🔧 fs.read\n🔧 fs.write\n🔧 fs.read"
        )

    def test_an_empty_prefix_turns_the_whole_thing_off(self) -> None:
        """不想要提示的运维不该还得忍受一个没有前缀的裸工具名。"""
        assert render("", ["fs.read"]) == ""

    def test_blank_names_are_dropped_not_fatal(self) -> None:
        """畸形的工具调用不该让整块提示消失。"""
        assert render("🔧", ["", "  ", "fs.read"]) == "🔧 fs.read"

    def test_no_names_is_an_empty_block(self) -> None:
        assert render("🔧", []) == ""


class TestGrouping:
    def test_adjacent_entries_of_one_turn_group(self) -> None:
        grouped = _group([(CHAT_ID, "t1", "a"), (CHAT_ID, "t1", "b")])
        assert grouped == [(CHAT_ID, "t1", ["a", "b"])]

    def test_two_conversations_do_not_merge(self) -> None:
        """按会话聚合成字典会把「A、B、A」重排成「A ×2、B」。"""
        grouped = _group([(CHAT_ID, "t1", "a"), ("oc_other", "t2", "b"), (CHAT_ID, "t1", "c")])
        assert grouped == [
            (CHAT_ID, "t1", ["a"]),
            ("oc_other", "t2", ["b"]),
            (CHAT_ID, "t1", ["c"]),
        ]


# ------------------------------------------------------------------------------ 订阅


class TestSubscription:
    def test_the_handler_enqueues_a_hint(self) -> None:
        channel, _, _ = make_channel()
        channel.on_tool_call(tool_event())
        assert channel._hints.get_nowait() == (CHAT_ID, "turn-1", "fs.read")  # noqa: SLF001

    def test_another_channels_tool_calls_are_ignored(self) -> None:
        """事件总线是实例级的：一条飞书 Channel 会看到 CLI 与 HTTP API 的全部工具调用。"""
        channel, _, _ = make_channel()
        channel.on_tool_call(tool_event(channel_id="cli"))
        assert channel._hints.empty()  # noqa: SLF001

    def test_an_instance_level_event_without_correlation_is_ignored(self) -> None:
        channel, _, _ = make_channel()
        event = RuntimeEvent(
            name=EventName.TOOL_CALL_STARTED,
            sequence=1,
            occurred_at=datetime.now(UTC),
            instance_id=InstanceId("test"),
            payload={"tool": "fs.read"},
        )
        channel.on_tool_call(event)
        assert channel._hints.empty()  # noqa: SLF001

    def test_a_payload_without_a_tool_name_is_ignored(self) -> None:
        channel, _, _ = make_channel()
        event = tool_event()
        object.__setattr__(event, "payload", {"call_id": "call-1"})
        channel.on_tool_call(event)
        assert channel._hints.empty()  # noqa: SLF001

    def test_a_full_queue_drops_instead_of_raising(self) -> None:
        """提示是锦上添花——为它把事件发布堵住或炸掉是本末倒置。"""
        channel, _, _ = make_channel()
        for _ in range(channel._hints.maxsize + 10):  # noqa: SLF001
            channel.on_tool_call(tool_event())
        assert channel._hints.full()  # noqa: SLF001

    async def test_no_prefix_means_no_pump_task(self) -> None:
        """关掉之后连那条后台任务都不该存在。"""
        channel, _, _ = make_channel(tool_hint_prefix="")
        await channel.start()
        assert channel._hint_pump is None  # noqa: SLF001
        await channel.stop()

    async def test_the_pump_stops_with_the_channel(self) -> None:
        channel, _, _ = make_channel()
        await channel.start()
        pump = channel._hint_pump  # noqa: SLF001
        assert pump is not None
        await channel.stop()
        assert pump.done()
        assert channel._hint_pump is None  # noqa: SLF001


# ------------------------------------------------------------------------------ 投递


class TestDelivery:
    async def _drain(self, channel: FeishuChannel) -> None:
        """让泵跑一轮。队列空即让出，不真的等时间。"""
        while not channel._hints.empty():  # noqa: SLF001
            await asyncio.sleep(0)

    async def test_a_hint_reaches_the_streaming_card(self) -> None:
        channel, client, clock = make_channel()
        await channel.start()
        await channel.deliver(outbound("", state=StreamState.STARTED))
        await channel.deliver(outbound("先说一句", state=StreamState.DELTA))
        clock.advance(600)
        channel.on_tool_call(tool_event())
        await self._drain(channel)
        assert any(DEFAULT_PREFIX in content for content in client.contents)
        await channel.stop()

    async def test_a_hint_for_another_turn_is_discarded(self) -> None:
        """一条迟到的提示落进下一轮的卡片，比不显示它更糟。"""
        channel, client, clock = make_channel()
        await channel.start()
        await channel.deliver(outbound("", state=StreamState.STARTED))
        await channel.deliver(outbound("先说一句", state=StreamState.DELTA))
        before = len(client.contents)
        clock.advance(600)
        channel.on_tool_call(tool_event(turn="turn-999"))
        await self._drain(channel)
        assert len(client.contents) == before
        await channel.stop()

    async def test_a_hint_for_an_unknown_conversation_is_discarded(self) -> None:
        channel, client, clock = make_channel()
        await channel.start()
        before = len(client.contents)
        clock.advance(600)
        channel.on_tool_call(tool_event(conversation="oc_never_seen"))
        await self._drain(channel)
        assert len(client.contents) == before
        await channel.stop()

    async def test_each_conversation_only_sees_its_own_hints(self) -> None:
        """`D33` 起 `deliver` 可并发，缓冲按 conversation 分片——提示不该串台。"""
        channel, client, clock = make_channel()
        await channel.start()
        other = "oc_chat_2"
        for conversation in (CHAT_ID, other):
            await channel.deliver(
                outbound("", state=StreamState.STARTED, conversation=conversation)
            )
            await channel.deliver(
                outbound("在想", state=StreamState.DELTA, conversation=conversation)
            )
        clock.advance(600)
        channel.on_tool_call(tool_event(tool="fs.read", conversation=CHAT_ID))
        channel.on_tool_call(tool_event(tool="shell.exec", conversation=other))
        await self._drain(channel)
        mine = [c for c in client.contents if "fs.read" in c]
        theirs = [c for c in client.contents if "shell.exec" in c]
        assert mine and theirs
        assert not any("shell.exec" in c for c in mine)
        assert not any("fs.read" in c for c in theirs)
        await channel.stop()

    async def test_the_final_answer_replaces_the_hints(self) -> None:
        """提示是过程指示：终态卡片上该是答案本身，不该还挂着一串工具名。"""
        channel, client, clock = make_channel()
        await channel.start()
        await channel.deliver(outbound("", state=StreamState.STARTED))
        await channel.deliver(outbound("想一下", state=StreamState.DELTA))
        clock.advance(600)
        channel.on_tool_call(tool_event())
        await self._drain(channel)
        await channel.deliver(outbound("最终答案"))
        assert client.contents[-1] == "最终答案"
        await channel.stop()
