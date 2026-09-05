"""请求侧线格式的验收：`wire.py` 的纯函数（开发方案 `D32`）。

| 验收项 | 测试 |
| --- | --- |
| 请求体符合 Anthropic Messages 线格式 | `TestRequestEncoding` |
| 消息序列规整三条（合并 / 禁尾部 assistant / 禁首部 assistant） | `TestTurnNormalization` |
| 工具名与 `tool_use_id` 编码 | `TestToolNaming` |
| thinking 四形态与 prompt caching 断点 | `TestThinkingAndCaching` |

这一整个文件**不需要事件循环、不发一次请求**：线格式的每一条规则都能逐字节钉住，
行为的断言归 `test_anthropic_plugin.py`。混在一起会让「payload 里少了一个键」这种失败在
一堆异步栈里冒出来。
"""

from __future__ import annotations

import json

import pytest
from _support import make_request, payload_for, sample_tool
from karyvia_plugin_anthropic import (
    CachingSpec,
    ThinkingSpec,
    build_payload,
    decode_tool_name,
    encode_tool_name,
)
from karyvia_plugin_anthropic.wire import (
    CONVERSATION_CONTINUED,
    EMPTY_TEXT,
    encode_messages,
    normalize_turns,
    sanitize_tool_id,
    thinking_blocks,
)

from karyvia.contracts import (
    ModelMessage,
    OpaqueBlock,
    Role,
    SamplingParams,
    ToolCall,
)

# ------------------------------------------------------------------------------ 请求编码


