"""CardKit 流式状态机的验收。

| 验收项 | 测试 |
| --- | --- |
| 建卡 / 节流 / 收束的正常路径 | `TestHappyPath` |
| **sequence 的唯一规则**（共用计数器、失败也 bump） | `TestSequence` |
| 每一条失败路径与它的 streaming_mode 关闭 | `TestFailurePaths` |
| `Channel.stop()` 关掉所有还开着的卡片 | `TestShutdown` |

**全程用注入的 `FakeClock`，不 `sleep`**：节流是本模块唯一的时间语义。
`FakeCards` 记录 `(op, card_id, sequence)` 三元组，因此每条失败路径都能断言**完整的
sequence 序列**——跳号或复用会当场被看见，而断言「调了几次」看不出来。
"""

from __future__ import annotations

from _feishu_fakes import CHAT_ID, FakeCards, FakeClock, FakeMessenger, outbound
from karyvia_plugin_feishu import StreamRelay

from karyvia.contracts import OutboundMessage, StreamState


def relay(cards: FakeCards, messenger: FakeMessenger, clock: FakeClock, **kwargs: object) -> StreamRelay:
    def target(message: OutboundMessage) -> tuple[str, str | None, bool]:
        return CHAT_ID, None, False

    return StreamRelay(
        cards=cards,  # type: ignore[arg-type]
        messenger=messenger,  # type: ignore[arg-type]
        now_ms=clock,  # type: ignore[arg-type]
        resolve_target=target,
        **kwargs,  # type: ignore[arg-type]
    )


def build() -> tuple[StreamRelay, FakeCards, FakeMessenger, FakeClock]:
    cards, messenger, clock = FakeCards(), FakeMessenger(), FakeClock()
    return relay(cards, messenger, clock), cards, messenger, clock


class TestHappyPath:
    async def test_started_creates_nothing(self) -> None:
        """空卡片在会话列表里是噪声。"""
        stream, cards, messenger, _ = build()
        await stream.handle(outbound("", state=StreamState.STARTED))
        assert cards.calls == []
        assert messenger.sent == []
        assert stream.active() == 1

    async def test_a_blank_first_delta_waits_for_real_text(self) -> None:
        stream, cards, _, _ = build()
        await stream.handle(outbound(" ", state=StreamState.DELTA))
        assert cards.calls == []
        await stream.handle(outbound("你好", state=StreamState.DELTA))
        assert cards.calls[0][0] == "create"

    async def test_the_card_is_sent_as_an_interactive_message(self) -> None:
        stream, _, messenger, _ = build()
        await stream.handle(outbound("你好", state=StreamState.DELTA))
        chat_id, body = messenger.sent[0]
        assert chat_id == CHAT_ID
        assert body.msg_type == "interactive"
        assert "card-1" in body.content

    async def test_throttled_text_is_not_lost(self) -> None:
        """被节流跳过的文本留在缓冲里，收束时必须完整。"""
        stream, cards, _, clock = build()
        await stream.handle(outbound("一", state=StreamState.DELTA))
        for text in ("二", "三", "四"):
            clock.advance(100)  # 全在 500ms 窗口内
            await stream.handle(outbound(text, state=StreamState.DELTA))
        assert cards.contents == ["一"]
        await stream.handle(outbound("", state=StreamState.CANCELLED))
        assert "一二三四" in cards.contents[-1]

    async def test_an_update_happens_once_the_interval_passes(self) -> None:
        stream, cards, _, clock = build()
        await stream.handle(outbound("一", state=StreamState.DELTA))
        clock.advance(600)
        await stream.handle(outbound("二", state=StreamState.DELTA))
        assert cards.contents == ["一", "一二"]

    async def test_the_terminal_content_wins_over_the_deltas(self) -> None:
        stream, cards, _, _ = build()
        await stream.handle(outbound("半", state=StreamState.DELTA))
        await stream.handle(outbound("完整的答案", state=StreamState.FINAL))
        assert cards.contents[-1] == "完整的答案"

    async def test_a_finished_conversation_releases_its_buffer(self) -> None:
        stream, _, _, _ = build()
        await stream.handle(outbound("x", state=StreamState.DELTA))
        await stream.handle(outbound("x", state=StreamState.FINAL))
        assert stream.active() == 0

    async def test_a_new_turn_gets_a_new_card(self) -> None:
        """旧卡片原样留着——它自己的终态已经带过  标记了。"""
        stream, cards, _, _ = build()
        await stream.handle(outbound("一轮", state=StreamState.DELTA, turn="t1"))
        await stream.handle(outbound("二轮", state=StreamState.DELTA, turn="t2"))
        assert sum(1 for op, _, _ in cards.calls if op == "create") == 2

    async def test_different_conversations_use_different_buffers(self) -> None:
        """ 的泵按 conversation 扇出，各条 lane 只碰自己那个键，因此不用加锁。"""
        stream, _, _, _ = build()
        await stream.handle(outbound("A", state=StreamState.DELTA, conversation="oc_a"))
        await stream.handle(outbound("B", state=StreamState.DELTA, conversation="oc_b"))
        assert stream.active() == 2

    async def test_streaming_off_only_sends_at_the_end(self) -> None:
        cards, messenger, clock = FakeCards(), FakeMessenger(), FakeClock()
        stream = relay(cards, messenger, clock, streaming=False)
        for text in ("一", "二"):
            await stream.handle(outbound(text, state=StreamState.DELTA))
        assert cards.calls == []
        await stream.handle(outbound("一二", state=StreamState.FINAL))
        assert messenger.sent and messenger.sent[0][1].msg_type == "text"

    async def test_a_terminal_marker_reaches_the_card(self) -> None:
        stream, cards, _, _ = build()
        await stream.handle(outbound("半句", state=StreamState.DELTA))
        await stream.handle(outbound("", state=StreamState.CANCELLED))
        assert "已中断" in cards.contents[-1]
        assert "半句" in cards.contents[-1]


