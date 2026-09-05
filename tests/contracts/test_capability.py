"""能力契约测试（`D04`，技术方案 §6.1、§6.6、需求 §9.3）。

两张常量表是本文件的重点：`CAPABILITY_ARITY` 与 `HOOK_KINDS` 都以**字面量**写在测试里，
再与实现比对——从实现反推期望值的测试只能证明代码没改，证明不了它和技术方案一致。
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from karyvia.contracts import (
    CAPABILITY_ARITY,
    HOOK_KINDS,
    HOOK_REQUIRED_SLOTS,
    Builtin,
    CapabilityArity,
    CapabilityKind,
    CapabilityRef,
    ContextFragment,
    Correlation,
    ErrorCode,
    FragmentKind,
    FragmentScope,
    HookAction,
    HookContext,
    HookKind,
    HookName,
    HookOutcome,
    InboundMessage,
    InstanceId,
    KaryviaError,
    ModelRequest,
    ModelResponse,
    Plugin,
    PluginId,
    Role,
    Sender,
    SessionKey,
    StopReason,
    ToolCall,
    ToolInvocation,
    ToolResult,
    TrustLevel,
    TurnId,
    TurnOutcome,
    TurnStatus,
    parse_capability_target,
    parse_provider,
    provider_sort_key,
)
from karyvia.contracts.model import ModelMessage
from karyvia.contracts.tool import SideEffect

CORRELATION = Correlation(InstanceId("default"), SessionKey("cli", "local"), TurnId("t-1"))
NOW = datetime(2026, 8, 11, tzinfo=UTC)

#: 技术方案 §6.1 的表格，逐行抄写。`MEMORY` 一行是 `D04` 补齐的：原表只列了 8 个 kind，
#: 决议见 `docs/project/README.md`——带 name 的注册方法本身就意味着可并存多个具名实现。
EXPECTED_ARITY: dict[CapabilityKind, CapabilityArity] = {
    CapabilityKind.TOOL: CapabilityArity.MULTI_UNIQUE,
    CapabilityKind.COMMAND: CapabilityArity.MULTI_UNIQUE,
    CapabilityKind.CONTEXT: CapabilityArity.MULTI,
    CapabilityKind.TURN_COMPACTOR: CapabilityArity.MULTI_UNIQUE,
    CapabilityKind.HOOK: CapabilityArity.MULTI,
    CapabilityKind.CHANNEL: CapabilityArity.MULTI_UNIQUE,
    CapabilityKind.MODEL: CapabilityArity.MULTI_UNIQUE,
    CapabilityKind.MEMORY: CapabilityArity.MULTI_UNIQUE,
    CapabilityKind.SESSION_STORE: CapabilityArity.SINGLETON,
    CapabilityKind.CLI_ENTRY: CapabilityArity.SINGLETON,
}

#: 技术方案 §6.6 的 Hook 表格，逐行抄写。
EXPECTED_HOOK_KINDS: dict[HookName, HookKind] = {
    HookName.INSTANCE_READY: HookKind.OBSERVER,
    HookName.INSTANCE_SHUTDOWN: HookKind.OBSERVER,
    HookName.TURN_START: HookKind.INTERCEPTOR,
    HookName.CONTEXT_ASSEMBLE: HookKind.INTERCEPTOR,
    HookName.BEFORE_MODEL_REQUEST: HookKind.INTERCEPTOR,
    HookName.AFTER_MODEL_RESPONSE: HookKind.OBSERVER,
    HookName.BEFORE_TOOL_CALL: HookKind.INTERCEPTOR,
    HookName.AFTER_TOOL_CALL: HookKind.INTERCEPTOR,
    HookName.TURN_END: HookKind.OBSERVER,
}


def fragment(**overrides: object) -> ContextFragment:
    base: dict[str, object] = {
        "source": "builtin:context_basic",
        "kind": FragmentKind.SYSTEM,
        "content": "你是一个助手。",
        "priority": 0,
        "estimated_tokens": 8,
        "scope": FragmentScope.SESSION,
        "trust": TrustLevel.SYSTEM,
    }
    base.update(overrides)
    return ContextFragment(**base)  # pyright: ignore[reportArgumentType]


def message() -> InboundMessage:
    return InboundMessage(
        message_id="m-1",
        instance_id=InstanceId("default"),
        channel_id="cli",
        conversation_id="local",
        sender=Sender("u-1"),
        content="你好",
        timestamp=NOW,
    )


def invocation() -> ToolInvocation:
    return ToolInvocation(ToolCall("c-1", "fs.read"), CORRELATION, timeout_ms=1000)


def tool_result() -> ToolResult:
    return ToolResult("c-1", ok=True, content="ok", truncated=False, side_effect=SideEffect.NONE)


def model_request() -> ModelRequest:
    return ModelRequest("gpt-x", (ModelMessage(Role.USER, "hi"),), CORRELATION)


def outcome() -> TurnOutcome:
    return TurnOutcome(CORRELATION, TurnStatus.COMPLETED, started_at=NOW, finished_at=NOW)


# ------------------------------------------------------------------------- arity 表


def test_capability_kind_has_exactly_ten_values() -> None:
    """10 个 kind 与 `sdk.KaryviaAPI` 的 10 个注册方法一一对应（技术方案 §7.5）。"""
    assert len(CapabilityKind) == 10


@pytest.mark.parametrize(("kind", "arity"), list(EXPECTED_ARITY.items()), ids=lambda x: str(x))
def test_arity_table_matches_technical_design(kind: CapabilityKind, arity: CapabilityArity) -> None:
    assert CAPABILITY_ARITY[kind] is arity
    assert kind.arity is arity


def test_every_kind_is_registered_in_the_arity_table() -> None:
    """缺项不是「暂未决定」，而是「有注册路径却没有冲突语义」。"""
    assert set(CAPABILITY_ARITY) == set(CapabilityKind)
    assert set(EXPECTED_ARITY) == set(CapabilityKind)


def test_arity_table_is_read_only() -> None:
    with pytest.raises(TypeError):
        CAPABILITY_ARITY[CapabilityKind.TOOL] = CapabilityArity.MULTI  # type: ignore[index]


# ------------------------------------------------------------------------ ProviderId


def test_provider_rendering_round_trip() -> None:
    for provider in (Builtin(), Plugin(PluginId("memory-sqlite"))):
        assert parse_provider(str(provider)) == provider


def test_builtin_sorts_before_any_plugin() -> None:
    """§6.1 的「内建优先」不需要排序特例——字典序已经保证了它。"""
    providers = [Plugin(PluginId("aaa")), Builtin(), Plugin(PluginId("zzz"))]
    assert [str(p) for p in sorted(providers, key=provider_sort_key)] == [
        "builtin",
        "plugin:aaa",
        "plugin:zzz",
    ]


@pytest.mark.parametrize("text", ["", "builtin:", "plugin", "plugin:", "other:x", "Builtin"])
def test_parse_provider_rejects_malformed(text: str) -> None:
    with pytest.raises(KaryviaError) as excinfo:
        parse_provider(text)
    assert excinfo.value.code is ErrorCode.INPUT_MALFORMED


def test_plugin_id_shape_is_validated() -> None:
    with pytest.raises(KaryviaError):
        Plugin(PluginId("Memory_SQLite"))


# ---------------------------------------------------------------------- CapabilityRef


def test_capability_ref_target_round_trips() -> None:
    for provider in (Builtin(), Plugin(PluginId("memory-sqlite"))):
        ref = CapabilityRef(CapabilityKind.TOOL, "fs.read", provider, version="1.2.0")
        assert parse_capability_target(ref.target) == (provider, "fs.read")


def test_capability_ref_exposes_arity_and_sort_key() -> None:
    ref = CapabilityRef(CapabilityKind.SESSION_STORE, "jsonl", Builtin())
    assert ref.arity is CapabilityArity.SINGLETON
    assert ref.sort_key == ("builtin", "jsonl")


@pytest.mark.parametrize("name", ["FS.read", "-fs", "fs read", ".fs"])
def test_capability_ref_rejects_malformed_names(name: str) -> None:
    with pytest.raises(KaryviaError):
        CapabilityRef(CapabilityKind.TOOL, name, Builtin())


@pytest.mark.parametrize("text", ["fs.read", "builtin", "plugin:memory-sqlite"])
def test_parse_capability_target_rejects_incomplete(text: str) -> None:
    with pytest.raises(KaryviaError):
        parse_capability_target(text)


def test_capability_ref_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        CapabilityRef(CapabilityKind.TOOL, "fs.read", Builtin()).name = "x"


def test_karyvia_error_carries_capability() -> None:
    """`errors.py` 里承诺随 `D04` 补上的字段（`PLG-006`：谁的问题要能一眼看出）。"""
    ref = CapabilityRef(CapabilityKind.MODEL, "openai-compat", Plugin(PluginId("acme")))
    error = KaryviaError(ErrorCode.CAPABILITY_MISSING, "能力缺失。", capability=ref)
    assert error.capability is ref
    assert error.with_correlation(CORRELATION).capability is ref


# ------------------------------------------------------------------------------ Hook


def test_hook_names_are_frozen_at_nine() -> None:
    assert len(HookName) == 9
    assert set(HOOK_KINDS) == set(HookName)
    assert set(HOOK_REQUIRED_SLOTS) == set(HookName)


@pytest.mark.parametrize(
    ("hook", "kind"), list(EXPECTED_HOOK_KINDS.items()), ids=lambda x: str(x)
)
def test_hook_kind_table_matches_technical_design(hook: HookName, kind: HookKind) -> None:
    assert HOOK_KINDS[hook] is kind
    assert hook.kind is kind


def test_four_observers_and_five_interceptors() -> None:
    observers = [hook for hook in HookName if hook.kind is HookKind.OBSERVER]
    interceptors = [hook for hook in HookName if hook.kind is HookKind.INTERCEPTOR]
    assert len(observers) == 4
    assert len(interceptors) == 5


def test_hook_context_requires_its_slots() -> None:
    with pytest.raises(KaryviaError) as excinfo:
        HookContext(HookName.BEFORE_TOOL_CALL, correlation=CORRELATION)
    assert excinfo.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED
    assert excinfo.value.detail["missing"] == ["invocation"]


def test_hook_context_reports_every_missing_slot() -> None:
    with pytest.raises(KaryviaError) as excinfo:
        HookContext(HookName.AFTER_TOOL_CALL)
    assert excinfo.value.detail["missing"] == ["correlation", "invocation", "result"]


def test_instance_hooks_need_no_correlation() -> None:
    """实例启动与停止时还没有会话与 turn（`OBS-001` 允许实例级事件无关联标识）。"""
    assert HookContext(HookName.INSTANCE_READY).correlation is None


@pytest.mark.parametrize(
    ("hook", "kwargs"),
    [
        (HookName.TURN_START, {"message": message()}),
        (HookName.CONTEXT_ASSEMBLE, {"fragments": (fragment(),)}),
        (HookName.BEFORE_MODEL_REQUEST, {"request": model_request()}),
        (
            HookName.AFTER_MODEL_RESPONSE,
            {"response": ModelResponse("gpt-x", StopReason.END_TURN, content="hi")},
        ),
        (HookName.BEFORE_TOOL_CALL, {"invocation": invocation()}),
        (HookName.AFTER_TOOL_CALL, {"invocation": invocation(), "result": tool_result()}),
        (HookName.TURN_END, {"outcome": outcome()}),
    ],
    ids=lambda x: str(x),
)
def test_hook_context_accepts_its_required_slots(hook: HookName, kwargs: dict[str, object]) -> None:
    context = HookContext(hook, correlation=CORRELATION, **kwargs)  # pyright: ignore[reportArgumentType]
    assert context.hook is hook


def test_hook_context_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        HookContext(HookName.INSTANCE_READY).hook = HookName.TURN_END


# --------------------------------------------------------------------- HookOutcome


def test_continue_carries_no_payload() -> None:
    assert HookOutcome(HookAction.CONTINUE).reason == ""
    with pytest.raises(KaryviaError) as excinfo:
        HookOutcome(HookAction.CONTINUE, fragments=(fragment(),))
    assert excinfo.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


@pytest.mark.parametrize(
    "payload",
    [
        {"fragments": (fragment(),)},
        {"request": model_request()},
        {"invocation": invocation()},
        {"result": tool_result()},
    ],
    ids=["fragments", "request", "invocation", "result"],
)
def test_replace_accepts_exactly_one_payload(payload: dict[str, object]) -> None:
    assert HookOutcome(HookAction.REPLACE, **payload).action is HookAction.REPLACE  # pyright: ignore[reportArgumentType]


def test_replace_rejects_zero_or_two_payloads() -> None:
    with pytest.raises(KaryviaError):
        HookOutcome(HookAction.REPLACE)
    with pytest.raises(KaryviaError):
        HookOutcome(HookAction.REPLACE, fragments=(fragment(),), result=tool_result())


@pytest.mark.parametrize("action", [HookAction.REJECT, HookAction.BLOCK])
def test_reject_and_block_require_a_reason(action: HookAction) -> None:
    with pytest.raises(KaryviaError):
        HookOutcome(action)
    assert HookOutcome(action, reason="策略不允许").reason == "策略不允许"
