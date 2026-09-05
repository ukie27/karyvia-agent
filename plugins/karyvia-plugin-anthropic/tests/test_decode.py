"""响应侧线格式的验收：`decode.py` 的纯函数与流式行为（开发方案 `D32`）。

| 验收项 | 测试 |
| --- | --- |
| 响应解码；refusal 是正常响应而非异常 | `TestResponseDecoding` |
| 流式增量拼装、用量合并与 `EDG-304` | `TestStreaming` |

流式用例分两种形态：能在 `StreamDecoder` 上直接推事件的，就不起 provider；要验
`EDG-304`、空闲看门狗与「未声明流式即拒绝」的，才走 MockTransport。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from _support import (
    MODEL_ID,
    SENTINEL_KEY,
    collect,
    make_provider,
    make_request,
    message_body,
    sse,
    text_stream,
)
from karyvia_plugin_anthropic import (
    StreamDecoder,
    decode_response,
    decode_stop_reason,
    decode_usage,
)
from karyvia_plugin_anthropic.decode import parse_sse_data
from karyvia_plugin_anthropic.settings import CONFIG_CAPABILITIES_KEY

from karyvia.contracts import (
    ChunkKind,
    ErrorCategory,
    ErrorCode,
    KaryviaError,
    ModelChunk,
    StopReason,
)
from karyvia.sdk.testing import ManualCancel

# ------------------------------------------------------------------------------ 响应解码


class TestResponseDecoding:
    def test_text_blocks_are_joined(self) -> None:
        response = decode_response(
            message_body(content=[{"type": "text", "text": "一"}, {"type": "text", "text": "二"}]),
            model_id=MODEL_ID,
        )
        assert response.content == "一二"
        assert response.stop_reason is StopReason.END_TURN

    def test_tool_use_blocks_decode_with_the_contract_name(self) -> None:
        response = decode_response(
            message_body(
                content=[{"type": "tool_use", "id": "toolu_1", "name": "fs-read", "input": {"path": "a"}}],
                stop_reason="tool_use",
            ),
            model_id=MODEL_ID,
        )
        assert response.stop_reason is StopReason.TOOL_CALLS
        assert response.tool_calls[0].name == "fs.read"
        assert response.tool_calls[0].arguments == {"path": "a"}

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("end_turn", StopReason.END_TURN),
            ("tool_use", StopReason.TOOL_CALLS),
            ("max_tokens", StopReason.MAX_TOKENS),
            ("stop_sequence", StopReason.STOP_SEQUENCE),
            ("refusal", StopReason.CONTENT_FILTER),
        ],
    )
    def test_stop_reason_table(self, raw: str, expected: StopReason) -> None:
        assert decode_stop_reason(raw, has_tool_calls=False) is expected

    def test_stop_sequence_is_reachable_here(self) -> None:
        """内建 `model_openai` 分不出这一档（OpenAI 对两种情况都回 `stop`）。"""
        response = decode_response(
            message_body(stop_reason="stop_sequence", stop_sequence="END"), model_id=MODEL_ID
        )
        assert response.stop_reason is StopReason.STOP_SEQUENCE
        assert response.provider_metadata["stop_sequence"] == "END"

    def test_refusal_is_a_normal_response_not_an_exception(self) -> None:
        """`is_complete_answer` 因此为假，Channel 侧的呈现规则据此区分（`EDG-304`）。"""
        response = decode_response(message_body(stop_reason="refusal"), model_id=MODEL_ID)
        assert response.stop_reason is StopReason.CONTENT_FILTER
        assert response.is_complete_answer is False

    def test_unknown_stop_reason_is_inferred_and_recorded(self) -> None:
        response = decode_response(message_body(stop_reason="pause_turn"), model_id=MODEL_ID)
        assert response.stop_reason is StopReason.END_TURN
        assert response.provider_metadata["raw_stop_reason"] == "pause_turn"

    def test_usage_sums_the_three_input_counters(self) -> None:
        """线格式的 `input_tokens` 只是未命中缓存的余量，不相加会少报一大截。"""
        usage = decode_usage(
            {
                "input_tokens": 10,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 1000,
                "output_tokens": 7,
            }
        )
        assert usage.input_tokens == 1110
        assert usage.cached_input_tokens == 1000
        assert usage.output_tokens == 7

    def test_reasoning_tokens_are_never_guessed(self) -> None:
        """Anthropic 不单独报思考 token；估算出来的数字会被当成实测值。"""
        usage = decode_usage({"input_tokens": 1, "output_tokens": 900})
        assert usage.reasoning_tokens == 0
        assert usage.cost_usd is None

    def test_missing_usage_is_zero_not_an_error(self) -> None:
        assert decode_usage(None).total_tokens == 0

    def test_thinking_blocks_come_out_as_opaque_blocks(self) -> None:
        """`D45`：思考块**不再被丢掉**，连着 `signature` 原样带出来。

        在此之前它们只被计数（`dropped_thinking_blocks`），因此 thinking 与工具调用不能
        同时用——续写请求缺了那些块会被 Anthropic 拒绝。
        """
        response = decode_response(
            message_body(
                content=[
                    {"type": "thinking", "thinking": "嗯", "signature": "sig"},
                    {"type": "redacted_thinking", "data": "..."},
                    {"type": "text", "text": "答案"},
                ]
            ),
            model_id=MODEL_ID,
        )
        assert response.content == "答案"
        assert [block.kind for block in response.provider_blocks] == [
            "thinking",
            "redacted_thinking",
        ]
        # **只保留 Anthropic 自己要回来的字段**，不塞我们的推断。
        assert dict(response.provider_blocks[0].payload) == {"thinking": "嗯", "signature": "sig"}
        assert dict(response.provider_blocks[1].payload) == {"data": "..."}
        # 所有权标记让别家的编码器跳过它们（`EDG-305`）。
        assert all(block.owned_by("anthropic") for block in response.provider_blocks)
        assert response.provider_metadata["thinking_blocks"] == 2

    def test_unknown_blocks_are_counted_not_fatal(self) -> None:
        response = decode_response(
            message_body(content=[{"type": "server_tool_use", "id": "x"}, {"type": "text", "text": "hi"}]),
            model_id=MODEL_ID,
        )
        assert response.provider_metadata["unknown_content_blocks"] == 1

    def test_cache_creation_is_exposed_as_metadata(self) -> None:
        """「缓存到底写进去没有」的唯一观测信号；`TokenUsage` 里没有对应字段。"""
        response = decode_response(
            message_body(usage={"input_tokens": 1, "cache_creation_input_tokens": 42, "output_tokens": 1}),
            model_id=MODEL_ID,
        )
        assert response.provider_metadata["cache_creation_input_tokens"] == 42

    def test_bad_tool_input_is_an_external_error(self) -> None:
        with pytest.raises(KaryviaError) as excinfo:
            decode_response(
                message_body(content=[{"type": "tool_use", "id": "t", "name": "fs-read", "input": "不是对象"}]),
                model_id=MODEL_ID,
            )
        assert excinfo.value.code is ErrorCode.EXTERNAL_MODEL_PROVIDER

    def test_a_bad_tool_name_does_not_echo_arguments(self) -> None:
        """参数是模型生成的自由文本，可能带着它从上下文里抄来的凭据。"""
        with pytest.raises(KaryviaError) as excinfo:
            decode_response(
                message_body(
                    content=[{"type": "tool_use", "id": "t", "name": "NOT-A-VALID-NAME", "input": {"secret": SENTINEL_KEY}}]
                ),
                model_id=MODEL_ID,
            )
        assert SENTINEL_KEY not in repr(excinfo.value)

    def test_request_id_is_recorded(self) -> None:
        response = decode_response(message_body(), model_id=MODEL_ID, request_id="req_01")
        assert response.provider_metadata["request_id"] == "req_01"


# ------------------------------------------------------------------------------ 流式


class TestStreaming:
    def test_only_data_lines_are_parsed(self) -> None:
        assert parse_sse_data("event: message_start") is None
        assert parse_sse_data("data: {}") == "{}"
        assert parse_sse_data("") is None

    async def test_text_stream_ends_with_exactly_one_done(self) -> None:
        provider = make_provider(
            lambda request: httpx.Response(200, text=text_stream("你好"))
        )
        chunks = await collect(provider, make_request(stream=True))
        assert [chunk.kind for chunk in chunks] == [ChunkKind.TEXT, ChunkKind.USAGE, ChunkKind.DONE]
        assert chunks[0].text == "你好"
        assert chunks[-1].stop_reason is StopReason.END_TURN

    async def test_thinking_deltas_become_reasoning_chunks(self) -> None:
        stream = sse(
            [
                {"type": "message_start", "message": {"id": "m", "model": MODEL_ID, "usage": {"input_tokens": 1}}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "推理"}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}},
                {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "答案"}},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
            ]
        )
        provider = make_provider(lambda request: httpx.Response(200, text=stream))
        chunks = await collect(provider, make_request(stream=True))
        kinds = [chunk.kind for chunk in chunks]
        assert kinds[:2] == [ChunkKind.REASONING, ChunkKind.TEXT]
        # 推理**文本**给人看（进事件流不进答案），签名给 Anthropic——两条独立通路，同一段
        # 思考在两处各出现一次是刻意的。
        assert all(chunk.text != "sig" for chunk in chunks)
        # `D45`：`signature_delta` 不再被吞掉，它随收尾的 OPAQUE 分片出来。
        opaque = [chunk for chunk in chunks if chunk.kind is ChunkKind.OPAQUE]
        assert len(opaque) == 1
        assert opaque[0].block is not None
        assert dict(opaque[0].block.payload) == {"thinking": "推理", "signature": "sig"}
        assert opaque[0].block.kind == "thinking"

    def test_tool_use_arguments_survive_any_split(self) -> None:
        """分片切在任意字节边界上，拼完才能解析。"""
        raw = '{"path": "a.txt", "limit": 12}'
        for cut in range(1, len(raw)):
            decoder = StreamDecoder()
            decoder.push(
                {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "fs-read"}}
            )
            decoder.push({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": raw[:cut]}})
            decoder.push({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": raw[cut:]}})
            decoder.push({"type": "message_delta", "delta": {"stop_reason": "tool_use"}})
            chunks = decoder.finish()
            assert chunks[0].tool_call.arguments == {"path": "a.txt", "limit": 12}
            assert chunks[0].tool_call.name == "fs.read"

    def test_interleaved_parallel_tool_calls_are_keyed_by_index(self) -> None:
        """`id` 与 `name` 只在首帧出现，交错到达时 `index` 是唯一站得住的相关性。"""
        decoder = StreamDecoder()
        for index, name in ((0, "fs-read"), (1, "shell-exec")):
            decoder.push(
                {"type": "content_block_start", "index": index, "content_block": {"type": "tool_use", "id": f"t{index}", "name": name}}
            )
        decoder.push({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"cmd":'}})
        decoder.push({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"path":'}})
        decoder.push({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '"ls"}'}})
        decoder.push({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '"a"}'}})
        decoder.push({"type": "message_delta", "delta": {"stop_reason": "tool_use"}})
        chunks = decoder.finish()
        calls = [chunk.tool_call for chunk in chunks if chunk.kind is ChunkKind.TOOL_CALL]
        assert [call.name for call in calls] == ["fs.read", "shell.exec"]
        assert calls[0].arguments == {"path": "a"}
        assert calls[1].arguments == {"cmd": "ls"}

    def test_usage_merges_message_start_and_message_delta(self) -> None:
        """输入侧只在 `message_start` 出现一次，输出侧在 `message_delta` 里是累计值。"""
        decoder = StreamDecoder()
        decoder.push(
            {"type": "message_start", "message": {"usage": {"input_tokens": 5, "cache_read_input_tokens": 50}}}
        )
        decoder.push({"type": "message_delta", "delta": {}, "usage": {"output_tokens": 3}})
        decoder.push({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 9}})
        usage = next(chunk.usage for chunk in decoder.finish() if chunk.kind is ChunkKind.USAGE)
        assert usage.input_tokens == 55
        assert usage.cached_input_tokens == 50
        assert usage.output_tokens == 9

    async def test_an_error_event_after_content_yields_done_error_first(self) -> None:
        """`EDG-304`：没有它，消费方分不清「流干净结束」和「流断在半截」。"""
        stream = sse(
            [
                {"type": "message_start", "message": {"id": "m", "model": MODEL_ID}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "半句"}},
                {"type": "error", "error": {"type": "overloaded_error"}},
            ]
        )
        provider = make_provider(lambda request: httpx.Response(200, text=stream))
        seen: list[ModelChunk] = []
        with pytest.raises(KaryviaError) as excinfo:
            async for chunk in provider.stream(make_request(stream=True), ManualCancel()):
                seen.append(chunk)
        assert seen[0].kind is ChunkKind.TEXT
        assert seen[-1].kind is ChunkKind.DONE
        assert seen[-1].stop_reason is StopReason.ERROR
        assert excinfo.value.retryable is True

    async def test_an_error_before_any_chunk_does_not_emit_done(self) -> None:
        stream = sse([{"type": "error", "error": {"type": "invalid_request_error"}}])
        provider = make_provider(lambda request: httpx.Response(200, text=stream))
        seen: list[ModelChunk] = []
        with pytest.raises(KaryviaError):
            async for chunk in provider.stream(make_request(stream=True), ManualCancel()):
                seen.append(chunk)
        assert seen == []

    async def test_streaming_is_refused_when_undeclared(self) -> None:
        """`MOD-005`：缺席即报缺失，不静默降级成一次性返回。"""
        provider = make_provider(
            lambda request: httpx.Response(200, json=message_body()),
            **{CONFIG_CAPABILITIES_KEY: ["tool_calls"]},
        )
        with pytest.raises(KaryviaError) as excinfo:
            await collect(provider, make_request(stream=True))
        assert excinfo.value.category is ErrorCategory.CAPABILITY_MISSING

    async def test_a_stalled_stream_times_out(self) -> None:
        """请求级超时保护不了「开了口就不再吐字」的流。"""

        async def never_ends():
            yield b'data: {"type": "message_start", "message": {}}\n\n'
            await asyncio.sleep(10)
            yield b""

        provider = make_provider(
            lambda request: httpx.Response(200, content=never_ends()),
            stream_idle_timeout_ms=50,
        )
        with pytest.raises(KaryviaError) as excinfo:
            await collect(provider, make_request(stream=True))
        assert excinfo.value.code is ErrorCode.TIMEOUT_MODEL_REQUEST
        assert excinfo.value.retryable is True




class TestStreamingThinkingBlocks:
    """流式下 thinking 块的三处到达（`D45`）：块类型、正文、签名。"""

    def test_opaque_chunks_come_first_and_in_index_order(self) -> None:
        """**顺序在这里就摆对**：`StreamFolder` 按到达顺序累积，而 Anthropic 要求续写时
        thinking 块保持原序排在最前面。`wire.encode_messages` 那边因此不再排一次。"""
        decoder = StreamDecoder()
        for index in (1, 0):
            decoder.push(
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "thinking", "thinking": ""},
                }
            )
            decoder.push(
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "signature_delta", "signature": f"sig{index}"},
                }
            )
        decoder.push({"type": "message_delta", "delta": {"stop_reason": "end_turn"}})

        chunks = decoder.finish()
        opaque = [chunk for chunk in chunks if chunk.kind is ChunkKind.OPAQUE]
        assert list(chunks[:2]) == opaque, "opaque 分片必须排在最前面"
        assert [chunk.block.payload["signature"] for chunk in opaque] == ["sig0", "sig1"]

    def test_a_redacted_block_arrives_whole_in_content_block_start(self) -> None:
        """它没有 delta——全部内容都在起始帧的 `data` 里。"""
        decoder = StreamDecoder()
        decoder.push(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "redacted_thinking", "data": "opaque=="},
            }
        )
        decoder.push({"type": "message_delta", "delta": {"stop_reason": "end_turn"}})

        block = decoder.finish()[0].block
        assert block is not None
        assert block.kind == "redacted_thinking"
        assert dict(block.payload) == {"data": "opaque=="}

    def test_an_omitted_thinking_block_still_produces_its_signature(self) -> None:
        """`display: "omitted"` 时正文恒为空串，但块本身仍然要建——Anthropic 要的是签名。"""
        decoder = StreamDecoder()
        decoder.push(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": ""},
            }
        )
        decoder.push(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": ""},
            }
        )
        decoder.push(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "sig"},
            }
        )
        decoder.push({"type": "message_delta", "delta": {"stop_reason": "end_turn"}})

        block = decoder.finish()[0].block
        assert block is not None
        assert dict(block.payload) == {"signature": "sig"}

    def test_a_signature_without_a_block_start_is_still_kept(self) -> None:
        """容错：中转吞掉了 `content_block_start` 时签名仍然该留住。"""
        decoder = StreamDecoder()
        decoder.push(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "sig"},
            }
        )
        decoder.push({"type": "message_delta", "delta": {"stop_reason": "end_turn"}})
        block = decoder.finish()[0].block
        assert block is not None and block.kind == "thinking"

    def test_a_stream_without_thinking_produces_no_opaque_chunks(self) -> None:
        """绝大多数流一个 opaque 分片都没有。它们必须与 `D45` 之前逐字相同。"""
        decoder = StreamDecoder()
        decoder.push(
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "好"}}
        )
        decoder.push({"type": "message_delta", "delta": {"stop_reason": "end_turn"}})
        assert [chunk.kind for chunk in decoder.finish()] == [ChunkKind.DONE]
        assert "thinking_blocks" not in decoder.metadata