class TestSequence:
    async def test_content_and_settings_share_one_counter(self) -> None:
        """**这是本模块最容易写错的一条**：两类操作共用一个严格递增的计数器。"""
        stream, cards, _, clock = build()
        await stream.handle(outbound("一", state=StreamState.DELTA))
        clock.advance(600)
        await stream.handle(outbound("二", state=StreamState.DELTA))
        await stream.handle(outbound("答案", state=StreamState.FINAL))
        sequences = cards.sequences()
        assert sequences == sorted(sequences)
        assert len(set(sequences)) == len(sequences), "sequence 不得复用"

    async def test_the_sequence_is_bumped_even_when_a_call_fails(self) -> None:
        """失败可能发生在平台**已经消费掉**那个 sequence 之后（超时、连接断在响应回来
        之前）。复用同一个数会让后续全部更新被判为非严格递增而**永久**失败。"""
        cards = FakeCards(fail_on={("content", 1)})
        stream = relay(cards, FakeMessenger(), FakeClock())
        await stream.handle(outbound("一", state=StreamState.DELTA))
        sequences = cards.sequences()
        assert sequences == sorted(sequences)
        assert len(set(sequences)) == len(sequences)
        # 首次更新失败 → reopen（2）→ 再更新（3）。
        assert sequences[:3] == [1, 2, 3]

    async def test_a_failed_update_reopens_streaming_then_retries(self) -> None:
        """飞书会在超时后自己把 streaming_mode 关掉，此后内容更新一律失败。"""
        cards = FakeCards(fail_on={("content", 1)})
        stream = relay(cards, FakeMessenger(), FakeClock())
        await stream.handle(outbound("一", state=StreamState.DELTA))
        ops = [op for op, _, _ in cards.calls]
        assert ops == ["create", "content", "settings", "content"]
        assert cards.contents == ["一"]