class TestRequestEncoding:
    """线格式的硬约束。发错就是 400，不是风格问题。"""

    def test_system_messages_are_hoisted_to_the_top_level(self) -> None:
        """Anthropic 没有 system 角色，它是顶层字段。"""
        payload = payload_for(
            ModelMessage(role=Role.SYSTEM, content="你是助手"),
            ModelMessage(role=Role.USER, content="你好"),
        )
        assert payload["system"] == [{"type": "text", "text": "你是助手"}]
        assert [turn["role"] for turn in payload["messages"]] == ["user"]

    def test_multiple_system_messages_become_multiple_blocks(self) -> None:
        """不拼成一个字符串：拼接会让「哪一段是谁加的」不可分，而 cache_control 挂在块上。"""
        payload = payload_for(
            ModelMessage(role=Role.SYSTEM, content="一"),
            ModelMessage(role=Role.SYSTEM, content="二"),
            ModelMessage(role=Role.USER, content="你好"),
        )
        assert payload["system"] == [
            {"type": "text", "text": "一"},
            {"type": "text", "text": "二"},
        ]

    def test_no_system_message_omits_the_key(self) -> None:
        assert "system" not in payload_for(ModelMessage(role=Role.USER, content="你好"))

    def test_tool_results_fold_into_the_preceding_user_turn(self) -> None:
        """契约的 `Role.TOOL` 在 Anthropic 这边是 user 轮里的一个 `tool_result` 块。"""
        call = ToolCall(call_id="toolu_1", name="fs.read", arguments={"path": "a.txt"})
        payload = payload_for(
            ModelMessage(role=Role.USER, content="读一下"),
            ModelMessage(role=Role.ASSISTANT, tool_calls=(call,)),
            ModelMessage(role=Role.TOOL, content="内容", tool_call_id="toolu_1"),
            ModelMessage(role=Role.USER, content="谢谢"),
        )
        roles = [turn["role"] for turn in payload["messages"]]
        assert roles == ["user", "assistant", "user"]
        blocks = payload["messages"][2]["content"]
        assert blocks[0] == {
            "type": "tool_result",
            "tool_use_id": "toolu_1",
            "content": "内容",
        }
        assert blocks[1] == {"type": "text", "text": "谢谢"}

    def test_a_tool_result_without_a_preceding_user_turn_opens_one(self) -> None:
        call = ToolCall(call_id="toolu_1", name="fs.read")
        payload = payload_for(
            ModelMessage(role=Role.USER, content="读"),
            ModelMessage(role=Role.ASSISTANT, tool_calls=(call,)),
            ModelMessage(role=Role.TOOL, content="内容", tool_call_id="toolu_1"),
        )
        assert [turn["role"] for turn in payload["messages"]] == ["user", "assistant", "user"]

    def test_assistant_tool_calls_become_tool_use_blocks(self) -> None:
        call = ToolCall(call_id="toolu_1", name="fs.read", arguments={"path": "a.txt"})
        payload = payload_for(
            ModelMessage(role=Role.USER, content="读"),
            ModelMessage(role=Role.ASSISTANT, content="好的", tool_calls=(call,)),
            ModelMessage(role=Role.TOOL, content="ok", tool_call_id="toolu_1"),
        )
        blocks = payload["messages"][1]["content"]
        assert blocks[0] == {"type": "text", "text": "好的"}
        assert blocks[1] == {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "fs-read",
            "input": {"path": "a.txt"},
        }

    def test_max_tokens_is_always_present(self) -> None:
        """Anthropic 的 `max_tokens` 是必填字段，没有「用服务端默认」这一档。"""
        assert payload_for(ModelMessage(role=Role.USER, content="hi"))["max_tokens"] == 1024

    def test_explicit_max_output_tokens_wins(self) -> None:
        payload = build_payload(
            make_request(params=SamplingParams(max_output_tokens=77)), max_output_tokens=1024
        )
        assert payload["max_tokens"] == 77

    def test_seed_is_dropped(self) -> None:
        """Anthropic 没有这个参数。为一个旋钮让整轮失败不合算，因此丢弃。"""
        payload = build_payload(
            make_request(params=SamplingParams(seed=7)), max_output_tokens=1024
        )
        assert "seed" not in payload

    def test_temperature_is_omitted_not_clamped_when_unsupported(self) -> None:
        """Opus 4.7+ 对 `temperature` 直接 400，而替用户挑一个温度是在替它改采样行为。"""
        request = make_request(params=SamplingParams(temperature=0.2))
        assert build_payload(request, max_output_tokens=1024)["temperature"] == 0.2
        assert "temperature" not in build_payload(
            request, max_output_tokens=1024, supports_temperature=False
        )

    def test_stop_sequences_use_the_anthropic_key(self) -> None:
        payload = build_payload(
            make_request(params=SamplingParams(stop_sequences=("END",))), max_output_tokens=1024
        )
        assert payload["stop_sequences"] == ["END"]

    def test_no_tools_means_neither_tools_nor_tool_choice(self) -> None:
        payload = build_payload(make_request(), max_output_tokens=1024)
        assert "tools" not in payload
        assert "tool_choice" not in payload

    def test_tools_are_encoded_with_input_schema(self) -> None:
        payload = build_payload(make_request(tools=(sample_tool(),)), max_output_tokens=1024)
        assert payload["tools"] == [
            {
                "name": "fs-read",
                "description": "读一个文件",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            }
        ]
        assert payload["tool_choice"] == {"type": "auto"}

    def test_stream_flag_only_when_requested(self) -> None:
        assert "stream" not in build_payload(make_request(), max_output_tokens=1)
        assert build_payload(make_request(), max_output_tokens=1, stream=True)["stream"] is True


