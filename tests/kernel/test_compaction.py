"""持久化上下文压缩的触发、最终请求校验与失败语义。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from nucleamind.contracts import (
    Builtin,
    CompactionRequest,
    CompactionResult,
    ErrorCode,
    ModelMessage,
    ModelRequest,
    NucleaError,
    Role,
    SessionMessage,
    SessionSnapshot,
)
from nucleamind.kernel.turn import (
    AssembledContext,
    CancelToken,
    CompactionPolicy,
    ContextBudget,
    TokenAccounting,
    compact_once,
    replay_messages,
)

from ._engine_support import CORRELATION
from ._orchestrator_support import FakeSessionStore

NOW = datetime(2026, 8, 18, tzinfo=UTC)
USER_INPUT = "当前问题"


def record(role: Role, content: str, index: int) -> SessionMessage:
    return SessionMessage(
        message_id=f"m{index}",
        role=role,
        content=content,
        created_at=NOW,
    )


def history_snapshot(*messages: SessionMessage, compacted_through: int = 0) -> SessionSnapshot:
    return SessionSnapshot(
        session_key=CORRELATION.session_key,
        messages=messages,
        compacted_through=compacted_through,
    )


def context_for(snapshot: SessionSnapshot) -> AssembledContext:
    messages = (*replay_messages(snapshot), ModelMessage(Role.USER, USER_INPUT))
    return AssembledContext(
        messages=messages,
        fragments=(),
        dropped=(),
        estimated_tokens=TokenAccounting().estimate_messages(messages),
    )


class RecordingCompactor:
    def __init__(
        self,
        result: CompactionResult | None = None,
        *,
        error: Exception | None = None,
        hang: bool = False,
    ) -> None:
        self.result = result
        self.error = error
        self.hang = hang
        self.requests: list[CompactionRequest] = []

    async def compact(self, request, cancel):  # noqa: ANN001, ANN202
        del cancel
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if self.hang:
            await asyncio.Event().wait()
        return self.result


def policy(compactor: RecordingCompactor, *, timeout_ms: int = 3_000) -> CompactionPolicy:
    return CompactionPolicy(compactor, "summary", Builtin(), timeout_ms)


async def run(
    snapshot: SessionSnapshot,
    compactor: CompactionPolicy | None,
    store: FakeSessionStore,
    *,
    hard_limit: int = 80,
):
    accounting = TokenAccounting()
    context = context_for(snapshot)
    request = ModelRequest(
        model_id="fake-model",
        messages=context.messages,
        correlation=CORRELATION,
    )
    return await compact_once(
        snapshot=snapshot,
        context=context,
        request=request,
        user_input=USER_INPUT,
        correlation=CORRELATION,
        cancel=CancelToken(),
        sessions=store,  # type: ignore[arg-type]
        policy=compactor,
        budget=ContextBudget.from_hard_limit(hard_limit),
        accounting=accounting,
        now=NOW,
    )


async def test_disabled_policy_and_request_below_trigger_do_not_call_plugin() -> None:
    short = history_snapshot(record(Role.USER, "旧问题", 1))
    store = FakeSessionStore(short.messages)
    compactor = RecordingCompactor(CompactionResult(through=1, content="摘要"))

    assert await run(short, None, store, hard_limit=20) is None
    assert await run(short, policy(compactor), store, hard_limit=1_000) is None
    assert compactor.requests == []


async def test_none_result_skips_persistence_without_using_a_hard_trim_fallback() -> None:
    snap = history_snapshot(record(Role.USER, "旧问题" * 100, 1))
    store = FakeSessionStore(snap.messages)
    compactor = RecordingCompactor()

    assert await run(snap, policy(compactor), store) is None
    assert len(compactor.requests) == 1
    assert store.compactions == []


async def test_persistent_compactor_is_not_called_when_history_cannot_reach_target() -> None:
    snap = history_snapshot(record(Role.USER, "旧问题" * 100, 1))
    store = FakeSessionStore(snap.messages)
    compactor = RecordingCompactor(CompactionResult(through=1, content="摘要"))

    assert await run(snap, policy(compactor), store, hard_limit=20) is None
    assert compactor.requests == []


@pytest.mark.parametrize(
    "result",
    [
        CompactionResult(through=1, content=" "),
        CompactionResult(through=0, content="摘要"),
        CompactionResult(through=4, content="摘要"),
    ],
)
async def test_invalid_result_fails_without_persistence(result: CompactionResult) -> None:
    snap = history_snapshot(
        record(Role.USER, "旧问题" * 100, 1),
        record(Role.ASSISTANT, "旧回答", 2),
    )
    store = FakeSessionStore(snap.messages)

    with pytest.raises(NucleaError) as caught:
        await run(snap, policy(RecordingCompactor(result)), store)

    assert caught.value.code is ErrorCode.PLUGIN_HOOK_FAILED
    assert store.compactions == []


@pytest.mark.parametrize(
    ("compactor", "code"),
    [
        (RecordingCompactor(error=RuntimeError("boom")), ErrorCode.PLUGIN_HOOK_FAILED),
        (RecordingCompactor(hang=True), ErrorCode.TIMEOUT_HOOK),
    ],
)
async def test_plugin_failure_propagates(
    compactor: RecordingCompactor, code: ErrorCode
) -> None:
    snap = history_snapshot(record(Role.USER, "旧问题" * 100, 1))
    store = FakeSessionStore(snap.messages)

    with pytest.raises(NucleaError) as caught:
        await run(snap, policy(compactor, timeout_ms=1), store)

    assert caught.value.code is code
    assert store.compactions == []


async def test_result_must_reach_target_before_persistence() -> None:
    snap = history_snapshot(record(Role.USER, "旧问题" * 100, 1))
    store = FakeSessionStore(snap.messages)
    too_long = RecordingCompactor(CompactionResult(through=1, content="很长摘要" * 100))

    with pytest.raises(NucleaError) as caught:
        await run(snap, policy(too_long), store)

    assert caught.value.code is ErrorCode.PLUGIN_HOOK_FAILED
    assert caught.value.detail["target_tokens"] == 60
    assert store.compactions == []


async def test_success_persists_summary_and_returns_rebuilt_context() -> None:
    snap = history_snapshot(
        record(Role.USER, "旧问题" * 100, 1),
        record(Role.ASSISTANT, "旧回答", 2),
    )
    store = FakeSessionStore(snap.messages)
    compactor = RecordingCompactor(CompactionResult(through=2, content="  对话摘要  "))

    applied = await run(snap, policy(compactor), store)

    assert applied is not None
    assert applied.through == 2
    assert len(store.compactions) == 1
    assert store.compactions[0][2].content == "对话摘要"
    assert applied.snapshot.live_messages[0].content == "对话摘要"
    assert applied.context.messages[0].content == "对话摘要"
    assert compactor.requests[0].target_tokens == 60


async def test_persistence_failure_propagates() -> None:
    class CompactFailingStore(FakeSessionStore):
        async def compact(self, key, through, summary):  # noqa: ANN001, ANN202
            del key, through, summary
            raise NucleaError(ErrorCode.PERSISTENCE_WRITE_FAILED, "磁盘满了。")

    snap = history_snapshot(record(Role.USER, "旧问题" * 100, 1))
    store = CompactFailingStore(snap.messages)
    compactor = RecordingCompactor(CompactionResult(through=1, content="摘要"))

    with pytest.raises(NucleaError) as caught:
        await run(snap, policy(compactor), store)
    assert caught.value.code is ErrorCode.PERSISTENCE_WRITE_FAILED