class TestFailurePaths:
    async def test_a_failed_create_degrades_to_a_plain_message(self) -> None:
        cards = FakeCards(card_id=None)
        messenger = FakeMessenger()
        stream = relay(cards, messenger, FakeClock())
        await stream.handle(outbound("你好", state=StreamState.DELTA))
        await stream.handle(outbound("你好", state=StreamState.FINAL))
        assert messenger.sent and messenger.sent[0][1].msg_type == "text"

    async def test_a_card_that_cannot_be_sent_degrades(self) -> None:
        """卡建出来了但没发出去：它不在任何会话里，关不关都没人看得见。"""
        cards = FakeCards()
        messenger = FakeMessenger(fail=True)
        stream = relay(cards, messenger, FakeClock())
        await stream.handle(outbound("你好", state=StreamState.DELTA))
        assert [op for op, _, _ in cards.calls] == ["create"]

    async def test_a_reopen_that_also_fails_closes_streaming(self) -> None:
        """**不关会让会话列表永久显示「生成中」。**"""
        cards = FakeCards(fail_on={("content", 1), ("settings", 2)})
        stream = relay(cards, FakeMessenger(), FakeClock())
        await stream.handle(outbound("一", state=StreamState.DELTA))
        ops = [op for op, _, _ in cards.calls]
        assert ops[-1] == "settings", "失败之后必须关掉 streaming_mode"

    async def test_a_failed_throttled_update_closes_and_degrades(self) -> None:
        cards = FakeCards(fail_on={("content", 2), ("settings", 3), ("content", 4)})
        clock = FakeClock()
        stream = relay(cards, FakeMessenger(), clock)
        await stream.handle(outbound("一", state=StreamState.DELTA))
        clock.advance(600)
        await stream.handle(outbound("二", state=StreamState.DELTA))
        assert [op for op, _, _ in cards.calls][-1] == "settings"

    async def test_the_terminal_always_closes_streaming(self) -> None:
        stream, cards, _, _ = build()
        await stream.handle(outbound("一", state=StreamState.DELTA))
        await stream.handle(outbound("答案", state=StreamState.FINAL))
        assert [op for op, _, _ in cards.calls][-1] == "settings"

    async def test_a_failed_close_is_retried_once(self) -> None:
        cards = FakeCards(fail_on={("settings", 3)})
        stream = relay(cards, FakeMessenger(), FakeClock())
        await stream.handle(outbound("一", state=StreamState.DELTA))
        await stream.handle(outbound("答案", state=StreamState.FINAL))
        closes = [seq for op, _, seq in cards.calls if op == "settings"]
        assert len(closes) >= 2, "关不掉要再试一次"

    async def test_a_terminal_update_failure_falls_back_to_a_plain_card(self) -> None:
        cards = FakeCards(fail_on={("content", 2), ("settings", 3), ("content", 4)})
        messenger = FakeMessenger()
        stream = relay(cards, messenger, FakeClock())
        await stream.handle(outbound("一", state=StreamState.DELTA))
        await stream.handle(outbound("最终答案", state=StreamState.FINAL))
        assert messenger.sent, "整条链失败要回落成普通消息"


class TestShutdown:
    async def test_shutdown_closes_every_open_card(self) -> None:
        """legacy 没有这一条：实例被 Ctrl-C 时留着的卡片会**永久**显示「生成中」。"""
        stream, cards, _, _ = build()
        await stream.handle(outbound("A", state=StreamState.DELTA, conversation="oc_a"))
        await stream.handle(outbound("B", state=StreamState.DELTA, conversation="oc_b"))
        await stream.shutdown()
        assert stream.active() == 0
        assert [op for op, _, _ in cards.calls].count("settings") >= 2

    async def test_shutdown_is_safe_with_nothing_open(self) -> None:
        stream, cards, _, _ = build()
        await stream.shutdown()
        assert cards.calls == []
