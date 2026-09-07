"""契约字段快照：锁定每个公开 dataclass 的完整字段集合。

字段增删必须同时更新这张表。断言使用相等而不是包含，因此未评审的新字段
和意外删除都会立即失败。每行的说明只描述类型用途，不依赖外部追溯编号。
"""

from __future__ import annotations

import dataclasses
from typing import Final

import pytest

from karyvia.contracts import (
    ArtifactRef,
    AttachmentRef,
    Builtin,
    CapabilityRef,
    CommandInvocation,
    CommandParam,
    CommandResult,
    CommandSpec,
    ContextFragment,
    HookContext,
    HookOutcome,
    InboundMessage,
    ModelChunk,
    ModelInfo,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    OutboundMessage,
    Plugin,
    SamplingParams,
    Sender,
    SessionMessage,
    SessionSnapshot,
    TokenUsage,
    ToolCall,
    ToolInvocation,
    ToolResult,
    ToolSpec,
    TurnOutcome,
)

#: 类型 -> (用途, 完整字段名集合)。
CONTRACT_FIELDS: Final[dict[type, tuple[str, frozenset[str]]]] = {
    Sender: ("发送者身份", frozenset({"user_id", "display_name", "is_operator", "is_bot"})),
    AttachmentRef: (
        "附件引用、媒体类型、大小与受控访问方式",
        frozenset({"source", "locator", "media_type", "size_bytes", "filename"}),
    ),
    InboundMessage: (
        "统一入站消息",
        frozenset(
            {
                "message_id",
                "instance_id",
                "channel_id",
                "conversation_id",
                "sender",
                "content",
                "timestamp",
                "attachments",
                "reply_to",
                "metadata",
            }
        ),
    ),
    OutboundMessage: (
        "统一出站消息",
        frozenset(
            {
                "session_key",
                "channel_id",
                "conversation_id",
                "turn_id",
                "content",
                "attachments",
                "reply_to",
                "stream_state",
                "metadata",
            }
        ),
    ),
    ContextFragment: (
        "Context 贡献及其信任、作用域与敏感度",
        frozenset(
            {
                "source",
                "kind",
                "content",
                "priority",
                "estimated_tokens",
                "scope",
                "trust",
                "sensitivity",
                "expires_at",
            }
        ),
    ),
    ToolSpec: (
        "工具名称、描述、schema、风险与输出语义",
        frozenset(
            {"name", "description", "parameters", "read_only", "risk", "concurrency"}
        ),
    ),
    ToolCall: ("Tool Call 的调用 ID、工具名与参数", frozenset({"call_id", "name", "arguments"})),
    ToolInvocation: (
        "Tool Call 的关联信息、超时与幂等键",
        frozenset({"call", "correlation", "timeout_ms", "idempotency_key"}),
    ),
    ArtifactRef: (
        "外部产物引用",
        frozenset({"locator", "media_type", "description", "size_bytes"}),
    ),
    ToolResult: (
        "Tool Result 输出、信任级别与附件",
        frozenset(
            {
                "call_id",
                "ok",
                "content",
                "truncated",
                "side_effect",
                "data",
                "artifacts",
                "error",
                "duration_ms",
                # 正文进入模型时必须携带明确的信任身份。
                "trust",
                # 产物面向 Workspace 与后续工具，附件面向 Channel 投递。
                "attachments",
            }
        ),
    ),
    ModelInfo: (
        "模型标识、能力与上下文上限",
        frozenset(
            {
                "model_id",
                "provider",
                "capabilities",
                "context_window_tokens",
                "max_output_tokens",
            }
        ),
    ),
    SamplingParams: (
        "采样、最大输出等受支持参数",
        frozenset({"temperature", "top_p", "max_output_tokens", "stop_sequences", "seed"}),
    ),
    ModelMessage: (
        # `provider_blocks` 是一条受控例外：有些供应商要求原样回传自己产出的块（Anthropic 的
        # `thinking`）才肯继续跑工具循环。它仍然只能是归一化 JSON、仍然带所有权标记、
        # 仍然不进 `SessionMessage`。
        "有序消息、Context 与受控的 provider blocks",
        frozenset({"role", "content", "tool_calls", "tool_call_id", "provider_blocks"}),
    ),
    ModelRequest: (
        "模型请求、消息、工具、参数与关联 ID",
        frozenset(
            {"model_id", "messages", "correlation", "tools", "params", "stream", "timeout_ms"}
        ),
    ),
    TokenUsage: (
        "Token 与费用用量",
        frozenset(
            {
                "input_tokens",
                "output_tokens",
                "cached_input_tokens",
                "reasoning_tokens",
                "cost_usd",
            }
        ),
    ),
    ModelResponse: (
        "模型响应内容、Tool Call、终止原因、用量与元数据",
        frozenset(
            {
                "model_id",
                "stop_reason",
                "content",
                "tool_calls",
                "usage",
                "provider_metadata",
                # 理由同 `ModelMessage` 的 provider blocks。
                "provider_blocks",
            }
        ),
    ),
    ModelChunk: (
        "流式增量",
        # `block` 是 `OPAQUE` 分片的载荷：流式下 opaque 块必须与文本、工具调用
        # 走同一条通路，否则 `StreamFolder` 收不到它。
        frozenset({"kind", "text", "tool_call", "usage", "stop_reason", "block"}),
    ),
    SessionMessage: (
        "Session 持久化单元",
        frozenset(
            {
                "message_id",
                "role",
                "content",
                "created_at",
                "turn_id",
                "tool_call_id",
                "interrupted",
                "attachments",
                "metadata",
            }
        ),
    ),
    SessionSnapshot: (
        "Session 可迁移存储快照",
        frozenset(
            {
                "session_key",
                "messages",
                "created_at",
                "updated_at",
                "compacted_through",
                "schema_version",
            }
        ),
    ),
    TurnOutcome: (
        "turn 终态与执行统计",
        frozenset(
            {
                "correlation",
                "status",
                "started_at",
                "finished_at",
                "iterations",
                "tool_calls",
                "error",
                "cancel_reason",
            }
        ),
    ),
    # ---------------------------------------------------------------- 能力层
    Builtin: ("ProviderId（内建无字段）", frozenset()),
    Plugin: ("ProviderId（外部插件）", frozenset({"plugin_id"})),
    CapabilityRef: (
        "能力种类、名称、提供方与版本",
        frozenset({"kind", "name", "provider", "version"}),
    ),
    HookContext: (
        "Hook 输入上下文",
        frozenset(
            {
                "hook",
                "correlation",
                "message",
                "fragments",
                "request",
                "response",
                "invocation",
                "result",
                "outcome",
            }
        ),
    ),
    HookOutcome: (
        "Hook 返回语义",
        frozenset({"action", "fragments", "request", "invocation", "result", "reason"}),
    ),
    CommandParam: (
        "命令参数形式",
        frozenset({"name", "description", "required", "repeated"}),
    ),
    CommandSpec: (
        "命令名称、参数、说明与操作者要求",
        frozenset(
            {"name", "description", "parameters", "operator_only", "aliases"}
        ),
    ),
    CommandInvocation: (
        "命令输入与关联信息",
        frozenset({"name", "args", "raw_text", "message", "correlation"}),
    ),
    CommandResult: (
        "命令分流结果",
        frozenset(
            {"disposition", "content", "rewritten_input", "fragments", "error", "metadata"}
        ),
    ),
}


@pytest.mark.parametrize(
    ("contract", "purpose", "expected"),
    [(cls, purpose, fields) for cls, (purpose, fields) in CONTRACT_FIELDS.items()],
    ids=[cls.__name__ for cls in CONTRACT_FIELDS],
)
def test_contract_fields_match_snapshot(
    contract: type, purpose: str, expected: frozenset[str]
) -> None:
    actual = frozenset(f.name for f in dataclasses.fields(contract))
    assert actual == expected, f"{contract.__name__} 的字段与用途「{purpose}」不一致"


def test_every_contract_is_a_frozen_slotted_dataclass() -> None:
    """三条不变量之一：契约对象一律不可变。

    `slots=True` 一并断言：字段落在 `__slots__` 里，实例才加不上临时属性。
    """
    for contract in CONTRACT_FIELDS:
        params = contract.__dataclass_params__  # pyright: ignore[reportAttributeAccessIssue]
        assert params.frozen, f"{contract.__name__} 不是 frozen dataclass"
        slots = getattr(contract, "__slots__", None)
        assert slots is not None, f"{contract.__name__} 未启用 slots"
        assert frozenset(slots) == frozenset(f.name for f in dataclasses.fields(contract))
