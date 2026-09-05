"""三条工具的用例：声明、参数校验、副作用档位与失败折叠。

职责：`memory.remember` / `memory.recall` / `memory.forget` 的可断言行为。
不负责：存储（`test_memory_store.py`）、自动召回（`test_memory_provider.py`）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _memory_fakes import KEY, Cancelled, Clock, NoCancel, make_fragment, make_invocation
from karyvia_plugin_memory.settings import MemorySettings
from karyvia_plugin_memory.store import MemoryStore
from karyvia_plugin_memory.tools import (
    FORGET_TOOL,
    RECALL_TOOL,
    REMEMBER_TOOL,
    TOOL_NAMES,
    MemoryForgetTool,
    MemoryRecallTool,
    MemoryRememberTool,
    forget_spec,
    recall_spec,
    remember_spec,
)

from karyvia.contracts import (
    Concurrency,
    ErrorCategory,
    ErrorCode,
    FragmentScope,
    RiskLevel,
    SideEffect,
    ToolSpec,
    TrustLevel,
)
from karyvia.sdk.testing import ToolContract


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "memory", now=Clock())


def _settings(**overrides: object) -> MemorySettings:
    return MemorySettings(**overrides)  # type: ignore[arg-type]


# ------------------------------------------------------------------------ 契约基类


class TestRecallToolContract(ToolContract):
    """只读那条走通用契约。写入那两条不走：契约基类会真的调用它们，而
    `make_tool()` 拿不到 `tmp_path`——让它们往一个进程级临时目录里写没有意义。"""

    def make_tool(self) -> tuple[ToolSpec, object]:
        import tempfile

        store = MemoryStore(Path(tempfile.mkdtemp(prefix="memory-tool-")))
        return recall_spec(), MemoryRecallTool(store, _settings())

    def valid_arguments(self) -> dict[str, object]:
        return {"query": "深色模式"}

    def invalid_arguments(self) -> dict[str, object] | None:
        return {"query": 42}


# ---------------------------------------------------------------------------- 声明


def test_tool_names_are_the_single_source() -> None:
    """manifest 的声明与 `register()` 的注册都从这一份来——两处各写一遍迟早分叉。"""
    assert TOOL_NAMES == (REMEMBER_TOOL, RECALL_TOOL, FORGET_TOOL)
    assert [spec().name for spec in (remember_spec, recall_spec, forget_spec)] == list(TOOL_NAMES)


def test_recall_is_read_only_and_safe() -> None:
    spec = recall_spec()
    assert spec.read_only is True
    assert spec.risk is RiskLevel.SAFE


def test_remember_is_mutating_not_destructive() -> None:
    """写一条新记忆只是追加，既不覆盖也不删除别的记忆。"""
    spec = remember_spec()
    assert spec.risk is RiskLevel.MUTATING
    assert spec.read_only is False


def test_forget_is_destructive() -> None:
    """删除不可撤销——本插件不留墓碑。"""
    assert forget_spec().risk is RiskLevel.DESTRUCTIVE


def test_writing_tools_are_exclusive() -> None:
    """两次并发写同一分区的结果取决于顺序，而 turn 内的并行调度不保证顺序。"""
    assert remember_spec().concurrency is Concurrency.EXCLUSIVE
    assert forget_spec().concurrency is Concurrency.EXCLUSIVE
    assert recall_spec().concurrency is Concurrency.PARALLEL


def test_schemas_reject_extra_properties() -> None:
    for spec in (remember_spec(), recall_spec(), forget_spec()):
        assert spec.parameters["additionalProperties"] is False


# ------------------------------------------------------------------ memory.remember


async def test_remember_writes_and_reports_the_record_id(store: MemoryStore) -> None:
    tool = MemoryRememberTool(store, _settings())
    result = await tool.execute(
        make_invocation(REMEMBER_TOOL, {"content": "用户偏好深色模式"}), NoCancel()
    )
    assert result.ok
    assert result.side_effect is SideEffect.OCCURRED
    assert result.data is not None
    stored = result.data["record_id"]
    record = await store.get(str(stored))
    assert record is not None
    assert record.content == "用户偏好深色模式"
    assert record.origin == "tool"


async def test_remember_defaults_to_the_agent_scope(store: MemoryStore) -> None:
    tool = MemoryRememberTool(store, _settings())
    result = await tool.execute(make_invocation(REMEMBER_TOOL, {"content": "跨会话的事"}), NoCancel())
    assert result.data is not None
    assert result.data["scope"] == "agent"


async def test_remember_honours_an_explicit_scope(store: MemoryStore) -> None:
    tool = MemoryRememberTool(store, _settings())
    await tool.execute(
        make_invocation(REMEMBER_TOOL, {"content": "只属于这段对话", "scope": "session"}), NoCancel()
    )
    records = await store.entries(KEY, scopes=(FragmentScope.SESSION,))
    assert [record.content for record in records] == ["只属于这段对话"]


async def test_remember_stores_an_absolute_expiry(store: MemoryStore) -> None:
    """存绝对时间而不是相对天数：改天数的含义会随着「从什么时候起算」漂移。"""
    tool = MemoryRememberTool(store, _settings())
    result = await tool.execute(
        make_invocation(REMEMBER_TOOL, {"content": "临时的事", "ttl_days": 7}), NoCancel()
    )
    assert result.data is not None
    assert "expires_at" in result.data


async def test_remember_uses_the_session_key_from_the_correlation(store: MemoryStore) -> None:
    """工具侧唯一的身份来源，也是它能服务 session / workspace 范围的原因。"""
    from karyvia.contracts import SessionKey

    other = SessionKey(channel_id="cli", conversation_id="elsewhere", scope="proj")
    tool = MemoryRememberTool(store, _settings())
    await tool.execute(
        make_invocation(REMEMBER_TOOL, {"content": "别处的会话记忆", "scope": "session"}, key=other),
        NoCancel(),
    )
    assert await store.entries(KEY, scopes=(FragmentScope.SESSION,)) == ()
    assert len(await store.entries(other, scopes=(FragmentScope.SESSION,))) == 1


# -------------------------------------------------------------------- memory.recall


async def test_recall_finds_what_remember_wrote(store: MemoryStore) -> None:
    await MemoryRememberTool(store, _settings()).execute(
        make_invocation(REMEMBER_TOOL, {"content": "用户偏好深色模式"}), NoCancel()
    )
    result = await MemoryRecallTool(store, _settings()).execute(
        make_invocation(RECALL_TOOL, {"query": "深色模式"}), NoCancel()
    )
    assert result.ok
    assert "用户偏好深色模式" in result.content
    assert result.side_effect is SideEffect.NONE


async def test_recall_says_so_when_nothing_matches(store: MemoryStore) -> None:
    """查不到不是失败——模型需要知道「确实没有」而不是「工具坏了」。"""
    result = await MemoryRecallTool(store, _settings()).execute(
        make_invocation(RECALL_TOOL, {"query": "不存在的东西"}), NoCancel()
    )
    assert result.ok
    assert result.data is not None
    assert result.data["count"] == 0


async def test_recall_result_is_declared_untrusted(store: MemoryStore) -> None:
    """`D42` 起隔离由契约层完成（`fold_tool_result` 包成不可信数据块）。

    原来这里断言的是一行自己加的提醒文字，而那**是提醒不是隔离**——存进来的内容本来就
    统一按 `UNTRUSTED` 收（`record.from_fragment` 忽略调用方声明的 trust），召回时若改口
    说它可信，那条判定就作废了。现在两侧口径一致。
    """
    await store.add(KEY, make_fragment("深色模式"))
    result = await MemoryRecallTool(store, _settings()).execute(
        make_invocation(RECALL_TOOL, {"query": "深色模式"}), NoCancel()
    )
    assert result.trust is TrustLevel.UNTRUSTED


async def test_recall_narrows_to_one_scope_when_asked(store: MemoryStore) -> None:
    await store.add(KEY, make_fragment("实例级的深色模式", scope=FragmentScope.AGENT))
    await store.add(KEY, make_fragment("会话级的深色模式", scope=FragmentScope.SESSION))
    result = await MemoryRecallTool(store, _settings()).execute(
        make_invocation(RECALL_TOOL, {"query": "深色模式", "scope": "session"}), NoCancel()
    )
    assert "会话级的深色模式" in result.content
    assert "实例级的深色模式" not in result.content


async def test_recall_returns_record_ids_so_forget_can_use_them(store: MemoryStore) -> None:
    stored = await store.add(KEY, make_fragment("要能被删掉"))
    result = await MemoryRecallTool(store, _settings()).execute(
        make_invocation(RECALL_TOOL, {"query": "要能被删掉"}), NoCancel()
    )
    assert result.data is not None
    # `ToolResult.data` 过一遍 `normalize_metadata()`，列表在那里被冻结成元组。
    assert list(result.data["record_ids"]) == [stored]  # type: ignore[arg-type]


async def test_recall_truncates_an_oversized_result(store: MemoryStore) -> None:
    for index in range(20):
        await store.add(KEY, make_fragment(f"深色模式 第{index}条 " + "填充" * 50))
    result = await MemoryRecallTool(store, _settings(max_result_chars=100, recall_limit=20)).execute(
        make_invocation(RECALL_TOOL, {"query": "深色模式"}), NoCancel()
    )
    assert result.truncated
    assert len(result.content) <= 100


# -------------------------------------------------------------------- memory.forget


async def test_forget_removes_the_record(store: MemoryStore) -> None:
    stored = await store.add(KEY, make_fragment("要删掉的"))
    result = await MemoryForgetTool(store, _settings()).execute(
        make_invocation(FORGET_TOOL, {"record_id": stored}), NoCancel()
    )
    assert result.ok
    assert result.side_effect is SideEffect.OCCURRED
    assert result.data is not None
    assert result.data["existed"] is True
    assert await store.get(stored) is None


async def test_forgetting_a_missing_record_is_not_a_failure_but_says_so(
    store: MemoryStore,
) -> None:
    """契约把它定义成 `forget() -> False`；但结果里要说清楚，否则模型会以为删掉了什么。"""
    from karyvia_plugin_memory.partition import partition_for

    missing = f"{partition_for(FragmentScope.AGENT, KEY).filename}#404"
    result = await MemoryForgetTool(store, _settings()).execute(
        make_invocation(FORGET_TOOL, {"record_id": missing}), NoCancel()
    )
    assert result.ok
    assert result.data is not None
    assert result.data["existed"] is False
    assert "什么都没删" in result.content


# ------------------------------------------------------------------------ 失败折叠


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (REMEMBER_TOOL, {"content": ""}),
        (REMEMBER_TOOL, {"content": "x", "scope": "nope"}),
        (REMEMBER_TOOL, {"content": "x", "scope": "user"}),
        (REMEMBER_TOOL, {"content": "x", "ttl_days": 0}),
        (REMEMBER_TOOL, {"content": "x", "ttl_days": 99_999}),
        (REMEMBER_TOOL, {"content": "x", "tags": "not-a-list"}),
        (REMEMBER_TOOL, {"content": "x", "surprise": 1}),
        (RECALL_TOOL, {}),
        (RECALL_TOOL, {"query": "x", "limit": 0}),
        (FORGET_TOOL, {"record_id": "malformed"}),
    ],
)
async def test_bad_arguments_come_back_as_a_failed_result_not_an_exception(
    store: MemoryStore, tool_name: str, arguments: dict[str, object]
) -> None:
    """逸出的异常会被 Kernel 记成 `side_effect=UNKNOWN`——那是一次谎报。"""
    tools = {
        REMEMBER_TOOL: MemoryRememberTool,
        RECALL_TOOL: MemoryRecallTool,
        FORGET_TOOL: MemoryForgetTool,
    }
    tool = tools[tool_name](store, _settings())
    result = await tool.execute(make_invocation(tool_name, arguments), NoCancel())  # type: ignore[arg-type]
    assert result.ok is False
    assert result.error is not None
    assert result.side_effect is SideEffect.NONE


async def test_no_tool_ever_reports_an_unknown_side_effect(store: MemoryStore) -> None:
    """本插件的可失败步骤全部发生在落盘之前，因此一次 `UNKNOWN` 都不产出。"""
    for tool_class, name, arguments in (
        (MemoryRememberTool, REMEMBER_TOOL, {"content": ""}),
        (MemoryRecallTool, RECALL_TOOL, {}),
        (MemoryForgetTool, FORGET_TOOL, {"record_id": "bad"}),
    ):
        result = await tool_class(store, _settings()).execute(
            make_invocation(name, arguments), NoCancel()  # type: ignore[arg-type]
        )
        assert result.side_effect is not SideEffect.UNKNOWN


async def test_an_already_requested_cancellation_stops_the_call_at_the_door(
    store: MemoryStore,
) -> None:
    result = await MemoryRecallTool(store, _settings()).execute(
        make_invocation(RECALL_TOOL, {"query": "x"}), Cancelled()
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.category is ErrorCategory.CANCELLED


async def test_a_secret_fragment_cannot_be_written_through_the_tool(store: MemoryStore) -> None:
    """工具构造的片段恒是 `NORMAL`，因此这条走的是 store 的拒绝路径而不是工具的。"""
    with pytest.raises(Exception) as caught:  # noqa: B017,PT011 - 见下方断言
        from karyvia.contracts import Sensitivity

        await store.add(KEY, make_fragment("token", sensitivity=Sensitivity.SECRET))
    assert getattr(caught.value, "code", None) is ErrorCode.INPUT_MALFORMED
