"""模型请求的统一 token 计量与上下文预算。

职责：按最终结构估算文本、消息和完整 `ModelRequest`，定义统一的触发线与压缩目标，并把
Provider 返回的实际输入 usage 作为相同 Session 后续请求的前缀锚点。
不负责：精确复刻任一供应商 tokenizer、选择压缩插件或实现摘要策略。当前消息契约只有文本
与工具结构，因此这里也不预埋尚不存在的多模态计量分支。
"""

from __future__ import annotations

import json
import math
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from karyvia.contracts import JsonValue, ModelMessage, ModelRequest, SessionKey, TokenUsage

__all__ = [
    "ContextBudget",
    "TokenAccounting",
    "estimate_message_tokens",
    "estimate_messages_tokens",
    "estimate_request_tokens",
    "estimate_tokens",
]

_MESSAGE_OVERHEAD = 4
_REQUEST_OVERHEAD = 8
_SAFETY_RATIO = 0.05
_TARGET_RATIO = 0.80
_MAX_USAGE_ANCHORS = 128


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """一次模型请求共用的硬上限、提前触发线与压缩目标。"""

    hard_limit: int
    trigger_limit: int
    target_limit: int

    @classmethod
    def from_hard_limit(cls, hard_limit: int) -> ContextBudget:
        """从已扣除输出空间的输入硬上限派生安全余量和低水位。"""
        safety = max(1, math.ceil(hard_limit * _SAFETY_RATIO))
        trigger = max(1, hard_limit - safety)
        target = max(1, math.floor(trigger * _TARGET_RATIO))
        return cls(hard_limit=hard_limit, trigger_limit=trigger, target_limit=target)


@dataclass(frozen=True, slots=True)
class _UsageAnchor:
    request: ModelRequest
    input_tokens: int


class TokenAccounting:
    """同一模型实例共享的估算器；真实前缀优先，无法匹配时完整估算。"""

    __slots__ = ("_anchors", "_factor")

    def __init__(self) -> None:
        self._factor = 1.0
        self._anchors: OrderedDict[SessionKey, _UsageAnchor] = OrderedDict()

    @property
    def correction_factor(self) -> float:
        return self._factor

    def estimate_messages(self, messages: Sequence[ModelMessage]) -> int:
        return self._correct(estimate_messages_tokens(messages))

    def estimate_request(self, request: ModelRequest) -> int:
        key = request.correlation.session_key
        anchor = self._anchors.get(key)
        if anchor is not None and _extends_anchor(request, anchor.request):
            self._anchors.move_to_end(key)
            suffix = request.messages[len(anchor.request.messages) :]
            return anchor.input_tokens + self.estimate_messages(suffix)
        return self._correct(estimate_request_tokens(request))

    def observe(self, request: ModelRequest, usage: TokenUsage) -> None:
        """记录实际请求前缀，并在估算低于实际值时提高冷启动修正系数。"""
        raw = estimate_request_tokens(request)
        if usage.input_tokens > raw > 0:
            self._factor = max(self._factor, usage.input_tokens / raw)
        if usage.input_tokens <= 0:
            return
        key = request.correlation.session_key
        self._anchors[key] = _UsageAnchor(request=request, input_tokens=usage.input_tokens)
        self._anchors.move_to_end(key)
        if len(self._anchors) > _MAX_USAGE_ANCHORS:
            self._anchors.popitem(last=False)

    def _correct(self, raw: int) -> int:
        return math.ceil(raw * self._factor)


def _extends_anchor(request: ModelRequest, anchor: ModelRequest) -> bool:
    """只接受 token 相关请求结构不变且消息在尾部追加的锚点。"""
    prefix_size = len(anchor.messages)
    return (
        request.model_id == anchor.model_id
        and request.tools == anchor.tools
        and request.params == anchor.params
        and len(request.messages) >= prefix_size
        and request.messages[:prefix_size] == anchor.messages
    )


def estimate_tokens(text: str) -> int:
    """按 UTF-8 体积粗估文本；同时避免纯字符数对中文的系统性低估。"""
    return math.ceil(len(text.encode("utf-8")) / 4) if text else 0


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
