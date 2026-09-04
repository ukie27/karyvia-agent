"""持久化会话压缩的触发、校验与提交。

职责：在统一请求预算接近上限且存在会话历史时调用选中的 `ContextCompactor`，在写 Session
之前验证摘要确实把最终请求压到目标水位，然后原子推进持久化水位。
不负责：摘要策略、Context Provider 调度、Turn 内临时压缩或任何备用硬裁剪。

插件超时、抛异常、返回非法结果或未达到目标都直接终止 Turn；Kernel 不保留旧裁剪结果作为
回退路径。插件显式返回 `None` 仍表示本轮不做持久化压缩，后续请求由正常 Turn 预算机制处理。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final

from nucleamind.contracts import (
    CancelSignal,
    CapabilityKind,
    CapabilityRef,
    CompactionRequest,
    CompactionResult,
    ContextCompactor,
    Correlation,
    ErrorCode,
    ModelRequest,
    NucleaError,
    ProviderId,
    Role,
    SessionMessage,
    SessionSnapshot,
    SessionStore,
)

from .context_builder import AssembledContext, reassemble_history, replay_messages
from .request_size import ContextBudget, TokenAccounting

__all__ = [
    "DEFAULT_COMPACTOR_TIMEOUT_MS",
    "CompactionApplied",
    "CompactionPolicy",
    "compact_once",
]

DEFAULT_COMPACTOR_TIMEOUT_MS: Final = 3_000


@dataclass(frozen=True, slots=True)
class CompactionPolicy:
    """显式选中的压缩能力及其调用预算。"""

    compactor: ContextCompactor
    name: str
    owner: ProviderId
    timeout_ms: int = DEFAULT_COMPACTOR_TIMEOUT_MS

    @property
    def ref(self) -> CapabilityRef:
        return CapabilityRef(kind=CapabilityKind.COMPACTOR, name=self.name, provider=self.owner)


@dataclass(frozen=True, slots=True)
class CompactionApplied:
    """一次成功持久化后的新快照、上下文与水位。"""

    snapshot: SessionSnapshot
    context: AssembledContext
    through: int


async def compact_once(
    *,
    snapshot: SessionSnapshot,
    context: AssembledContext,
    request: ModelRequest,
    user_input: str,
    correlation: Correlation,
    cancel: CancelSignal,
    sessions: SessionStore,
    policy: CompactionPolicy | None,
    budget: ContextBudget,
    accounting: TokenAccounting,
    now: datetime,
) -> CompactionApplied | None:
    """接近触发线且有可重放历史时，至多执行一次持久化压缩。"""
    if (
        policy is None
        or accounting.estimate_request(request) <= budget.trigger_limit
        or not replay_messages(snapshot)
    ):
        return None

    without_history = reassemble_history(
        context,
        SessionSnapshot(session_key=snapshot.session_key),
        user_input,
        accounting,
    )
    if accounting.estimate_request(
        replace(request, messages=without_history.messages)
    ) > budget.target_limit:
        # 持久化 Compactor 只能替换 Session 历史。压力来自系统段、当前输入或临时片段时，
        # 不调用一个注定无法达到目标的能力；最终请求仍由 Turn Compactor 统一处理。
        return None

    compaction_request = CompactionRequest(
        snapshot=snapshot,
        target_tokens=budget.target_limit,
        correlation=correlation,
        user_input=user_input,
    )
    result = await _invoke(policy, compaction_request, cancel)
    if result is None:
        return None

    summary = _validated_summary(snapshot, result, policy, correlation, now)
    projected = _project_snapshot(snapshot, result.through, summary)
    projected_context = reassemble_history(context, projected, user_input, accounting)
    projected_request = replace(request, messages=projected_context.messages)
    estimated = accounting.estimate_request(projected_request)
    if estimated > budget.target_limit:
        raise NucleaError(
            ErrorCode.PLUGIN_HOOK_FAILED,
            "Context Compactor 的结果未达到请求预算目标。",
            detail={"estimated_tokens": estimated, "target_tokens": budget.target_limit},
            capability=policy.ref,
        )

    await sessions.compact(snapshot.session_key, result.through, summary)
    persisted = await sessions.load(snapshot.session_key)
    persisted_context = reassemble_history(context, persisted, user_input, accounting)
    return CompactionApplied(
        snapshot=persisted,
        context=persisted_context,
        through=result.through,
    )


async def _invoke(
    policy: CompactionPolicy,
    request: CompactionRequest,
    cancel: CancelSignal,
) -> CompactionResult | None:
    try:
        return await asyncio.wait_for(
            policy.compactor.compact(request, cancel),
            timeout=policy.timeout_ms / 1000,
        )
    except TimeoutError as error:
        raise NucleaError(
            ErrorCode.TIMEOUT_HOOK,
            "Context Compactor 超时。",
            detail={"timeout_ms": policy.timeout_ms},
            capability=policy.ref,
        ) from error
    except NucleaError:
        raise
    except Exception as error:
        raise NucleaError(
            ErrorCode.PLUGIN_HOOK_FAILED,
            "Context Compactor 抛出了异常。",
            detail={"exception": type(error).__name__},
            capability=policy.ref,
        ) from error


def _validated_summary(
    snapshot: SessionSnapshot,
    result: CompactionResult,
    policy: CompactionPolicy,
    correlation: Correlation,
    now: datetime,
) -> SessionMessage:
    content = result.content.strip()
    if (
        result.through <= snapshot.compacted_through
        or result.through > len(snapshot.messages)
        or not content
    ):
        raise NucleaError(
            ErrorCode.PLUGIN_HOOK_FAILED,
            "Context Compactor 返回了非法结果。",
            detail={
                "through": result.through,
                "compacted_through": snapshot.compacted_through,
                "messages": len(snapshot.messages),
                "empty_content": not bool(content),
            },
            capability=policy.ref,
        )
    return SessionMessage(
        message_id=f"compaction-{correlation.turn_id}",
        role=Role.SYSTEM,
        content=content,
        created_at=now,
        turn_id=correlation.turn_id,
        metadata={"compactor": policy.name, "provider": str(policy.owner)},
    )


def _project_snapshot(
    snapshot: SessionSnapshot,
    through: int,
    summary: SessionMessage,
) -> SessionSnapshot:
    return SessionSnapshot(
        session_key=snapshot.session_key,
        messages=(*snapshot.messages[:through], summary, *snapshot.messages[through:]),
        created_at=snapshot.created_at,
        updated_at=snapshot.updated_at,
        compacted_through=through,
        schema_version=snapshot.schema_version,
    )
