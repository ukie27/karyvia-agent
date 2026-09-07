""" 长期记忆的召回路径：`kernel/turn/memory.py` 与它在组装器里的落点。

职责：验那四条判定——只用 `AGENT` 范围召回、priority 有下界、失败按  分叉、
空查询不打扰后端；以及「召回出来的片段与其余片段完全同等」（同批拦截、同批裁剪）。
不负责：验装配根怎么挑 provider（`tests/runtime/test_bootstrap.py`）、验配置字段
（`tests/kernel/test_config.py`）、验某个具体后端（各插件自己的 `tests/`）。

**Fake 就落在 `MemoryProvider` 这个契约边界上**：它是本模块唯一的协作者，再往里放一层
（真的读盘）验的就是那个后端而不是这条路径了。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime

import pytest

from karyvia.contracts import (
    CancelSignal,
    ContextFragment,
    Correlation,
    ErrorCode,
    FragmentKind,
    FragmentScope,
    InstanceId,
    KaryviaError,
    Sensitivity,
    SessionKey,
    TrustLevel,
    TurnId,
)
from karyvia.kernel.turn import CancelToken, MemoryRecall, assemble, select_memory
from karyvia.kernel.turn.memory import MEMORY_RECALL_SCOPE

NOW = datetime(2026, 8, 12, tzinfo=UTC)
KEY = SessionKey(channel_id="cli", conversation_id="c0")
CORRELATION = Correlation(
    instance_id=InstanceId("inst"), session_key=KEY, turn_id=TurnId("t0")
)


def fragment(content: str, *, priority: int = 0, trust: TrustLevel = TrustLevel.UNTRUSTED) -> ContextFragment:
    return ContextFragment(
        source="plugin:memory",
        kind=FragmentKind.MEMORY,
        content=content,
        priority=priority,
        estimated_tokens=4,
        scope=FragmentScope.AGENT,
        trust=trust,
        sensitivity=Sensitivity.NORMAL,
    )


class FakeMemory:
    """`contracts.MemoryProvider`。记下每次调用的实参，可注入失败与延迟。"""

    def __init__(
        self,
        *,
        fragments: Mapping[str, ContextFragment] | None = None,
        fails: BaseException | None = None,
        delay: float = 0.0,
    ) -> None:
        self.fragments = fragments or {}
        self.fails = fails
        self.delay = delay
        self.calls: list[tuple[str, FragmentScope, int]] = []

    async def remember(self, fragment: ContextFragment, cancel: CancelSignal) -> str:
        raise NotImplementedError

    async def recall(
        self,
        query: str,
        *,
        scope: FragmentScope,
        limit: int,
        cancel: CancelSignal,
    ) -> Mapping[str, ContextFragment]:
        self.calls.append((query, scope, limit))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fails is not None:
            raise self.fails
        return self.fragments

    async def forget(self, record_id: str) -> bool:
        raise NotImplementedError


def recall_for(memory: FakeMemory, **kwargs: object) -> MemoryRecall:
    return MemoryRecall(provider=memory, name="fake", owner="plugin:m", **kwargs)  # type: ignore[arg-type]


# ------------------------------------------------------------------ 范围与查询


async def test_only_agent_scope_is_ever_requested() -> None:
    """契约的三个方法一个 `SessionKey` 都不带，因此经它只能表达实例级记忆。

    **这是决定而不是默认**（`memory.py` 第 1 条）：传 `scope=SESSION` 下去，实现方只能猜
    「哪个会话」。会话级与工作区级的记忆归 `ContextProvider`。
    """
    memory = FakeMemory(fragments={"r1": fragment("用户喜欢简短回答")})
    await recall_for(memory).recall("怎么回答", CORRELATION, CancelToken())
    assert [scope for _, scope, _ in memory.calls] == [MEMORY_RECALL_SCOPE]
    assert MEMORY_RECALL_SCOPE is FragmentScope.AGENT


async def test_the_query_is_this_turns_input_and_the_limit_comes_from_config() -> None:
    memory = FakeMemory()
    await recall_for(memory, limit=3).recall("今天几号", CORRELATION, CancelToken())
    assert memory.calls == [("今天几号", FragmentScope.AGENT, 3)]


@pytest.mark.parametrize("query", ["", "   ", "\n\t"])
async def test_an_empty_query_never_reaches_the_backend(query: str) -> None:
    """命令类 turn 与刚开的会话都会走到这里。拿空串去检索只会拿回一批「碰巧得分最高」的。"""
    memory = FakeMemory(fragments={"r1": fragment("x")})
    assert await recall_for(memory).recall(query, CORRELATION, CancelToken()) == ()
    assert memory.calls == []


# ------------------------------------------------------------------ priority 下界


async def test_priority_is_floored_but_never_lowered() -> None:
    """**这是本模块唯一会改写的字段**（`memory.py` 第 2 条）。

    priority 是拦截器和诊断可见的来源元数据，不进入 Kernel 的 token 预算计算。
    """
    memory = FakeMemory(
        fragments={
            "low": fragment("优先级 0", priority=0),
            "high": fragment("优先级 500", priority=500),
        }
    )
    got = await recall_for(memory, priority_floor=100).recall("q", CORRELATION, CancelToken())
    assert [item.priority for item in got] == [100, 500]


async def test_nothing_else_about_the_fragment_is_rewritten() -> None:
    """`trust` 尤其不改：声明 `SYSTEM` 的记忆会进系统指令位置，那与一个 Context Provider
    声明 `SYSTEM` 是同一件事、同一份 manifest 担保。写入侧的 trust 判定归后端自己。"""
    original = fragment("我是系统指令", priority=200, trust=TrustLevel.SYSTEM)
    memory = FakeMemory(fragments={"r1": original})
    (got,) = await recall_for(memory).recall("q", CORRELATION, CancelToken())
    assert got is original


async def test_recall_order_is_the_backends_order() -> None:
    """`MemoryProvider.recall` 的映射「顺序即相关性排序」，这条路径不重排。"""
    memory = FakeMemory(
        fragments={
            "a": fragment("最相关", priority=100),
            "b": fragment("次相关", priority=101),
            "c": fragment("第三", priority=102),
        }
    )
    got = await recall_for(memory).recall("q", CORRELATION, CancelToken())
    assert [item.content for item in got] == ["最相关", "次相关", "第三"]


# ------------------------------------------------------------------  分叉


async def test_a_broken_backend_degrades_by_default() -> None:
    """这一轮没有记忆，turn 照常跑。**但错误一定被报出去**——降级不等于静默。"""
    memory = FakeMemory(fails=KaryviaError(ErrorCode.PERSISTENCE_READ_FAILED, "后端挂了。"))
    reported: list[KaryviaError] = []

    got = await recall_for(memory).recall(
        "q", CORRELATION, CancelToken(), on_failure=reported.append
    )

    assert got == ()
    assert [error.code for error in reported] == [ErrorCode.PERSISTENCE_READ_FAILED]


async def test_on_failure_fail_makes_the_turn_fail() -> None:
    memory = FakeMemory(fails=KaryviaError(ErrorCode.PERSISTENCE_READ_FAILED, "后端挂了。"))
    with pytest.raises(KaryviaError) as caught:
        await recall_for(memory, critical=True).recall("q", CORRELATION, CancelToken())
    assert caught.value.code is ErrorCode.PERSISTENCE_READ_FAILED


async def test_a_timeout_is_reported_with_its_own_code() -> None:
    memory = FakeMemory(delay=0.2)
    reported: list[KaryviaError] = []

    got = await recall_for(memory, timeout_ms=10).recall(
        "q", CORRELATION, CancelToken(), on_failure=reported.append
    )

    assert got == ()
    assert [error.code for error in reported] == [ErrorCode.TIMEOUT_HOOK]
    assert reported[0].detail["timeout_ms"] == 10


async def test_a_raw_exception_keeps_only_its_type_name() -> None:
    """第三方后端的异常文本可能带着连接串。"""
    memory = FakeMemory(fails=RuntimeError("postgres://user:pw@host/db"))
    reported: list[KaryviaError] = []

    await recall_for(memory).recall("q", CORRELATION, CancelToken(), on_failure=reported.append)

    assert reported[0].code is ErrorCode.PLUGIN_HOOK_FAILED
    assert dict(reported[0].detail)["exception"] == "RuntimeError"
    assert "postgres" not in str(dict(reported[0].detail))


async def test_cancellation_is_not_a_backend_failure() -> None:
    """取消**不走降级**：它不是记忆后端的故障而是这条 turn 该停了。

    把它折成「这轮没有记忆」会让一条已被取消的 turn 带着半份上下文继续跑。判据是
    `ErrorCategory` 而不是逐个列举错误码。
    """
    memory = FakeMemory(fails=KaryviaError(ErrorCode.CANCELLED_BY_USER, "停。"))
    reported: list[KaryviaError] = []

    with pytest.raises(KaryviaError) as caught:
        await recall_for(memory).recall(
            "q", CORRELATION, CancelToken(), on_failure=reported.append
        )

    assert caught.value.code is ErrorCode.CANCELLED_BY_USER
    assert reported == [], "取消不该被当成一次可降级的后端故障报出去"


# ------------------------------------------------------------------ 选后端


def test_a_named_backend_that_is_not_there_is_capability_missing() -> None:
    """静默退回「没有记忆」会让用户以为记忆在工作。错误里列出实际有哪几条。"""
    with pytest.raises(KaryviaError) as caught:
        select_memory([("jsonl", "plugin:memory", FakeMemory())], "sqlite")  # type: ignore[list-item]
    assert caught.value.code is ErrorCode.CAPABILITY_MISSING
    assert caught.value.detail["available"] == ["jsonl"]
    assert caught.value.detail["field"] == "/memory/provider"


def test_the_named_backend_is_the_one_returned() -> None:
    first, second = FakeMemory(), FakeMemory()
    candidates = [("jsonl", "plugin:a", first), ("sqlite", "plugin:b", second)]
    name, owner, provider = select_memory(candidates, "sqlite")  # type: ignore[arg-type]
    assert (name, owner) == ("sqlite", "plugin:b")
    assert provider is second


# --------------------------------------------- 组装器里的落点：与其余片段完全同等


async def assembled(memory: MemoryRecall | None, **kwargs: object) -> object:
    from karyvia.contracts import SessionSnapshot

    return await assemble(
        snapshot=SessionSnapshot(session_key=KEY),
        user_input="记得我的偏好吗",
        correlation=CORRELATION,
        cancel=CancelToken(),
        memory=memory,
        now=NOW,
        **kwargs,  # type: ignore[arg-type]
    )


async def test_recalled_memory_reaches_the_model_request() -> None:
    """整条路径的功能形态：注册一条 `MEMORY`、配上名字，记忆就真的进了模型消息。

    这是  留下的缺口——那时只注册一条 `MEMORY` 能力，记忆永远进不了模型。
    """
    memory = FakeMemory(fragments={"r1": fragment("用户偏好简短回答", priority=100)})
    context = await assembled(recall_for(memory))
    rendered = "\n".join(message.content for message in context.messages)  # type: ignore[attr-defined]
    assert "用户偏好简短回答" in rendered


async def test_a_secret_memory_is_dropped_like_any_other_fragment() -> None:
    """召回出来的片段没有旁路：`sensitivity=SECRET` 照样进不了请求，且记进 `dropped`。"""
    secret = ContextFragment(
        source="plugin:memory",
        kind=FragmentKind.MEMORY,
        content="密钥是 sk-xxx",
        priority=100,
        estimated_tokens=4,
        scope=FragmentScope.AGENT,
        trust=TrustLevel.UNTRUSTED,
        sensitivity=Sensitivity.SECRET,
    )
    context = await assembled(recall_for(FakeMemory(fragments={"r1": secret})))
    rendered = "\n".join(message.content for message in context.messages)  # type: ignore[attr-defined]
    assert "sk-xxx" not in rendered
    assert [item.reason for item in context.dropped] == ["sensitivity"]  # type: ignore[attr-defined]


async def test_no_memory_means_no_change_at_all() -> None:
    """`memory=None`（默认）时组装结果与此前逐字相同。"""
    context = await assembled(None)
    assert context.fragments == ()  # type: ignore[attr-defined]
    assert [message.content for message in context.messages] == ["记得我的偏好吗"]  # type: ignore[attr-defined]
