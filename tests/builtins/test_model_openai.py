"""内建 Model Provider `model_openai` 的验收（开发方案 `D19`）。

| 验收项 | 测试 |
| --- | --- |
| 通过 `ModelProviderContract` 全部用例 | `TestOpenAIModelProvider` |
| 请求体符合 OpenAI 线格式 | `TestRequestEncoding` |
| 响应解码，内容过滤是正常响应而非异常 | `TestResponseDecoding` |
| 流式 tool_call 增量拼装与 `EDG-304` | `TestStreaming` |
| 四类错误映射到正确的 `ErrorCategory` 且 `retryable` 正确（`MOD-003`） | `TestFaultMapping` |
| 认证信息不进日志与事件（`MOD-002`） | `TestCredentialNeverLeaks` |
| 取消语义 | `TestCancellation` |
| 配置校验在 `setup()` 时发生 | `TestSettings` |
| 内建以普通 manifest + `setup(api)` 注册（`BAS-005`） | `TestRegistration` |

三条写这些用例时的取舍：

- **全部走 `httpx.MockTransport`，一个 socket 都不开。** `tests/builtins/conftest.py` 的
  autouse 闸门是这件事的断言而不是补充手段——`ModelProviderContract` 会不带参数地反复
  构造 provider 并真的发起 `complete()` / `stream()`，没有注入口就会打到真实网络。
- **线格式的断言落在 `wire.py` 的纯函数上，行为的断言落在 provider 上。** 前者能逐字节
  对照且不需要事件循环，后者才需要 transport。混在一起会让「payload 里少了一个键」这种
  失败在一堆异步栈里冒出来。
- **哨兵密钥必须匹配 `contracts/errors.py::_SECRET_VALUE_PATTERNS`**（`sk-` 后至少 16 字符），
  否则「没泄漏」可能只是因为它压根不长得像密钥，那样的用例证明不了任何事。
"""

from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from typing import Final

import httpx
import pytest

from karyvia.builtins import model_openai as model_openai_module
from karyvia.builtins.model_openai import (
    AUTH_MODES,
    CAPABILITY_NAME,
    CHAT_COMPLETIONS_PATH,
    CONFIG_AUTH_KEY,
    CONFIG_BASE_URL_KEY,
    CONFIG_CAPABILITIES_KEY,
    CONFIG_DEFAULT_CONTEXT_WINDOW_KEY,
    CONFIG_DEFAULT_MAX_OUTPUT_KEY,
    CONFIG_INCLUDE_USAGE_KEY,
    CONFIG_MAX_TOKENS_FIELD_KEY,
    CONFIG_MODELS_KEY,
    CONFIG_REQUEST_TIMEOUT_KEY,
    CONFIG_STREAM_IDLE_TIMEOUT_KEY,
    CONFIG_SUPPORTS_TEMPERATURE_KEY,
    MODEL_ENTRY_KEYS,
    PROVIDER_NAME,
    SECRET_NAME,
    OpenAIModelProvider,
    build_payload,
    decode_response,
    decode_usage,
    encode_messages,
    error_for_status,
    error_for_transport,
    is_local_endpoint,
    parse_sse_data,
    resolve_settings,
    retry_after_ms,
    setup,
    strip_lone_surrogates,
)
from karyvia.builtins.registry import BUILTIN_MANIFESTS, MODEL_OPENAI
from karyvia.contracts import (
    Builtin,
    CapabilityKind,
    ChunkKind,
    ErrorCategory,
    ErrorCode,
    JsonValue,
    KaryviaError,
    ModelCapability,
    ModelChunk,
    ModelMessage,
    ModelRequest,
    ProviderId,
    Role,
    SamplingParams,
    SecretStr,
    StopReason,
    ToolCall,
    ToolSpec,
)
from karyvia.kernel.observability import prepare_payload
from karyvia.kernel.plugins import model_providers_from
from karyvia.runtime.wiring import wire_capabilities
from karyvia.sdk import PluginContext
from karyvia.sdk.testing import (
    FakePluginContext,
    ManualCancel,
    ModelProviderContract,
    make_correlation,
)

MODEL_ID: Final = "gpt-4o-mini"
BASE_URL: Final = "https://api.example.test/v1"

#: 形状必须匹配 `_SECRET_VALUE_PATTERNS` 里的 `sk-[A-Za-z0-9_-]{16,}`，否则脱敏根本不会
#: 认出它，「没泄漏」就变成了一句同义反复。
SENTINEL_KEY: Final = "sk-ThisMustNeverLeak0123456789"

# ------------------------------------------------------------------------------ 夹具


def make_context(**config: JsonValue) -> FakePluginContext:
    """一个带哨兵凭据的 ctx。默认端点是可控的测试域名。"""
    payload: dict[str, JsonValue] = {CONFIG_BASE_URL_KEY: BASE_URL}
    payload.update(config)
    return FakePluginContext(
        config=payload, secrets={SECRET_NAME: SENTINEL_KEY}
    )


