"""`/memory` 命令与 `settings.py` 的用例。

职责：五个子命令的行为、`forget` 的权限粒度、约定不抛，以及配置校验。
不负责：存储（`test_memory_store.py`）、工具（`test_memory_tools.py`）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _memory_fakes import KEY, Cancelled, Clock, NoCancel, make_command, make_fragment
from karyvia_plugin_memory.commands import (
    COMMAND_NAME,
    SUBCOMMANDS,
    MemoryCommand,
    memory_spec,
)
from karyvia_plugin_memory.partition import partition_for
from karyvia_plugin_memory.settings import (
    CONFIG_SCHEMA,
    DEFAULT_ENABLED_SCOPES,
    MemorySettings,
    resolve_settings,
)
from karyvia_plugin_memory.store import MemoryStore

from karyvia.contracts import (
    Disposition,
    ErrorCategory,
    ErrorCode,
    FragmentScope,
    JsonValue,
    KaryviaError,
)


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "memory", now=Clock())


def _command(store: MemoryStore, **overrides: object) -> MemoryCommand:
    return MemoryCommand(store, MemorySettings(**overrides))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------- 声明


def test_the_command_is_not_operator_only() -> None:
    """读自己的记忆不该要管理员。"""
    assert memory_spec().operator_only is False


def test_the_trailing_parameter_is_repeated() -> None:
    """`/memory search 深色 模式` 是完全正常的敲法；不声明会被按「参数过多」拒掉。"""
    assert memory_spec().parameters[-1].repeated is True


def test_the_command_name_does_not_collide_with_the_builtin_command_set() -> None:
    """命令名与别名在同一个命名空间里判定（`build_command_index`）。

    `R4` 让本插件够不着 `builtins/commands_core`，因此这里对照的是那六个名字的字面量。
    """
    builtin = {"help", "config", "session", "plugins", "capabilities", "cancel"}
    spec = memory_spec()
    assert not builtin & ({spec.name, *spec.aliases})


def test_subcommands_are_the_single_source_of_the_usage_text() -> None:
    assert [name for name, _ in SUBCOMMANDS] == ["list", "search", "show", "add", "forget"]


# ------------------------------------------------------------------------ 子命令


async def test_no_arguments_prints_the_usage(store: MemoryStore) -> None:
    result = await _command(store).handle(make_command([]), NoCancel())
    assert result.disposition is Disposition.COMMAND_HANDLED
    for name, _ in SUBCOMMANDS:
        assert name in result.content


async def test_list_on_an_empty_store(store: MemoryStore) -> None:
    result = await _command(store).handle(make_command(["list"]), NoCancel())
    assert result.disposition is Disposition.COMMAND_HANDLED
    assert "还没有" in result.content


async def test_list_shows_recent_memories(store: MemoryStore) -> None:
    for index in range(3):
        await store.add(KEY, make_fragment(f"第 {index} 条"))
    result = await _command(store).handle(make_command(["list"]), NoCancel())
    assert "第 2 条" in result.content
    assert "共 3 条" in result.content


async def test_list_respects_the_limit(store: MemoryStore) -> None:
    for index in range(10):
        await store.add(KEY, make_fragment(f"第 {index} 条"))
    result = await _command(store, list_limit=2).handle(make_command(["list"]), NoCancel())
    assert "最近 2 条" in result.content


async def test_search_joins_the_trailing_arguments(store: MemoryStore) -> None:
    """dispatcher 按空白切分参数，而 `/memory search 深色 模式` 的意图是一个短语。"""
    await store.add(KEY, make_fragment("用户偏好深色模式"))
    result = await _command(store).handle(make_command(["search", "深色", "模式"]), NoCancel())
    assert "用户偏好深色模式" in result.content


async def test_search_says_so_when_nothing_matches(store: MemoryStore) -> None:
    result = await _command(store).handle(make_command(["search", "不存在"]), NoCancel())
    assert "没有找到" in result.content


async def test_show_prints_the_full_record(store: MemoryStore) -> None:
    stored = await store.add(KEY, make_fragment("很长的" * 100), tags=("a", "b"))
    result = await _command(store).handle(make_command(["show", stored]), NoCancel())
    assert result.disposition is Disposition.COMMAND_HANDLED
    assert stored in result.content
    assert "a, b" in result.content
    assert result.content.endswith("很长的" * 100)


async def test_list_truncates_but_show_does_not(store: MemoryStore) -> None:
    """摘要一行读得完，全文用 `show`——这是两个子命令并存的理由。"""
    long = "长内容" * 100
    stored = await store.add(KEY, make_fragment(long))
    listed = await _command(store).handle(make_command(["list"]), NoCancel())
    assert long not in listed.content
    shown = await _command(store).handle(make_command(["show", stored]), NoCancel())
    assert long in shown.content


async def test_show_rejects_a_missing_record(store: MemoryStore) -> None:
    missing = f"{partition_for(FragmentScope.AGENT, KEY).filename}#404"
    result = await _command(store).handle(make_command(["show", missing]), NoCancel())
    assert result.disposition is Disposition.REJECTED
    assert result.error is not None


async def test_add_writes_through_the_same_path_as_the_tool(store: MemoryStore) -> None:
    """`trust` 被统一成 `UNTRUSTED`：敲命令的人不会因此获得指令优先级。"""
    from karyvia_plugin_memory.record import to_fragment

    from karyvia.contracts import TrustLevel

    result = await _command(store).handle(make_command(["add", "手动", "记一条"]), NoCancel())
    assert result.disposition is Disposition.COMMAND_HANDLED
    records = await store.entries(KEY, scopes=(FragmentScope.AGENT,))
    assert [record.content for record in records] == ["手动 记一条"]
    assert records[0].origin == "command"
    assert to_fragment(records[0], priority=1).trust is TrustLevel.UNTRUSTED


async def test_add_refuses_oversized_content(store: MemoryStore) -> None:
    from karyvia_plugin_memory.record import MAX_CONTENT_CHARS

    result = await _command(store).handle(make_command(["add", "字" * (MAX_CONTENT_CHARS + 1)]), NoCancel())
    assert result.disposition is Disposition.REJECTED
    assert result.error is not None
    assert result.error.code is ErrorCode.INPUT_TOO_LARGE


# ------------------------------------------------------------------------ 权限粒度


async def test_forgetting_a_session_memory_needs_no_operator(store: MemoryStore) -> None:
    stored = await store.add(KEY, make_fragment("会话级的", scope=FragmentScope.SESSION))
    result = await _command(store).handle(make_command(["forget", stored], is_operator=False), NoCancel())
    assert result.disposition is Disposition.COMMAND_HANDLED
    assert await store.get(stored) is None


@pytest.mark.parametrize("scope", [FragmentScope.AGENT, FragmentScope.WORKSPACE])
async def test_forgetting_a_shared_memory_needs_an_operator(
    store: MemoryStore, scope: FragmentScope
) -> None:
    """那两个分区是全实例共享的，群聊里任何人都能删掉它们不合理。"""
    stored = await store.add(KEY, make_fragment("共享的", scope=scope))
    result = await _command(store).handle(make_command(["forget", stored], is_operator=False), NoCancel())
    assert result.disposition is Disposition.REJECTED
    assert result.error is not None
    assert result.error.category is ErrorCategory.PERMISSION_DENIED
    assert await store.get(stored) is not None, "被拒绝之后那条记忆必须还在"


@pytest.mark.parametrize("scope", [FragmentScope.AGENT, FragmentScope.WORKSPACE])
async def test_an_operator_can_forget_shared_memories(
    store: MemoryStore, scope: FragmentScope
) -> None:
    stored = await store.add(KEY, make_fragment("共享的", scope=scope))
    result = await _command(store).handle(make_command(["forget", stored], is_operator=True), NoCancel())
    assert result.disposition is Disposition.COMMAND_HANDLED
    assert await store.get(stored) is None


async def test_forgetting_a_missing_record_says_so(store: MemoryStore) -> None:
    missing = f"{partition_for(FragmentScope.SESSION, KEY).filename}#404"
    result = await _command(store).handle(make_command(["forget", missing]), NoCancel())
    assert result.disposition is Disposition.COMMAND_HANDLED
    assert "没有这条记忆" in result.content


# ------------------------------------------------------------------------ 约定不抛


@pytest.mark.parametrize(
    "args",
    [
        ["nosuchsubcommand"],
        ["show"],
        ["show", "a", "b"],
        ["forget"],
        ["search"],
        ["add"],
        ["forget", "malformed-id"],
    ],
)
async def test_bad_input_is_rejected_not_raised(store: MemoryStore, args: list[str]) -> None:
    result = await _command(store).handle(make_command(args), NoCancel())
    assert result.disposition is Disposition.REJECTED
    assert result.error is not None


async def test_the_session_stays_usable_after_a_rejection(store: MemoryStore) -> None:
    """ 的可断言形态：炸掉一次之后，下一条命令照常跑。"""
    command = _command(store)
    assert (await command.handle(make_command(["nope"]), NoCancel())).disposition is Disposition.REJECTED
    assert (await command.handle(make_command(["list"]), NoCancel())).disposition is Disposition.COMMAND_HANDLED


async def test_an_unexpected_exception_becomes_a_rejection_without_its_message(
    tmp_path: Path,
) -> None:
    """**只放类型名不放异常消息**——第三方栈的异常文本可能带着凭据或宿主机路径。"""

    class Exploding(MemoryStore):
        async def entries(self, key: object, *, scopes: object) -> tuple[()]:  # type: ignore[override]

            raise RuntimeError("sk-secret-token-must-not-appear-1234")

    command = MemoryCommand(Exploding(tmp_path / "memory"), MemorySettings())
    result = await command.handle(make_command(["list"]), NoCancel())
    assert result.disposition is Disposition.REJECTED
    assert result.error is not None
    assert result.error.code is ErrorCode.KERNEL_INVARIANT_VIOLATED
    rendered = repr(result.error) + str(result.error.detail) + result.error.user_message
    assert "sk-secret-token" not in rendered
    assert result.error.detail["cause"] == "RuntimeError"


async def test_cancellation_is_not_swallowed(tmp_path: Path) -> None:
    """捕 `Exception` 不捕 `BaseException`：取消与 Ctrl-C 要放行。"""
    import asyncio

    class Cancelling(MemoryStore):
        async def entries(self, key: object, *, scopes: object) -> tuple[()]:  # type: ignore[override]

            raise asyncio.CancelledError

    command = MemoryCommand(Cancelling(tmp_path / "memory"), MemorySettings())
    with pytest.raises(asyncio.CancelledError):
        await command.handle(make_command(["list"]), NoCancel())


# ---------------------------------------------------------------------------- 配置


def test_defaults_need_no_configuration() -> None:
    """装上就能用：默认三个范围全开、自动召回打开。"""
    settings = resolve_settings({})
    assert settings.auto_recall is True
    assert tuple(scope.value for scope in settings.enabled_scopes) == DEFAULT_ENABLED_SCOPES
    assert settings.fragment_priority > 0


def test_the_config_schema_forbids_unknown_keys() -> None:
    assert CONFIG_SCHEMA["additionalProperties"] is False


def test_scope_order_comes_from_the_module_not_the_config() -> None:
    """「调一下数组里的顺序」不该变成一次行为变更。"""
    written_backwards: dict[str, JsonValue] = {"enabled_scopes": ["agent", "session"]}
    settings = resolve_settings(written_backwards)
    assert [scope.value for scope in settings.enabled_scopes] == ["session", "agent"]


@pytest.mark.parametrize(
    "config",
    [
        {"dir": 1},
        {"auto_recall": 1},  # `1` 不是 `True`
        {"auto_recall": "yes"},
        {"recall_limit": 0},
        {"recall_limit": True},  # `True` 是 `int` 的实例
        {"min_score": -1},
        {"fragment_priority": 0},
        {"list_limit": -3},
        {"max_result_chars": 0},
        {"enabled_scopes": "agent"},
        {"enabled_scopes": [1]},
        {"enabled_scopes": ["nosuchscope"]},
        {"enabled_scopes": ["user"]},  # 合法的 FragmentScope，但本实现服务不了它
        {"enabled_scopes": []},
    ],
)
def test_bad_configuration_is_rejected_with_a_key_path(config: dict[str, JsonValue]) -> None:
    with pytest.raises(KaryviaError) as caught:
        resolve_settings(config)
    assert caught.value.code is ErrorCode.CONFIG_INVALID
    assert str(caught.value.detail["key"]).startswith("plugins.memory.config.")


def test_command_name_is_stable() -> None:
    assert COMMAND_NAME == "memory"


async def test_an_already_requested_cancellation_is_rejected_not_raised(
    store: MemoryStore,
) -> None:
    """契约原文：被取消时**返回** `REJECTED` 并在 `error` 里说明，而不是抛出。"""
    result = await _command(store).handle(make_command(["list"]), Cancelled())
    assert result.disposition is Disposition.REJECTED
    assert result.error is not None
    assert result.error.category is ErrorCategory.CANCELLED
