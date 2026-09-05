"""`anthropic` 插件测试的公共夹具（供同目录的三个测试文件 import）。

职责：哨兵凭据、`FakePluginContext` 构造、canned 响应体与 SSE、MockTransport provider。
不负责：任何断言——每条判定都写在对应的 `test_*.py` 里。

**不放在 `conftest.py`**：那个文件的职责是零网络闸门那条 autouse 夹具，把一堆构造函数
塞进去会让「conftest 里有什么」不再一眼可知。测试目录不是包，pytest 的 prepend 导入模式
会把它加进 `sys.path`，因此 `import _support` 成立。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Final

import httpx
from karyvia_plugin_anthropic import (
    MANIFEST,
    SECRET_NAME,
    AnthropicModelProvider,
    AnthropicSettings,
    build_payload,
    resolve_settings,
)
from karyvia_plugin_anthropic.settings import CONFIG_BASE_URL_KEY

from karyvia.contracts import (
    JsonValue,
    ModelChunk,
    ModelMessage,
    ModelRequest,
    Role,
    SamplingParams,
    SecretStr,
    ToolSpec,
)
from karyvia.sdk.testing import FakePluginContext, ManualCancel, make_correlation

MODEL_ID: Final = "claude-sonnet-4-5"
BASE_URL: Final = "https://api.anthropic.test/v1"

#: 形状必须匹配 `_SECRET_VALUE_PATTERNS` 里的 `sk-[A-Za-z0-9_-]{16,}`。
SENTINEL_KEY: Final = "sk-ant-ThisMustNeverLeak0123456789"

# ------------------------------------------------------------------------------ 夹具


def make_context(**config: JsonValue) -> FakePluginContext:
    """一个带哨兵凭据的 ctx。默认端点是可控的测试域名。"""
    payload: dict[str, JsonValue] = {CONFIG_BASE_URL_KEY: BASE_URL}
    payload.update(config)
    return FakePluginContext(
        plugin_id=MANIFEST.id,
        config=payload,
        secrets={SECRET_NAME: SENTINEL_KEY},
    )


def make_settings(**config: JsonValue) -> AnthropicSettings:
    return resolve_settings(make_context(**config))


def message_body(
    *,
    content: Sequence[JsonValue] = (),
    stop_reason: str | None = "end_turn",
    usage: Mapping[str, JsonValue] | None = None,
    **extra: JsonValue,
) -> dict[str, JsonValue]:
    body: dict[str, JsonValue] = {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": MODEL_ID,
        "content": list(content) or [{"type": "text", "text": "pong"}],
    }
    if stop_reason is not None:
        body["stop_reason"] = stop_reason
    if usage is not None:
        body["usage"] = dict(usage)
    body.update(extra)
    return body


def sse(events: Sequence[Mapping[str, JsonValue]]) -> str:
    """把事件序列渲染成 SSE。**Anthropic 没有 `[DONE]` 哨兵**，流结束就是迭代结束。"""
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(dict(event))}\n\n" for event in events
    )


def text_stream(text: str = "pong", *, stop_reason: str = "end_turn") -> str:
    return sse(
        [
            {"type": "message_start", "message": {"id": "msg_01", "model": MODEL_ID, "usage": {"input_tokens": 3}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": stop_reason}, "usage": {"output_tokens": 5}},
            {"type": "message_stop"},
        ]
    )


def make_provider(
    handler: Callable[[httpx.Request], httpx.Response],
    **config: JsonValue,
) -> AnthropicModelProvider:
    """把一个 MockTransport 接到真的 provider 上。"""
    return AnthropicModelProvider(
        make_settings(**config),
        credential=SecretStr(SENTINEL_KEY),
        transport=httpx.MockTransport(handler),
    )


def make_request(
    *,
    stream: bool = False,
    messages: Sequence[ModelMessage] = (),
    tools: Sequence[ToolSpec] = (),
    params: SamplingParams | None = None,
    model_id: str = MODEL_ID,
) -> ModelRequest:
    return ModelRequest(
        model_id=model_id,
        messages=tuple(messages) or (ModelMessage(role=Role.USER, content="ping"),),
        correlation=make_correlation(),
        tools=tuple(tools),
        params=params or SamplingParams(),
        stream=stream,
    )


def payload_for(*messages: ModelMessage, **kwargs: object) -> dict[str, JsonValue]:
    return build_payload(make_request(messages=messages), max_output_tokens=1024, **kwargs)


async def collect(
    provider: AnthropicModelProvider,
    request: ModelRequest,
    cancel: ManualCancel | None = None,
) -> list[ModelChunk]:
    return [chunk async for chunk in provider.stream(request, cancel or ManualCancel())]


def sample_tool(name: str = "fs.read") -> ToolSpec:
    return ToolSpec(
        name=name,
        description="读一个文件",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
    )

