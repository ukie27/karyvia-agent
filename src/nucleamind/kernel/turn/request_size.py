"""模型请求的保守 token 估算。

职责：以同一把确定性的尺估算文本、消息和完整 `ModelRequest`，供初始 Context 裁剪与
Turn 内压缩共同使用。
不负责：精确复刻任一供应商 tokenizer，也不执行裁剪或压缩策略。
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence

from nucleamind.contracts import JsonValue, ModelMessage, ModelRequest

__all__ = [
    "estimate_message_tokens",
    "estimate_messages_tokens",
    "estimate_request_tokens",
    "estimate_tokens",
]

_CHARS_PER_TOKEN = 3
_MESSAGE_OVERHEAD = 4
_REQUEST_OVERHEAD = 8


def estimate_tokens(text: str) -> int:
    """粗估一段文本的 token 数。空串为 0，其余至少 1。"""
    return math.ceil(len(text) / _CHARS_PER_TOKEN) if text else 0


def _json_tokens(value: Mapping[str, JsonValue]) -> int:
    return estimate_tokens(json.dumps(dict(value), ensure_ascii=False, sort_keys=True))


def estimate_message_tokens(message: ModelMessage) -> int:
    """估算一条消息及其工具调用、关联标识和供应商私有块。"""
    total = _MESSAGE_OVERHEAD + estimate_tokens(message.role.value) + estimate_tokens(
        message.content
    )
    if message.tool_call_id is not None:
        total += estimate_tokens(message.tool_call_id)
    for call in message.tool_calls:
        total += 4 + estimate_tokens(call.call_id) + estimate_tokens(call.name)
        total += _json_tokens(call.arguments)
    for block in message.provider_blocks:
        total += 4 + estimate_tokens(block.provider) + estimate_tokens(block.kind)
        total += _json_tokens(block.payload)
    return total


def estimate_messages_tokens(messages: Sequence[ModelMessage]) -> int:
    """估算一组消息；每条消息的结构开销独立计算。"""
    return sum(estimate_message_tokens(message) for message in messages)


def estimate_request_tokens(request: ModelRequest) -> int:
    """估算完整请求，包括消息、工具 schema 和采样参数。"""
    total = _REQUEST_OVERHEAD + estimate_tokens(request.model_id)
    total += estimate_messages_tokens(request.messages)
    for spec in request.tools:
        total += 8 + estimate_tokens(spec.name) + estimate_tokens(spec.description)
        total += _json_tokens(spec.parameters)
        total += estimate_tokens(spec.risk.value) + estimate_tokens(spec.concurrency.value)
    params = request.params
    for value in (params.temperature, params.top_p, params.max_output_tokens, params.seed):
        if value is not None:
            total += estimate_tokens(str(value)) + 1
    total += sum(estimate_tokens(item) + 1 for item in params.stop_sequences)
    return total
