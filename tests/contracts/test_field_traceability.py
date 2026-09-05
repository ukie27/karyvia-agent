"""字段追溯表：每个契约类型的字段集合对照需求 §10（`D03` 验收项 2）。

本文件是「字段遗漏会在阶段 5 才暴露」这条风险的对冲：字段增删必须同时改这里，
`docs/project/README.md` 记录的追溯依据就是这张表，而不是散落在各处的注释。

每行的 `requirement` 写明该类型对应需求文档的哪一节；`fields` 是**完整**字段名集合，
断言用相等而不是包含——包含关系拦不住「多加了一个没人讨论过的字段」。
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

#: 类型 -> (需求出处, 完整字段名集合)。
TRACEABILITY: Final[dict[type, tuple[str, frozenset[str]]]] = {
    Sender: ("§10.2 sender", frozenset({"user_id", "display_name", "is_operator", "is_bot"})),
    AttachmentRef: (
        "§10.2 attachments（引用/媒体类型/大小/受控访问方式）",
        frozenset({"source", "locator", "media_type", "size_bytes", "filename"}),
    ),
    InboundMessage: (
        "§10.2 统一输入消息（十行）",
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
        "§10.3 统一输出消息（九行）",
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
        "§10.4 Context 贡献（七行）",
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
        "§10.5 / TOL-001（名称、描述、schema、风险、输出语义）",
        frozenset(
            {"name", "description", "parameters", "read_only", "risk", "concurrency"}
        ),
    ),
    ToolCall: ("§10.5 Tool Call 输入（调用 ID / 标识 / 参数）", frozenset({"call_id", "name", "arguments"})),
    ToolInvocation: (
        "§10.5 Tool Call 输入（关联信息 / 超时 / 幂等）",
        frozenset({"call", "correlation", "timeout_ms", "idempotency_key"}),
    ),
    ArtifactRef: (
        "§10.5 外部产物引用",
        frozenset({"locator", "media_type", "description", "size_bytes"}),
    ),
    ToolResult: (
        "§10.5 Tool Result 输出（七条）+ D42 的 trust + D47 的 attachments",
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
                # `D42` 加的，**不在 §10.5 那七条里**——需求写的是「输出有哪些部分」，
                # 而这一条说的是「其中的正文进模型时按什么身份出现」。它对应的是
                # `EDG-306`（不可信内容必须被包裹），原来只覆盖 `ContextFragment`。
                "trust",
                # `D47` 加的，同样不在那七条里。它与 `artifacts` 是两个消费者而不是
                # 重复：产物面向 Workspace 与后续工具，附件面向 Channel 投递
                # （`ArtifactRef` 的 docstring 早就把这条分工写死了）。在它之前
                # `artifacts` 零消费者，生成出来的文件没有任何出站通路。
                "attachments",
            }
        ),
    ),
    ModelInfo: (
        "§10.6 模型标识与所需能力 / MOD-001",
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
        "§10.6 采样、最大输出等受支持参数",
        frozenset({"temperature", "top_p", "max_output_tokens", "stop_sequences", "seed"}),
    ),
    ModelMessage: (
        # `provider_blocks` 是 `D45` 加的，它对应的不是 §10.6 的某一行，而是 `EDG-305`
        # 的一条**受控例外**：有些供应商要求原样回传自己产出的块（Anthropic 的
        # `thinking`）才肯继续跑工具循环。它仍然只能是归一化 JSON、仍然带所有权标记、
        # 仍然不进 `SessionMessage`。
        "§10.6 有序消息与 Context（+ EDG-305 的 provider_blocks 例外）",
        frozenset({"role", "content", "tool_calls", "tool_call_id", "provider_blocks"}),
    ),
    ModelRequest: (
        "§10.6 请求（模型标识/消息/工具/参数/关联 ID）",
        frozenset(
            {"model_id", "messages", "correlation", "tools", "params", "stream", "timeout_ms"}
        ),
    ),
    TokenUsage: (
        "§10.6 Token 或费用用量",
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
        "§10.6 响应（内容/Tool Call/终止原因/用量/归一化元数据）",
        frozenset(
            {
                "model_id",
                "stop_reason",
                "content",
                "tool_calls",
                "usage",
                "provider_metadata",
                # `D45`，理由同 `ModelMessage` 那条。
                "provider_blocks",
            }
        ),
    ),
    ModelChunk: (
        "§10.6 流式增量",
        # `block` 是 `OPAQUE` 分片的载荷（`D45`）：流式下 opaque 块必须与文本、工具调用
        # 走同一条通路，否则 `StreamFolder` 收不到它。
        frozenset({"kind", "text", "tool_call", "usage", "stop_reason", "block"}),
    ),
    SessionMessage: (
        "§9.7 SES-002 / SES-004 持久化单元",
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
        "§9.7 SES-004 / SES-006 可迁移存储格式",
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
        "技术方案 §6.4 turn 终态 / KER-003 / KER-005",
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
    # ---------------------------------------------------------------- D04 能力层
    Builtin: ("技术方案 §6.1 ProviderId（内建无字段）", frozenset()),
    Plugin: ("技术方案 §6.1 ProviderId / PLG-001", frozenset({"plugin_id"})),
    CapabilityRef: (
        "技术方案 §6.1 能力标识 / SDK-002",
        frozenset({"kind", "name", "provider", "version"}),
    ),
    HookContext: (
        "技术方案 §6.6 Hook 表格的输入侧",
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
        "技术方案 §6.6「返回语义」列",
        frozenset({"action", "fragments", "request", "invocation", "result", "reason"}),
    ),
    CommandParam: (
        "§9.13 CMD-001「参数形式」",
        frozenset({"name", "description", "required", "repeated"}),
    ),
    CommandSpec: (
        "§9.13 CMD-001（名称/参数形式/说明/操作者要求）",
        frozenset(
            {"name", "description", "parameters", "operator_only", "aliases"}
        ),
    ),
    CommandInvocation: (
        "技术方案 §6.3 输入分流 / KER-010",
        frozenset({"name", "args", "raw_text", "message", "correlation"}),
    ),
    CommandResult: (
        "技术方案 §6.3 Disposition / CMD-003 / CMD-004",
        frozenset(
            {"disposition", "content", "rewritten_input", "fragments", "error", "metadata"}
        ),
    ),
}


@pytest.mark.parametrize(
    ("contract", "requirement", "expected"),
    [(cls, req, fields) for cls, (req, fields) in TRACEABILITY.items()],
    ids=[cls.__name__ for cls in TRACEABILITY],
)
def test_fields_match_requirement(
    contract: type, requirement: str, expected: frozenset[str]
) -> None:
    actual = frozenset(f.name for f in dataclasses.fields(contract))
    assert actual == expected, f"{contract.__name__} 的字段与 {requirement} 不一致"


def test_every_contract_is_a_frozen_slotted_dataclass() -> None:
    """三条不变量之一：契约对象一律不可变（技术方案 §5.1）。

    `slots=True` 一并断言：字段落在 `__slots__` 里，实例才加不上临时属性。
    """
    for contract in TRACEABILITY:
        params = contract.__dataclass_params__  # pyright: ignore[reportAttributeAccessIssue]
        assert params.frozen, f"{contract.__name__} 不是 frozen dataclass"
        slots = getattr(contract, "__slots__", None)
        assert slots is not None, f"{contract.__name__} 未启用 slots"
        assert frozenset(slots) == frozenset(f.name for f in dataclasses.fields(contract))