class TestTurnNormalization:
    """规整三条。它们是 Anthropic 比 OpenAI 严格的地方，漏一条就是 400。"""

    def test_consecutive_same_role_turns_are_merged(self) -> None:
        payload = payload_for(
            ModelMessage(role=Role.USER, content="一"),
            ModelMessage(role=Role.USER, content="二"),
        )
        assert len(payload["messages"]) == 1
        assert payload["messages"][0]["content"] == [
            {"type": "text", "text": "一"},
            {"type": "text", "text": "二"},
        ]

    def test_trailing_assistant_turns_are_stripped(self) -> None:
        """Anthropic 不支持 assistant prefill，尾部留着就是 400。"""
        payload = payload_for(
            ModelMessage(role=Role.USER, content="一"),
            ModelMessage(role=Role.ASSISTANT, content="二"),
        )
        assert [turn["role"] for turn in payload["messages"]] == ["user"]

    def test_stripping_everything_reroutes_the_last_assistant_as_user(self) -> None:
        """剥空了就改投，否则换来的是一句「messages 为空」的 400，更难诊断。"""
        payload = payload_for(ModelMessage(role=Role.ASSISTANT, content="孤儿"))
        assert payload["messages"] == [{"role": "user", "content": [{"type": "text", "text": "孤儿"}]}]

    def test_a_tool_use_carrying_assistant_is_not_rerouted(self) -> None:
        """`tool_use` 块在 user 轮里非法，改投会造出更难诊断的 400。"""
        turns = normalize_turns(
            [{"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "fs-read", "input": {}}]}]
        )
        assert turns == []

    def test_a_leading_assistant_gets_a_synthetic_user_turn(self) -> None:
        turns = normalize_turns(
            [
                {"role": "assistant", "content": [{"type": "text", "text": "上半句"}]},
                {"role": "user", "content": [{"type": "text", "text": "接着说"}]},
            ]
        )
        assert turns[0] == {"role": "user", "content": [{"type": "text", "text": CONVERSATION_CONTINUED}]}
        assert [turn["role"] for turn in turns] == ["user", "assistant", "user"]

    def test_a_leading_tool_use_assistant_is_left_alone(self) -> None:
        """前插一条会让紧随其后的 `tool_result` 找不到配对。"""
        turns = normalize_turns(
            [
                {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "fs-read", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
            ]
        )
        assert [turn["role"] for turn in turns] == ["assistant", "user"]

    def test_empty_text_blocks_are_dropped_and_empty_turns_get_a_floor(self) -> None:
        """Anthropic 拒绝空 `text` 与空 `content`。"""
        turns = normalize_turns([{"role": "user", "content": [{"type": "text", "text": ""}]}])
        assert turns == [{"role": "user", "content": [{"type": "text", "text": EMPTY_TEXT}]}]


class TestToolNaming:
    """工具名与 tool id 的编码。两者都是 Anthropic 独有的字符集约束。"""

    def test_tool_names_round_trip(self) -> None:
        """契约名恒不含 `-`，因此 `.` ↔ `-` 是无碰撞双射。"""
        for name in ("fs.read", "shell.exec", "echo.say", "a", "a.b.c", "a_b.c_d"):
            assert decode_tool_name(encode_tool_name(name)) == name
            assert "." not in encode_tool_name(name)

    def test_encoded_names_match_the_anthropic_charset(self) -> None:
        import re

        pattern = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
        for name in ("fs.read", "shell.exec", "a.b.c.d"):
            assert pattern.match(encode_tool_name(name))

    def test_valid_tool_ids_pass_through_unchanged(self) -> None:
        """绝大多数情况下这是恒等变换，历史不会因为换 Provider 就产生一批新 id。"""
        assert sanitize_tool_id("toolu_01ABC") == "toolu_01ABC"
        assert sanitize_tool_id("call_abc-123") == "call_abc-123"

    def test_illegal_tool_ids_are_rewritten_and_stay_distinct(self) -> None:
        first = sanitize_tool_id("call|a.b")
        second = sanitize_tool_id("call|a:b")
        assert first != second
        assert "|" not in first and "." not in first

    def test_tool_use_and_tool_result_agree_after_rewriting(self) -> None:
        """两侧走同一张映射表，否则 `tool_result` 会认错它对应的 `tool_use`。"""
        call = ToolCall(call_id="call|weird", name="fs.read")
        _, turns = encode_messages(
            (
                ModelMessage(role=Role.USER, content="读"),
                ModelMessage(role=Role.ASSISTANT, tool_calls=(call,)),
                ModelMessage(role=Role.TOOL, content="ok", tool_call_id="call|weird"),
            )
        )
        used = turns[1]["content"][0]["tool_use_id" if "tool_use_id" in turns[1]["content"][0] else "id"]
        result = turns[2]["content"][0]["tool_use_id"]
        assert used == result != "call|weird"


class TestThinkingAndCaching:
    """thinking 的四种形状与 prompt caching 的三个断点。"""

    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            (ThinkingSpec(mode="off"), None),
            (ThinkingSpec(mode="disabled"), {"type": "disabled"}),
            (ThinkingSpec(mode="adaptive"), {"type": "adaptive"}),
            (ThinkingSpec(mode="adaptive", display="summarized"), {"type": "adaptive", "display": "summarized"}),
            (ThinkingSpec(mode="budget", budget_tokens=2048), {"type": "enabled", "budget_tokens": 2048}),
        ],
    )
    def test_thinking_shapes(self, spec: ThinkingSpec, expected: object) -> None:
        payload = build_payload(make_request(), max_output_tokens=4096, thinking=spec)
        assert payload.get("thinking") == expected

    def test_thinking_off_omits_the_key_entirely(self) -> None:
        assert "thinking" not in build_payload(make_request(), max_output_tokens=1)

    def test_effort_becomes_output_config(self) -> None:
        payload = build_payload(make_request(), max_output_tokens=1, effort="high")
        assert payload["output_config"] == {"effort": "high"}
        assert "output_config" not in build_payload(make_request(), max_output_tokens=1)

    def test_caching_marks_three_positions(self) -> None:
        caching = CachingSpec(enabled=True, ttl="5m")
        payload = build_payload(
            make_request(
                messages=(
                    ModelMessage(role=Role.SYSTEM, content="系统"),
                    ModelMessage(role=Role.USER, content="一"),
                    ModelMessage(role=Role.ASSISTANT, content="二"),
                    ModelMessage(role=Role.USER, content="三"),
                ),
                tools=(sample_tool(),),
            ),
            max_output_tokens=1,
            caching=caching,
        )
        marker = {"type": "ephemeral", "ttl": "5m"}
        assert payload["tools"][-1]["cache_control"] == marker
        assert payload["system"][-1]["cache_control"] == marker
        assert payload["messages"][-2]["content"][-1]["cache_control"] == marker

    def test_caching_never_exceeds_the_four_breakpoint_limit(self) -> None:
        payload = build_payload(
            make_request(
                messages=(
                    ModelMessage(role=Role.SYSTEM, content="系统"),
                    ModelMessage(role=Role.USER, content="一"),
                    ModelMessage(role=Role.ASSISTANT, content="二"),
                    ModelMessage(role=Role.USER, content="三"),
                ),
                tools=(sample_tool(), sample_tool("shell.exec")),
            ),
            max_output_tokens=1,
            caching=CachingSpec(enabled=True),
        )
        rendered = json.dumps(payload)
        assert rendered.count('"cache_control"') <= 4

    def test_disabled_breakpoints_are_respected(self) -> None:
        payload = build_payload(
            make_request(
                messages=(
                    ModelMessage(role=Role.SYSTEM, content="系统"),
                    ModelMessage(role=Role.USER, content="一"),
                ),
                tools=(sample_tool(),),
            ),
            max_output_tokens=1,
            caching=CachingSpec(enabled=True, system=False, tools=True, history=False),
        )
        assert "cache_control" in payload["tools"][-1]
        assert "cache_control" not in payload["system"][-1]

    def test_caching_off_leaves_no_markers(self) -> None:
        payload = build_payload(
            make_request(messages=(ModelMessage(role=Role.SYSTEM, content="系统"),), tools=(sample_tool(),)),
            max_output_tokens=1,
        )
        assert '"cache_control"' not in json.dumps(payload)

    def test_history_breakpoint_needs_at_least_three_turns(self) -> None:
        """只有两轮时最后一条就是本轮新增的，给它设断点等于每轮都写一份用不上的缓存。"""
        payload = build_payload(
            make_request(
                messages=(
                    ModelMessage(role=Role.USER, content="一"),
                    ModelMessage(role=Role.ASSISTANT, content="二"),
                    ModelMessage(role=Role.USER, content="三"),
                )
            ),
            max_output_tokens=1,
            caching=CachingSpec(enabled=True),
        )
        # 规整之后只剩 user/assistant/user 三轮，`messages[-2]` 才拿得到断点。
        assert len(payload["messages"]) == 3
        assert "cache_control" in payload["messages"][-2]["content"][-1]




# ------------------------------------------ thinking 块的多轮回放（`D45`）


def thinking(text: str = "嗯", signature: str = "sig") -> OpaqueBlock:
    payload: dict[str, str] = {}
    if text:
        payload["thinking"] = text
    if signature:
        payload["signature"] = signature
    return OpaqueBlock(provider="anthropic", kind="thinking", payload=payload)


class TestThinkingReplay:
    """`D45` 补上的能力：thinking 与工具调用现在可以同时用。

    在此之前 `thinking` 块在解码时就被丢掉，续写请求因此缺了 Anthropic 要求原样回传的块，
    直接被拒——那是相对 legacy 的一处真实能力回退，如实记在包 docstring 里。
    """

    def test_thinking_blocks_are_replayed_before_text_and_tool_use(self) -> None:
        """**顺序是行为**：Anthropic 要求 thinking 块排在同一条 assistant 轮的最前面。"""
        _, turns = encode_messages(
            [
                ModelMessage(role=Role.USER, content="算一下"),
                ModelMessage(
                    role=Role.ASSISTANT,
                    content="我查一下",
                    tool_calls=(ToolCall(call_id="c1", name="fs.read", arguments={}),),
                    provider_blocks=(thinking(),),
                ),
                ModelMessage(role=Role.TOOL, content="42", tool_call_id="c1"),
            ]
        )
        assistant = next(turn for turn in turns if turn["role"] == "assistant")
        assert [block["type"] for block in assistant["content"]] == [
            "thinking",
            "text",
            "tool_use",
        ]
        assert assistant["content"][0] == {
            "type": "thinking",
            "thinking": "嗯",
            "signature": "sig",
        }

    def test_a_redacted_block_is_replayed_by_its_data(self) -> None:
        """它没有 `signature`，凭据在 `data` 里；我们连读都读不了，只能原样回传。"""
        block = OpaqueBlock(
            provider="anthropic", kind="redacted_thinking", payload={"data": "opaque=="}
        )
        assert thinking_blocks((block,)) == [{"type": "redacted_thinking", "data": "opaque=="}]

    def test_a_block_from_another_provider_is_skipped(self) -> None:
        """`EDG-305`：切换 Provider 之后同一段历史不该带着上一家的块跑。

        `payload` 的形状是私有的，把别家的 `thinking` 块当成自己的塞进请求体换来一个 400。
        """
        alien = OpaqueBlock(provider="openai", kind="thinking", payload={"signature": "x"})
        assert thinking_blocks((alien,)) == []

    def test_a_thinking_block_without_a_signature_is_skipped(self) -> None:
        """Anthropic 拒绝无签名的思考块。留一半比不留更糟——这正是 `D32` 当初整块丢弃的
        理由，现在它只作用在残缺的块上。"""
        assert thinking_blocks((thinking(signature=""),)) == []

    def test_an_omitted_thinking_block_still_replays_its_signature(self) -> None:
        """`display: "omitted"` 时正文恒为空串。Anthropic 要的是签名，正文空不空与它无关。"""
        assert thinking_blocks((thinking(text=""),)) == [
            {"type": "thinking", "thinking": "", "signature": "sig"}
        ]

    def test_an_unknown_kind_is_skipped(self) -> None:
        """本插件只产出那两种；第三种只可能来自手改的历史或未来版本。"""
        odd = OpaqueBlock(provider="anthropic", kind="future_block", payload={"x": "y"})
        assert thinking_blocks((odd,)) == []

    def test_a_message_without_blocks_encodes_exactly_as_before(self) -> None:
        """绝大多数消息一个 opaque 块都没有。它们的线格式必须与 `D45` 之前逐字相同。"""
        _, turns = encode_messages(
            [
                ModelMessage(role=Role.USER, content="在吗"),
                ModelMessage(role=Role.ASSISTANT, content="在"),
                ModelMessage(role=Role.USER, content="好"),
            ]
        )
        assistant = next(turn for turn in turns if turn["role"] == "assistant")
        assert assistant["content"] == [{"type": "text", "text": "在"}]