def chat_body(
    *,
    content: str = "pong",
    finish_reason: str = "stop",
    tool_calls: Sequence[JsonValue] = (),
    usage: Mapping[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    message: dict[str, JsonValue] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = list(tool_calls)
    body: dict[str, JsonValue] = {
        "id": "chatcmpl-1",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    if usage is not None:
        body["usage"] = dict(usage)
    return body


def sse(events: Sequence[Mapping[str, JsonValue]], *, done: bool = True) -> str:
    lines = [f"data: {json.dumps(dict(event))}\n\n" for event in events]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines)


def make_provider(
    handler: Callable[[httpx.Request], httpx.Response],
    **config: JsonValue,
) -> OpenAIModelProvider:
    """把一个 MockTransport 接到真的 provider 上。"""
    ctx = make_context(**config)
    settings = resolve_settings(ctx)
    return OpenAIModelProvider(
        settings,
        credential=SecretStr(SENTINEL_KEY),
        transport=httpx.MockTransport(handler),
    )


def ok_handler(body: Mapping[str, JsonValue] | None = None) -> Callable[[httpx.Request], httpx.Response]:
    payload = dict(body) if body is not None else chat_body()
    return lambda request: httpx.Response(200, json=payload)


def make_request(
    *,
    stream: bool = False,
    messages: Sequence[ModelMessage] = (),
    tools: Sequence[ToolSpec] = (),
    params: SamplingParams | None = None,
) -> ModelRequest:
    return ModelRequest(
        model_id=MODEL_ID,
        messages=tuple(messages) or (ModelMessage(role=Role.USER, content="ping"),),
        correlation=make_correlation(),
        tools=tuple(tools),
        params=params or SamplingParams(),
        stream=stream,
    )


async def collect(
    provider: OpenAIModelProvider,
    request: ModelRequest,
    cancel: ManualCancel | None = None,
) -> list[ModelChunk]:
    return [chunk async for chunk in provider.stream(request, cancel or ManualCancel())]


# ------------------------------------------------------------------------------ 契约基类


class TestOpenAIModelProvider(ModelProviderContract):
    """`ModelProvider` 的通用契约。每个用例都真的发一次请求，走 MockTransport。"""

    def make_provider(self) -> OpenAIModelProvider:
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            if payload.get("stream"):
                return httpx.Response(
                    200,
                    text=sse([{"choices": [{"delta": {"content": "pong"}, "finish_reason": "stop"}]}]),
                    headers={"content-type": "text/event-stream"},
                )
            return httpx.Response(200, json=chat_body())

        return make_provider(handler)

    def model_id(self) -> str:
        return MODEL_ID


# ------------------------------------------------------------------------------ 请求编码


class TestRequestEncoding:
    """线格式的三条硬约束与采样参数。发错就是 400，不是风格问题。"""

    def test_assistant_with_tool_calls_sends_null_content(self) -> None:
        """多个兼容网关拒绝「正文与 tool_calls 同时非空」的 assistant 消息。"""
        call = ToolCall(call_id="c1", name="fs.read", arguments={"path": "a.txt"})
        encoded = encode_messages(
            (ModelMessage(role=Role.ASSISTANT, content="思考中", tool_calls=(call,)),)
        )
        assert encoded[0]["content"] is None
        assert encoded[0]["tool_calls"][0]["id"] == "c1"

    def test_tool_call_arguments_are_always_an_object_string(self) -> None:
        """契约里 arguments 是 Mapping，线格式要字符串；空参数的地板值是 "{}"。"""
        call = ToolCall(call_id="c1", name="fs.list")
        encoded = encode_messages((ModelMessage(role=Role.ASSISTANT, tool_calls=(call,)),))
        assert encoded[0]["tool_calls"][0]["function"]["arguments"] == "{}"

    def test_tool_messages_carry_their_call_id(self) -> None:
        encoded = encode_messages(
            (ModelMessage(role=Role.TOOL, content="done", tool_call_id="c1"),)
        )
        assert encoded[0] == {"role": "tool", "content": "done", "tool_call_id": "c1"}

    def test_system_and_user_messages_round_trip(self) -> None:
        encoded = encode_messages(
            (
                ModelMessage(role=Role.SYSTEM, content="你是助手"),
                ModelMessage(role=Role.USER, content="你好"),
            )
        )
        assert [item["role"] for item in encoded] == ["system", "user"]

    def test_no_tools_means_neither_tools_nor_tool_choice(self) -> None:
        """只发 tool_choice 会让若干网关直接 400，因此两个键一起省。"""
        payload = build_payload(make_request(), default_max_output_tokens=100)
        assert "tools" not in payload
        assert "tool_choice" not in payload

    def test_tools_are_sent_with_their_json_schema(self) -> None:
        spec = ToolSpec(
            name="fs.read",
            description="读文件",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
        )
        payload = build_payload(make_request(tools=(spec,)), default_max_output_tokens=100)
        assert payload["tool_choice"] == "auto"
        assert payload["tools"][0]["function"]["parameters"] == spec.parameters

    def test_max_completion_tokens_replaces_max_tokens(self) -> None:
        """gpt-5、o1/o3/o4 只认后者，发错就是 400。"""
        payload = build_payload(
            make_request(), max_tokens_field="max_completion_tokens", default_max_output_tokens=77
        )
        assert payload["max_completion_tokens"] == 77
        assert "max_tokens" not in payload

    def test_temperature_is_omitted_not_clamped(self) -> None:
        """推理模型对 temperature 直接 400，而替用户挑一个温度是在改它的采样行为。"""
        request = make_request(params=SamplingParams(temperature=0.7))
        payload = build_payload(
            request, supports_temperature=False, default_max_output_tokens=100
        )
        assert "temperature" not in payload
        assert build_payload(request, default_max_output_tokens=100)["temperature"] == 0.7

    def test_sampling_params_reach_the_wire(self) -> None:
        request = make_request(
            params=SamplingParams(top_p=0.9, stop_sequences=("END",), seed=7, max_output_tokens=64)
        )
        payload = build_payload(request, default_max_output_tokens=100)
        assert payload["top_p"] == 0.9
        assert payload["stop"] == ["END"]
        assert payload["seed"] == 7
        assert payload["max_tokens"] == 64

    def test_stream_options_are_opt_in(self) -> None:
        """默认关：不少兼容端点会对未知字段 400。"""
        plain = build_payload(make_request(), default_max_output_tokens=100, stream=True)
        assert "stream_options" not in plain
        opted = build_payload(
            make_request(), default_max_output_tokens=100, stream=True, include_usage=True
        )
        assert opted["stream_options"] == {"include_usage": True}

    def test_lone_surrogates_are_stripped_before_serialization(self) -> None:
        """留着它们，httpx 会在编码请求体时抛 UnicodeEncodeError，整轮对话因为一段
        Windows 控制台粘贴文本而失败，且错误信息指不到原因。"""
        dirty = "你好\ud800世界"
        assert strip_lone_surrogates(dirty) == "你好世界"
        encoded = encode_messages((ModelMessage(role=Role.USER, content=dirty),))
        assert encoded[0]["content"].encode("utf-8")

    def test_clean_text_passes_through_unchanged(self) -> None:
        text = "普通文本 with emoji 🎉 and 中文"
        assert strip_lone_surrogates(text) == text


# ------------------------------------------------------------------------------ 响应解码


class TestResponseDecoding:
    def test_plain_text_response(self) -> None:
        response = decode_response(chat_body(content="你好"), model_id=MODEL_ID)
        assert response.content == "你好"
        assert response.stop_reason is StopReason.END_TURN
        assert response.is_complete_answer

    @pytest.mark.parametrize(
        ("finish_reason", "expected"),
        [
            ("stop", StopReason.END_TURN),
            ("length", StopReason.MAX_TOKENS),
            ("content_filter", StopReason.CONTENT_FILTER),
            ("function_call", StopReason.TOOL_CALLS),
        ],
        ids=["自然结束", "长度截断", "内容过滤", "旧式函数调用"],
    )
    def test_finish_reasons_map_to_stop_reasons(
        self, finish_reason: str, expected: StopReason
    ) -> None:
        calls: Sequence[JsonValue] = (
            [{"id": "c1", "function": {"name": "fs.read", "arguments": "{}"}}]
            if expected is StopReason.TOOL_CALLS
            else []
        )
        response = decode_response(
            chat_body(finish_reason=finish_reason, tool_calls=calls), model_id=MODEL_ID
        )
        assert response.stop_reason is expected

    def test_content_filter_is_a_normal_response_not_an_exception(self) -> None:
        """`EDG-304`：过滤掉的输出不是完整答案，但它也不是一次失败的调用。"""
        response = decode_response(
            chat_body(content="", finish_reason="content_filter"), model_id=MODEL_ID
        )
        assert response.stop_reason is StopReason.CONTENT_FILTER
        assert not response.is_complete_answer

    def test_parallel_tool_calls_are_decoded(self) -> None:
        body = chat_body(
            finish_reason="tool_calls",
            tool_calls=[
                {"id": "c1", "function": {"name": "fs.read", "arguments": '{"path": "a"}'}},
                {"id": "c2", "function": {"name": "fs.list", "arguments": "{}"}},
            ],
        )
        response = decode_response(body, model_id=MODEL_ID)
        assert [call.call_id for call in response.tool_calls] == ["c1", "c2"]
        assert response.tool_calls[0].arguments == {"path": "a"}

    def test_duplicate_call_ids_are_repaired_and_recorded(self) -> None:
        """`ModelResponse` 对重复 call_id 直接抛错，而部分端点真的会这么发。
        补救本身没问题，**悄悄补救**才有问题——所以它必须留在 provider_metadata 里。"""
        body = chat_body(
            finish_reason="tool_calls",
            tool_calls=[
                {"id": "same", "function": {"name": "fs.read", "arguments": "{}"}},
                {"id": "same", "function": {"name": "fs.list", "arguments": "{}"}},
            ],
        )
        response = decode_response(body, model_id=MODEL_ID)
        assert len({call.call_id for call in response.tool_calls}) == 2
        assert response.provider_metadata["repaired_tool_call_ids"] == 1

    def test_missing_call_id_is_repaired(self) -> None:
        body = chat_body(
            finish_reason="tool_calls",
            tool_calls=[{"function": {"name": "fs.read", "arguments": "{}"}}],
        )
        response = decode_response(body, model_id=MODEL_ID)
        assert response.tool_calls[0].call_id
        assert response.provider_metadata["repaired_tool_call_ids"] == 1

    def test_a_tool_name_that_violates_the_contract_is_reported_not_normalized(self) -> None:
        """不做大小写规整——那是替模型改主意。`detail` 只放名字，不回显参数。"""
        body = chat_body(
            finish_reason="tool_calls",
            tool_calls=[{"id": "c1", "function": {"name": "FS-Read!", "arguments": "{}"}}],
        )
        with pytest.raises(KaryviaError) as caught:
            decode_response(body, model_id=MODEL_ID)
        assert caught.value.category is ErrorCategory.EXTERNAL_SERVICE

    def test_unparsable_arguments_fail_loudly(self) -> None:
        """`json_repair` 是仓库依赖，但用它意味着拿一份猜出来的参数去产生真实副作用。"""
        body = chat_body(
            finish_reason="tool_calls",
            tool_calls=[{"id": "c1", "function": {"name": "fs.read", "arguments": "{not json"}}],
        )
        with pytest.raises(KaryviaError) as caught:
            decode_response(body, model_id=MODEL_ID)
        assert caught.value.code is ErrorCode.EXTERNAL_MODEL_PROVIDER

    @pytest.mark.parametrize(
        "usage",
        [
            {"prompt_tokens": 10, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 4}},
            {"prompt_tokens": 10, "completion_tokens": 5, "cached_tokens": 4},
            {"prompt_tokens": 10, "completion_tokens": 5, "prompt_cache_hit_tokens": 4},
        ],
        ids=["details.cached_tokens", "顶层 cached_tokens", "prompt_cache_hit_tokens"],
    )
    def test_all_three_cached_token_spellings_are_understood(
        self, usage: Mapping[str, JsonValue]
    ) -> None:
        """少认一种就是把 prompt_caching 的观测信号丢掉。"""
        decoded = decode_usage({"usage": dict(usage)})
        assert decoded.input_tokens == 10
        assert decoded.output_tokens == 5
        assert decoded.cached_input_tokens == 4

    def test_missing_usage_is_zero_not_an_error(self) -> None:
        assert decode_usage({}).total_tokens == 0

    def test_a_malformed_body_is_an_external_error(self) -> None:
        with pytest.raises(KaryviaError) as caught:
            decode_response({"choices": []}, model_id=MODEL_ID)
        assert caught.value.category is ErrorCategory.EXTERNAL_SERVICE

    def test_provider_metadata_only_carries_normalized_json(self) -> None:
        body = chat_body()
        body["system_fingerprint"] = "fp_1"
        response = decode_response(body, model_id=MODEL_ID)
        assert response.provider_metadata["id"] == "chatcmpl-1"
        assert response.provider_metadata["system_fingerprint"] == "fp_1"


# ------------------------------------------------------------------------------ 流式


class TestStreaming:
    async def test_text_deltas_are_emitted_then_a_single_done(self) -> None:
        stream = sse(
            [
                {"choices": [{"delta": {"content": "你"}}]},
                {"choices": [{"delta": {"content": "好"}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ]
        )
        provider = make_provider(lambda _: httpx.Response(200, text=stream))
        chunks = await collect(provider, make_request(stream=True))
        assert [c.text for c in chunks if c.kind is ChunkKind.TEXT] == ["你", "好"]
        assert [c.kind for c in chunks].count(ChunkKind.DONE) == 1
        assert chunks[-1].stop_reason is StopReason.END_TURN

    async def test_tool_calls_are_assembled_by_index(self) -> None:
        """id 与 name 只在首片出现，arguments 被切碎；index 是唯一稳定的相关性。"""
        stream = sse(
            [
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "id": "c1", "function": {"name": "fs.read", "arguments": ""}},
                    {"index": 1, "id": "c2", "function": {"name": "fs.list", "arguments": ""}},
                ]}}]},
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": '{"pa'}},
                    {"index": 1, "function": {"arguments": "{}"}},
                ]}}]},
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": 'th": "a.txt"}'}},
                ]}}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ]
        )
        provider = make_provider(lambda _: httpx.Response(200, text=stream))
        chunks = await collect(provider, make_request(stream=True))
        calls = [c.tool_call for c in chunks if c.kind is ChunkKind.TOOL_CALL]
        assert [call.call_id for call in calls] == ["c1", "c2"]
        assert calls[0].name == "fs.read"
        assert calls[0].arguments == {"path": "a.txt"}
        assert calls[1].arguments == {}
        assert chunks[-1].stop_reason is StopReason.TOOL_CALLS

    async def test_an_empty_arguments_fragment_does_not_reset_state(self) -> None:
        """有端点在首片发 `"arguments": ""`。用 `is not None` 判空会把已累积的内容清掉。"""
        stream = sse(
            [
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "id": "c1", "function": {"name": "fs.read", "arguments": '{"a": 1}'}},
                ]}}]},
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": ""}},
                ]}}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ]
        )
        provider = make_provider(lambda _: httpx.Response(200, text=stream))
        chunks = await collect(provider, make_request(stream=True))
        call = next(c.tool_call for c in chunks if c.kind is ChunkKind.TOOL_CALL)
        assert call.arguments == {"a": 1}

    async def test_a_missing_index_falls_back_to_array_position(self) -> None:
        """非规范网关会漏 index，此时唯一还站得住的相关性就是数组顺序。"""
        stream = sse(
            [
                {"choices": [{"delta": {"tool_calls": [
                    {"id": "c1", "function": {"name": "fs.read", "arguments": "{}"}},
                    {"id": "c2", "function": {"name": "fs.list", "arguments": "{}"}},
                ]}}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ]
        )
        provider = make_provider(lambda _: httpx.Response(200, text=stream))
        chunks = await collect(provider, make_request(stream=True))
        assert [c.tool_call.call_id for c in chunks if c.kind is ChunkKind.TOOL_CALL] == ["c1", "c2"]

    async def test_the_trailing_usage_chunk_has_no_choices(self) -> None:
        """开了 include_usage 时收尾片的 choices 是空数组，finish_reason 因此不在最后一片。"""
        stream = sse(
            [
                {"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 1}},
            ]
        )
        provider = make_provider(
            lambda _: httpx.Response(200, text=stream), **{CONFIG_INCLUDE_USAGE_KEY: True}
        )
        chunks = await collect(provider, make_request(stream=True))
        usage = next(c.usage for c in chunks if c.kind is ChunkKind.USAGE)
        assert usage.input_tokens == 3
        assert chunks[-1].stop_reason is StopReason.END_TURN

    async def test_a_stream_that_fails_midway_emits_done_error_first(self) -> None:
        """`EDG-304`：没有这个 DONE(ERROR)，消费方分不清「流干净结束了」与「断在半截」，
        一份残缺的回答会被当成完整答案。"""
        stream = "data: " + json.dumps({"choices": [{"delta": {"content": "半"}}]}) + "\n\ndata: {not json\n\n"
        provider = make_provider(lambda _: httpx.Response(200, text=stream))
        seen: list[ModelChunk] = []
        with pytest.raises(KaryviaError):
            async for chunk in provider.stream(make_request(stream=True), ManualCancel()):
                seen.append(chunk)
        assert seen[0].text == "半"
        assert seen[-1].kind is ChunkKind.DONE
        assert seen[-1].stop_reason is StopReason.ERROR

    async def test_an_http_error_before_any_chunk_does_not_emit_done(self) -> None:
        """一片都没吐过就失败，没有「已产生的内容」需要收尾。"""
        provider = make_provider(lambda _: httpx.Response(500, json={}))
        seen: list[ModelChunk] = []
        with pytest.raises(KaryviaError):
            async for chunk in provider.stream(make_request(stream=True), ManualCancel()):
                seen.append(chunk)
        assert seen == []

    async def test_streaming_requires_the_declared_capability(self) -> None:
        """没声明就必须报缺失，不得自行降级为一次性返回（`MOD-005`）。"""
        provider = make_provider(
            ok_handler(), **{CONFIG_CAPABILITIES_KEY: ["tool_calls"]}
        )
        with pytest.raises(KaryviaError) as caught:
            await collect(provider, make_request(stream=True))
        assert caught.value.category is ErrorCategory.CAPABILITY_MISSING

    def test_sse_parsing_ignores_comments_and_blank_lines(self) -> None:
        assert parse_sse_data("data: {}") == "{}"
        assert parse_sse_data(": keep-alive") is None
        assert parse_sse_data("") is None
        assert parse_sse_data("event: message") is None


# ------------------------------------------------------------------------------ 错误映射


class TestFaultMapping:
    """`MOD-003` 的四类错误。逐条断言 `ErrorCategory` 与 `retryable`。"""

    def test_401_is_a_missing_credential(self) -> None:
        """与 `ctx.secret()` 在凭据缺失时抛的是同一个码——用户看到的是同一件事。"""
        error = error_for_status(401, body={"error": {"code": "invalid_api_key"}})
        assert error.code is ErrorCode.CONFIG_SECRET_MISSING
        assert error.category is ErrorCategory.CONFIG
        assert not error.retryable

    def test_403_is_permission_denied(self) -> None:
        error = error_for_status(403)
        assert error.category is ErrorCategory.PERMISSION_DENIED
        assert not error.retryable

    def test_429_rate_limit_is_retryable(self) -> None:
        error = error_for_status(429, body={"error": {"code": "rate_limit_exceeded"}})
        assert error.category is ErrorCategory.EXTERNAL_SERVICE
        assert error.retryable

    def test_429_quota_exhaustion_is_not_retryable(self) -> None:
        """撞限速等一会儿就好，欠费重试一万次也不会好，而两者都是 429。"""
        error = error_for_status(429, body={"error": {"code": "insufficient_quota"}})
        assert error.category is ErrorCategory.EXTERNAL_SERVICE
        assert not error.retryable

    def test_an_unknown_429_defaults_to_retryable(self) -> None:
        assert error_for_status(429, body={}).retryable

    def test_a_flat_error_body_is_understood(self) -> None:
        """兼容网关常发扁平的 {"code": …} 而不是 OpenAI 标准的 {"error": {...}}。"""
        error = error_for_status(429, body={"code": "insufficient_quota"})
        assert not error.retryable

    @pytest.mark.parametrize("status", [500, 502, 503, 408, 409], ids=str)
    def test_transient_statuses_are_retryable(self, status: int) -> None:
        assert error_for_status(status).retryable

    @pytest.mark.parametrize("status", [400, 404, 422], ids=str)
    def test_other_client_errors_are_not_retryable(self, status: int) -> None:
        """重试一次坏请求只是再错一次。"""
        assert not error_for_status(status).retryable

    def test_context_overflow_has_a_semantic_error_code(self) -> None:
        error = error_for_status(
            400,
            body={"error": {"type": "invalid_request_error", "code": "context_length_exceeded"}},
        )
        assert error.code is ErrorCode.EXTERNAL_MODEL_CONTEXT_OVERFLOW
        assert not error.retryable

    def test_request_entity_too_large_uses_the_same_recovery_code(self) -> None:
        assert error_for_status(413).code is ErrorCode.EXTERNAL_MODEL_CONTEXT_OVERFLOW

    def test_timeouts_map_to_the_timeout_category(self) -> None:
        error = error_for_transport(httpx.ReadTimeout("slow"))
        assert error.code is ErrorCode.TIMEOUT_MODEL_REQUEST
        assert error.category is ErrorCategory.TIMEOUT
        assert error.retryable

    def test_connection_failures_are_external_and_retryable(self) -> None:
        error = error_for_transport(httpx.ConnectError("refused"))
        assert error.category is ErrorCategory.EXTERNAL_SERVICE
        assert error.retryable

    def test_the_provider_message_never_reaches_detail(self) -> None:
        """那段自由文本会回显用户的 prompt，也可能带着被原样 echo 回来的凭据。"""
        body = {"error": {"code": "invalid_api_key", "message": f"bad key {SENTINEL_KEY}"}}
        error = error_for_status(401, body=body)
        assert SENTINEL_KEY not in repr(error.detail)
        assert error.detail["code"] == "invalid_api_key"

    @pytest.mark.parametrize(
        ("headers", "expected"),
        [
            ({"retry-after": "2"}, 2000),
            ({"retry-after-ms": "1500"}, 1500),
            ({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, None),
            ({}, None),
        ],
        ids=["秒", "毫秒", "HTTP-date 不解析", "没有提示"],
    )
    def test_retry_after_parsing(self, headers: Mapping[str, str], expected: int | None) -> None:
        assert retry_after_ms(headers) == expected

    async def test_transport_errors_do_not_escape_as_httpx_exceptions(self) -> None:
        """供应商客户端库的原生异常不得从 `complete()` 逸出（`protocols.py`）。"""
        def boom(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        provider = make_provider(boom)
        with pytest.raises(KaryviaError):
            await provider.complete(make_request(), ManualCancel())


# ------------------------------------------------------------------------------ 凭据哨兵


class TestCredentialNeverLeaks:
    """`MOD-002`：认证信息不进日志、事件与错误。"""

    def test_the_credential_reaches_only_the_authorization_header(self) -> None:
        """明文只该出现在认证头里：不进 URL（会被中间设备记进 access log）、
        不进请求体、不进 provider 自己持有的任何可渲染结构。"""
        sent: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(200, json=chat_body())

        provider = make_provider(handler)
        assert SENTINEL_KEY not in repr(provider.settings)
        assert SENTINEL_KEY not in repr(provider)

    async def test_the_credential_is_absent_from_the_url_and_the_body(self) -> None:
        sent: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(200, json=chat_body())

        await make_provider(handler).complete(make_request(), ManualCancel())
        request = sent[0]
        assert SENTINEL_KEY not in str(request.url)
        assert SENTINEL_KEY not in request.content.decode("utf-8")
        # 唯一允许出现的位置。
        assert request.headers["authorization"] == f"Bearer {SENTINEL_KEY}"

    def test_the_module_has_no_logging_path_to_leak_through(self) -> None:
        """`MOD-002` 说的是「不进日志与事件」。事件那半边由下面两条用例盯着；
        日志这半边的最强形态不是「日志里没有」，而是**根本没有日志调用**——
        与 `D18` 用「没有语法途径」判定只读内建是同一种判据。"""
        package = Path(model_openai_module.__file__).parent
        for path in sorted(package.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            names = {
                node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
            } | {
                node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
            }
            assert "logger" not in names, path.name
            assert "print" not in names, path.name

    async def test_an_auth_failure_never_echoes_the_key(self) -> None:
        """端点把 key 原样 echo 回错误消息里是真实发生过的事。"""
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401, json={"error": {"code": "invalid_api_key", "message": f"key {SENTINEL_KEY}"}}
            )

        provider = make_provider(handler)
        with pytest.raises(KaryviaError) as caught:
            await provider.complete(make_request(), ManualCancel())
        error = caught.value
        assert SENTINEL_KEY not in repr(error)
        assert SENTINEL_KEY not in str(error)
        assert SENTINEL_KEY not in error.user_message
        assert SENTINEL_KEY not in repr(error.detail)

    async def test_the_key_does_not_survive_event_serialization(self) -> None:
        """事件是离开进程的那些字节，脱敏的意义全在这里。"""
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": {"message": SENTINEL_KEY}})

        provider = make_provider(handler)
        with pytest.raises(KaryviaError) as caught:
            await provider.complete(make_request(), ManualCancel())
        payload = prepare_payload({"error": dict(caught.value.detail)})
        assert SENTINEL_KEY not in json.dumps(payload, ensure_ascii=False)

    def test_the_secret_is_masked_in_every_rendering(self) -> None:
        secret = SecretStr(SENTINEL_KEY)
        assert SENTINEL_KEY not in f"{secret}"
        assert SENTINEL_KEY not in repr(secret)
        assert secret.reveal() == SENTINEL_KEY


# ------------------------------------------------------------------------------ 取消


class TestCancellation:
    async def test_an_already_cancelled_turn_makes_no_round_trip(self) -> None:
        """「进入网络调用前检查 cancel」——已取消就不该再产生一次外部往返。"""
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json=chat_body())

        provider = make_provider(handler)
        cancel = ManualCancel()
        cancel.request()
        with pytest.raises(KaryviaError) as caught:
            await provider.complete(make_request(), cancel)
        assert caught.value.category is ErrorCategory.CANCELLED
        assert calls == []

    async def test_streaming_refuses_an_already_cancelled_turn(self) -> None:
        stream = sse([{"choices": [{"delta": {"content": "hi"}}]}])
        provider = make_provider(lambda _: httpx.Response(200, text=stream))
        cancel = ManualCancel()
        cancel.request()
        with pytest.raises(KaryviaError) as caught:
            await collect(provider, make_request(stream=True), cancel)
        assert caught.value.category is ErrorCategory.CANCELLED

    async def test_streaming_checks_cancellation_between_chunks(self) -> None:
        """取消发生在流中途：已 yield 的分片由调用方保留，且必须先给一个 DONE(ERROR)
        （`EDG-304`）——否则半截回答会被当成完整答案。"""
        stream = sse(
            [
                {"choices": [{"delta": {"content": "第一片"}}]},
                {"choices": [{"delta": {"content": "第二片"}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ]
        )
        provider = make_provider(lambda _: httpx.Response(200, text=stream))
        cancel = ManualCancel()
        seen: list[ModelChunk] = []
        with pytest.raises(KaryviaError) as caught:
            async for chunk in provider.stream(make_request(stream=True), cancel):
                seen.append(chunk)
                cancel.request()  # 收到第一片就叫停。
        assert caught.value.category is ErrorCategory.CANCELLED
        assert seen[0].text == "第一片"
        assert seen[-1].kind is ChunkKind.DONE
        assert seen[-1].stop_reason is StopReason.ERROR


# ------------------------------------------------------------------------------ 配置


class TestSettings:
    def test_defaults_are_usable_without_any_configuration(self) -> None:
        """`BAS-001`：配置一份凭据就能用。"""
        settings = resolve_settings(FakePluginContext())
        info = settings.describe(MODEL_ID)
        assert info.provider == PROVIDER_NAME
        assert info.context_window_tokens > 0
        assert info.supports(ModelCapability.TOOL_CALLS)
        assert info.supports(ModelCapability.STREAMING)

    def test_unsupported_capabilities_are_absent_not_downgraded(self) -> None:
        """`MOD-005`：缺席即报缺失，不静默降级后假装支持。"""
        info = resolve_settings(make_context()).describe(MODEL_ID)
        assert not info.supports(ModelCapability.IMAGE_INPUT)
        assert not info.supports(ModelCapability.REASONING)

    def test_a_non_empty_models_table_is_a_whitelist(self) -> None:
        """运维一旦列举了自己在用的模型，一个拼错的 model_id 就该被当场指出来。"""
        settings = resolve_settings(
            make_context(**{CONFIG_MODELS_KEY: {MODEL_ID: {"context_window_tokens": 8192}}})
        )
        assert settings.describe(MODEL_ID).context_window_tokens == 8192
        with pytest.raises(KaryviaError) as caught:
            settings.describe("gpt-typo")
        assert caught.value.category is ErrorCategory.CAPABILITY_MISSING

    def test_an_empty_models_table_accepts_any_model_id(self) -> None:
        assert resolve_settings(make_context()).describe("anything-goes").model_id == "anything-goes"

    def test_the_base_url_is_used_verbatim(self) -> None:
        """各家后缀差异极大（OpenVINO 是 /v3，vLLM 没有约定），替用户拼一段只会拼错。"""
        settings = resolve_settings(
            make_context(**{CONFIG_BASE_URL_KEY: "http://127.0.0.1:8000/v3"})
        )
        assert settings.base_url == "http://127.0.0.1:8000/v3"

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            (CONFIG_BASE_URL_KEY, "ftp://nope"),
            (CONFIG_BASE_URL_KEY, ""),
            (CONFIG_BASE_URL_KEY, 42),
            (CONFIG_AUTH_KEY, "basic"),
            (CONFIG_MAX_TOKENS_FIELD_KEY, "tokens"),
            (CONFIG_SUPPORTS_TEMPERATURE_KEY, 1),
            (CONFIG_REQUEST_TIMEOUT_KEY, 0),
            (CONFIG_REQUEST_TIMEOUT_KEY, True),
            (CONFIG_STREAM_IDLE_TIMEOUT_KEY, -1),
            (CONFIG_DEFAULT_CONTEXT_WINDOW_KEY, "big"),
            (CONFIG_INCLUDE_USAGE_KEY, "yes"),
            (CONFIG_CAPABILITIES_KEY, ["telepathy"]),
            (CONFIG_CAPABILITIES_KEY, "streaming"),
            (CONFIG_MODELS_KEY, []),
        ],
        ids=[
            "非 http 端点", "空端点", "端点不是字符串", "未知认证形态", "未知上限字段名",
            "1 不是 True", "超时为零", "布尔不是整数", "负超时", "窗口不是整数",
            "include_usage 非布尔", "未知能力名", "能力不是数组", "models 不是对象",
        ],
    )
    def test_bad_configuration_is_rejected(self, key: str, value: JsonValue) -> None:
        with pytest.raises(KaryviaError) as caught:
            resolve_settings(make_context(**{key: value}))
        assert caught.value.category is ErrorCategory.CONFIG

    def test_an_unknown_key_inside_a_model_entry_is_reported_separately(self) -> None:
        """「你多写了一个键」与「你的值写错了」补救动作不同，因此码也不同。"""
        with pytest.raises(KaryviaError) as caught:
            resolve_settings(
                make_context(**{CONFIG_MODELS_KEY: {MODEL_ID: {"windows": 100}}})
            )
        assert caught.value.code is ErrorCode.CONFIG_UNKNOWN_FIELD

    def test_a_bad_configuration_fails_at_setup_rather_than_at_the_first_turn(self) -> None:
        """配置错误在 setup 时暴露，不推迟到第一次 turn。"""
        class RefusingApi:
            ctx = FakePluginContext(
                config={CONFIG_AUTH_KEY: "basic"}, secrets={SECRET_NAME: SENTINEL_KEY}
            )

            def register_model_provider(self, name: str, provider: object) -> None:
                raise AssertionError("配置非法时不该注册任何东西")

        with pytest.raises(KaryviaError) as caught:
            setup(RefusingApi())  # type: ignore[arg-type]
        assert caught.value.category is ErrorCategory.CONFIG

    def test_setup_registers_exactly_one_model_provider(self) -> None:
        registered: list[tuple[str, object]] = []

        class RecordingApi:
            ctx = make_context()

            def register_model_provider(self, name: str, provider: object) -> None:
                registered.append((name, provider))

        setup(RecordingApi())  # type: ignore[arg-type]
        assert len(registered) == 1
        assert registered[0][0] == CAPABILITY_NAME
        assert isinstance(registered[0][1], OpenAIModelProvider)
        assert RecordingApi.ctx.cleanup_actions == [registered[0][1].aclose]

    def test_auth_none_never_touches_the_secret(self) -> None:
        """本地模型服务没有密钥，去要一个必然缺失的凭据只会让实例起不来。"""
        registered: list[object] = []

        class RecordingApi:
            ctx = FakePluginContext(
                config={CONFIG_AUTH_KEY: "none", CONFIG_BASE_URL_KEY: "http://127.0.0.1:11434/v1"},
            )

            def register_model_provider(self, name: str, provider: object) -> None:
                registered.append(provider)

        setup(RecordingApi())  # type: ignore[arg-type]
        assert len(registered) == 1

    def test_a_missing_credential_reports_the_configuration_problem(self) -> None:
        missing_credential = FakePluginContext(config={}, secrets={})
        with pytest.raises(KaryviaError) as missing:
            missing_credential.secret(SECRET_NAME)
        assert missing.value.code is ErrorCode.CONFIG_SECRET_MISSING


# ------------------------------------------------------------------------------ 客户端


class TestHttpClient:
    """`ctx.net` 用不了，所以出网这条路的每个决定都得自己盯着。"""

    @pytest.mark.parametrize(
        ("url", "local"),
        [
            ("http://127.0.0.1:11434/v1", True),
            ("http://localhost:1234/v1", True),
            ("http://192.168.1.9:8000/v1", True),
            ("http://10.0.0.4:8000/v1", True),
            ("http://[::1]:8080/v1", True),
            ("http://host.docker.internal:11434/v1", True),
            ("https://api.openai.com/v1", False),
            ("https://api.example.test/v1", False),
        ],
        ids=[
            "回环", "localhost", "192.168 私有", "10.x 私有", "IPv6 回环",
            "docker 宿主", "OpenAI", "普通域名",
        ],
    )
    def test_local_endpoints_are_detected_by_address_not_by_prefix(
        self, url: str, local: bool
    ) -> None:
        """`127.0.0.1` 之外还有 10.x / 192.168.x / IPv6 回环，它们全是本地模型的常见落点。"""
        assert is_local_endpoint(url) is local

    def test_a_local_endpoint_disables_keepalive(self) -> None:
        """Ollama / llama.cpp / vLLM 会在客户端 keepalive 到期前关掉空闲连接，
        不关就是每轮第一次调用必失败。"""
        provider = make_provider(
            ok_handler(), **{CONFIG_BASE_URL_KEY: "http://127.0.0.1:11434/v1"}
        )
        assert provider.uses_local_endpoint

    def test_a_remote_endpoint_keeps_the_default_pool(self) -> None:
        assert not make_provider(ok_handler()).uses_local_endpoint

    async def test_bearer_is_the_default_authorization_scheme(self) -> None:
        seen: list[httpx.Headers] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers)
            return httpx.Response(200, json=chat_body())

        await make_provider(handler).complete(make_request(), ManualCancel())
        assert seen[0]["authorization"] == f"Bearer {SENTINEL_KEY}"
        assert "api-key" not in seen[0]

    async def test_api_key_header_is_used_for_azure_style_endpoints(self) -> None:
        seen: list[httpx.Headers] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers)
            return httpx.Response(200, json=chat_body())

        provider = make_provider(handler, **{CONFIG_AUTH_KEY: "api_key_header"})
        await provider.complete(make_request(), ManualCancel())
        assert seen[0]["api-key"] == SENTINEL_KEY
        assert "authorization" not in seen[0]

    async def test_auth_none_sends_no_credential_header(self) -> None:
        seen: list[httpx.Headers] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers)
            return httpx.Response(200, json=chat_body())

        ctx = FakePluginContext(
            config={CONFIG_AUTH_KEY: "none", CONFIG_BASE_URL_KEY: "http://127.0.0.1:11434/v1"},
        )
        provider = OpenAIModelProvider(
            resolve_settings(ctx), credential=None, transport=httpx.MockTransport(handler)
        )
        await provider.complete(make_request(), ManualCancel())
        assert "authorization" not in seen[0]
        assert "api-key" not in seen[0]

    async def test_the_request_goes_to_the_chat_completions_path(self) -> None:
        seen: list[httpx.URL] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url)
            return httpx.Response(200, json=chat_body())

        await make_provider(handler).complete(make_request(), ManualCancel())
        assert str(seen[0]) == f"{BASE_URL}{CHAT_COMPLETIONS_PATH}"

    async def test_aclose_is_idempotent(self) -> None:
        """`ModelProvider` 协议里没有生命周期钩子，`D23` 的装配根负责调它。"""
        provider = make_provider(ok_handler())
        await provider.complete(make_request(), ManualCancel())
        await provider.aclose()
        await provider.aclose()

    async def test_a_non_json_success_body_is_an_external_error(self) -> None:
        """200 但正文不是 JSON——中转服务返回一页 HTML 错误页是常见情形。"""
        provider = make_provider(lambda _: httpx.Response(200, text="<html>oops</html>"))
        with pytest.raises(KaryviaError) as caught:
            await provider.complete(make_request(), ManualCancel())
        assert caught.value.category is ErrorCategory.EXTERNAL_SERVICE

    async def test_a_stalled_stream_times_out_on_its_own_watchdog(self) -> None:
        """请求级超时保护不了「开了口就不再吐字」的流：响应头到了、连接活着、字节不来。"""
        async def never_finishes() -> AsyncIterator[bytes]:
            yield b'data: {"choices": [{"delta": {"content": "hi"}}]}\n\n'
            await asyncio.sleep(30)  # 永远等不到的下一片。
            yield b"data: [DONE]\n\n"  # pragma: no cover

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=never_finishes())

        provider = make_provider(handler, **{CONFIG_STREAM_IDLE_TIMEOUT_KEY: 30})
        seen: list[ModelChunk] = []
        with pytest.raises(KaryviaError) as caught:
            async for chunk in provider.stream(make_request(stream=True), ManualCancel()):
                seen.append(chunk)
        assert caught.value.code is ErrorCode.TIMEOUT_MODEL_REQUEST
        assert caught.value.retryable
        # 已经吐过内容，因此仍要有那个 DONE(ERROR)（`EDG-304`）。
        assert seen[-1].stop_reason is StopReason.ERROR

    async def test_a_transport_failure_midstream_is_wrapped(self) -> None:
        async def dies() -> AsyncIterator[bytes]:
            yield b'data: {"choices": [{"delta": {"content": "hi"}}]}\n\n'
            raise httpx.ReadError("connection reset")

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=dies())

        provider = make_provider(handler)
        with pytest.raises(KaryviaError) as caught:
            await collect(provider, make_request(stream=True))
        assert caught.value.category is ErrorCategory.EXTERNAL_SERVICE


