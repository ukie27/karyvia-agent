"""Hook 替换结果的边界校验。

职责：允许插件改写模型请求策略和工具参数，同时守住只属于 Kernel 的关联、预算与调用身份。
不负责：分发 Hook、执行模型或工具、决定某个 Hook 支持哪些动作。

完整请求/调用对象作为公开载荷便于插件使用 ``dataclasses.replace``，但“可以交回一个对象”
不等于其中每个字段都属于插件策略。本模块集中定义这条边界，避免 Engine 的模型路径和工具
路径各自留下不同的隐式规则。
"""

from __future__ import annotations

from dataclasses import replace

from karyvia.contracts import ErrorCode, KaryviaError, ModelRequest, ToolInvocation

__all__ = ["validated_model_request", "validated_tool_invocation"]


def validated_model_request(
    original: ModelRequest,
    replacement: ModelRequest,
    *,
    remaining_ms: int,
) -> ModelRequest:
    """验证模型请求替换，并把单次超时重新夹进 Turn 剩余预算。"""
    if replacement.correlation != original.correlation:
        raise KaryviaError(
            ErrorCode.KERNEL_INVARIANT_VIOLATED,
            "before_model_request 不能改变 Turn 关联标识。",
            detail={"hook": "before_model_request", "field": "correlation"},
        )
    requested_timeout = replacement.timeout_ms or original.timeout_ms
    return replace(replacement, timeout_ms=max(1, min(requested_timeout, remaining_ms)))


def validated_tool_invocation(
    original: ToolInvocation,
    replacement: ToolInvocation,
) -> ToolInvocation:
    """只允许 ``before_tool_call`` 改写工具参数，不允许重定向调用身份。"""
    changed: list[str] = []
    if replacement.call.name != original.call.name:
        changed.append("call.name")
    if replacement.call.call_id != original.call.call_id:
        changed.append("call.call_id")
    if replacement.correlation != original.correlation:
        changed.append("correlation")
    if replacement.timeout_ms != original.timeout_ms:
        changed.append("timeout_ms")
    if replacement.idempotency_key != original.idempotency_key:
        changed.append("idempotency_key")
    if changed:
        raise KaryviaError(
            ErrorCode.KERNEL_INVARIANT_VIOLATED,
            "before_tool_call 只能改写工具参数。",
            detail={"hook": "before_tool_call", "fields": changed},
        )
    return replacement