# ------------------------------------------------------------------------------ 注册


class TestRegistration:
    """内建的落地形态：一份普通 manifest + 一个 `setup(api)`，没有第二条路（`BAS-005`）。"""

    def test_the_manifest_is_listed_as_a_builtin(self) -> None:
        assert MODEL_OPENAI in BUILTIN_MANIFESTS
        assert MODEL_OPENAI.id == "model-openai"
        declaration = MODEL_OPENAI.capabilities[0]
        assert declaration.kind is CapabilityKind.MODEL
        assert declaration.name == CAPABILITY_NAME
        assert declaration.overrides is None
        # `priority` 不写：内建基准是 0，写了（哪怕写的是默认值 100）就会被原样采纳。
        assert "priority" not in declaration.model_fields_set

    def test_the_config_schema_lists_exactly_the_keys_the_code_reads(self) -> None:
        properties = MODEL_OPENAI.config_schema["properties"]
        assert isinstance(properties, dict)
        assert set(properties) == {
            CONFIG_BASE_URL_KEY,
            CONFIG_AUTH_KEY,
            CONFIG_MODELS_KEY,
            CONFIG_DEFAULT_CONTEXT_WINDOW_KEY,
            CONFIG_DEFAULT_MAX_OUTPUT_KEY,
            CONFIG_CAPABILITIES_KEY,
            CONFIG_MAX_TOKENS_FIELD_KEY,
            CONFIG_SUPPORTS_TEMPERATURE_KEY,
            CONFIG_REQUEST_TIMEOUT_KEY,
            CONFIG_STREAM_IDLE_TIMEOUT_KEY,
            CONFIG_INCLUDE_USAGE_KEY,
        }
        assert MODEL_OPENAI.config_schema["additionalProperties"] is False

    def test_the_model_entry_schema_matches_the_keys_the_code_reads(self) -> None:
        entry_schema = MODEL_OPENAI.config_schema["properties"][CONFIG_MODELS_KEY]
        assert isinstance(entry_schema, dict)
        item = entry_schema["additionalProperties"]
        assert isinstance(item, dict)
        assert set(item["properties"]) == set(MODEL_ENTRY_KEYS)
        assert item["additionalProperties"] is False

    def test_the_auth_enum_matches_the_modes_the_code_accepts(self) -> None:
        schema = MODEL_OPENAI.config_schema["properties"][CONFIG_AUTH_KEY]
        assert isinstance(schema, dict)
        assert set(schema["enum"]) == set(AUTH_MODES)

    async def test_the_capability_is_reachable_through_the_whole_wiring_chain(self) -> None:
        """manifest → import_setup → Host → registry → 取回，与外部插件同一条路。"""
        def context_for(provider: ProviderId) -> PluginContext:
            del provider
            return make_context()

        wiring = await wire_capabilities(manifests=[MODEL_OPENAI], context_for=context_for)
        assert wiring.report.ok
        bindings = model_providers_from(wiring.registry)
        assert len(bindings) == 1
        assert bindings[0].ref.name == CAPABILITY_NAME
        assert bindings[0].owner == Builtin()
        assert isinstance(bindings[0].value, OpenAIModelProvider)

    async def test_the_builtin_priority_baseline_is_zero(self) -> None:
        """§10.2 的「其余按 priority 逆序丢弃」依赖内建排在最前。"""
        def context_for(provider: ProviderId) -> PluginContext:
            del provider
            return make_context()

        wiring = await wire_capabilities(manifests=[MODEL_OPENAI], context_for=context_for)
        assert model_providers_from(wiring.registry)[0].priority == 0
