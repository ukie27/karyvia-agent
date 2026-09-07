"""内建 Context Provider `context_basic`当前行为。

| 验收项 | 测试 |
| --- | --- |
| 通过 `ContextProviderContract` 全部用例 | `TestBasicContextProvider` |
| 无 Memory / 检索插件时组装正常完成 | `TestUsableWithoutPlugins` |
| trust 分级与放置位置 | `TestTrustPlacement` |
| 片段提示值与最终消息结构计量分离 | `TestTokenEstimate` |
| 配置校验：类型、数组写法、自相矛盾的组合 | `TestSettings` |
| 内建以普通 manifest + `setup(api)` 注册 | `TestRegistration` |

两条写这些用例时的取舍：

- **和真的组装器对接，而不是只断言片段字段**。本内建产出什么，只有经
  `kernel/turn/context_builder.assemble()` 渲染成 `ModelMessage` 之后才谈得上「可用上下文」；
  `trust` 决定位置这条尤其如此——片段上写着 `OPERATOR` 不等于它真的没进 system 消息。
  测试可以 import `kernel/`（`R4` 只约束 `src/karyvia/builtins/`），实现不行。
- **时钟注入而不是冻结**。运行时事实片段的内容要能逐字符断言，注入一个固定 `clock` 比
  monkeypatch `datetime.now` 少一层魔法，也顺带证明了这个注入点确实存在。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

import pytest

from karyvia.builtins.context_basic import (
    BASELINE_INSTRUCTIONS,
    CAPABILITY_NAME,
    CONFIG_INSTRUCTIONS_KEY,
    CONFIG_RUNTIME_FACTS_KEY,
    CONFIG_USE_BASELINE_KEY,
    FRAGMENT_SOURCE,
    BasicContextProvider,
    BasicContextSettings,
    estimate_tokens,
    resolve_settings,
    setup,
)
from karyvia.builtins.registry import BUILTIN_MANIFESTS, CONTEXT_BASIC
from karyvia.contracts import (
    UNTRUSTED_DATA_PREFIX,
    Builtin,
    CapabilityKind,
    ContextProvider,
    ErrorCode,
    FragmentKind,
    FragmentScope,
    JsonValue,
    KaryviaError,
    Role,
    SessionKey,
    SessionMessage,
    SessionSnapshot,
    TrustLevel,
)
from karyvia.kernel.turn.context_builder import assemble
from karyvia.kernel.turn.context_builder import estimate_tokens as kernel_estimate_tokens
from karyvia.kernel.turn.request_size import estimate_messages_tokens
from karyvia.sdk.testing import (
    ContextProviderContract,
    FakePluginContext,
    ManualCancel,
    make_correlation,
)

#: 固定时钟。运行时事实片段要能逐字符断言，就不能读真实时间。
FIXED_NOW: Final = datetime(2026, 8, 12, 9, 30, tzinfo=UTC)

KEY: Final = SessionKey(channel_id="cli", conversation_id="local")


def make_settings(
    *,
    instructions: str = "",
    use_baseline: bool = True,
    include_runtime_facts: bool = True,
) -> BasicContextSettings:
    return BasicContextSettings(
        instructions=instructions,
        use_baseline=use_baseline,
        include_runtime_facts=include_runtime_facts,
    )


def make_provider(**kwargs: object) -> BasicContextProvider:
    return BasicContextProvider(
        make_settings(**kwargs),  # type: ignore[arg-type]
        clock=lambda: FIXED_NOW,
    )


def snapshot_with(*contents: str, compacted_through: int = 0) -> SessionSnapshot:
    messages = tuple(
        SessionMessage(
            message_id=f"m{index}",
            role=Role.USER if index % 2 == 0 else Role.ASSISTANT,
            content=content,
            created_at=FIXED_NOW,
        )
        for index, content in enumerate(contents)
    )
    return SessionSnapshot(
        session_key=KEY, messages=messages, compacted_through=compacted_through
    )


async def provide(provider: BasicContextProvider, snapshot: SessionSnapshot):
    return await provider.provide(snapshot, make_correlation(), ManualCancel())


async def assemble_with(
    provider: BasicContextProvider,
    snapshot: SessionSnapshot,
    *,
    user_input: str = "你好",
):
    """走真的组装器，拿到最终会发给模型的消息序列。"""
    from karyvia.kernel.turn.context_builder import ContextProviderBinding

    return await assemble(
        snapshot=snapshot,
        user_input=user_input,
        correlation=make_correlation(),
        cancel=ManualCancel(),
        bindings=[ContextProviderBinding(provider=provider, owner=Builtin(), name="basic")],
        now=FIXED_NOW,
    )


# --------------------------------------------------------------------------- 契约


class TestBasicContextProvider(ContextProviderContract):
    """`ContextProviderContract` 的全部通用用例。"""

    def make_provider(self) -> ContextProvider:
        return make_provider()


  # -------------------------------------------------------------------  /



class TestUsableWithoutPlugins:
    """没有 Memory、没有检索插件、没有任何配置时，仍必须产出可用上下文。"""

    async def test_an_empty_session_still_yields_system_instructions(self) -> None:
        fragments = await provide(make_provider(), SessionSnapshot(session_key=KEY))
        assert fragments, "空会话必须仍有系统指令，否则  不成立"
        assert fragments[0].content == BASELINE_INSTRUCTIONS
        assert fragments[0].trust is TrustLevel.SYSTEM

    async def test_assembly_completes_with_no_other_providers(self) -> None:
        """未安装 Memory 插件不得产生缺失依赖错误。"""
        assembled = await assemble_with(make_provider(), SessionSnapshot(session_key=KEY))
        assert assembled.messages[0].role is Role.SYSTEM
        assert BASELINE_INSTRUCTIONS in assembled.messages[0].content
        assert assembled.messages[-1].content == "你好"
        assert assembled.dropped == ()

    async def test_the_provider_never_raises_on_a_valid_configuration(self) -> None:
        """配置合法时 `provide()` 没有可失败的外部依赖。"""
        for snapshot in (
            SessionSnapshot(session_key=KEY),
            snapshot_with("hi", "hello"),
            snapshot_with("hi", "hello", "again", compacted_through=2),
        ):
            assert await provide(make_provider(), snapshot)

    async def test_runtime_facts_report_how_much_history_is_visible(self) -> None:
        """模型据此才知道自己看到的是全部历史还是一截。"""
        fragments = await provide(
            make_provider(), snapshot_with("a", "b", "c", compacted_through=2)
        )
        facts = next(item for item in fragments if item.kind is FragmentKind.RUNTIME)
        assert facts.content == (
            f"当前时间：{FIXED_NOW.isoformat()}\n"
            "会话：cli / local（scope=default）\n"
            "可见历史消息：1 条\n"
            "已被摘要覆盖、原文不可见的更早消息：2 条"
        )
        assert facts.scope is FragmentScope.SESSION

    async def test_runtime_facts_omit_the_compaction_line_when_nothing_is_compacted(
        self,
    ) -> None:
        fragments = await provide(make_provider(), snapshot_with("a"))
        facts = next(item for item in fragments if item.kind is FragmentKind.RUNTIME)
        assert "已被摘要覆盖" not in facts.content

    async def test_runtime_facts_can_be_switched_off(self) -> None:
        fragments = await provide(make_provider(include_runtime_facts=False), snapshot_with("a"))
        assert [item.kind for item in fragments] == [FragmentKind.SYSTEM]


  # ---------------------------------------------------------------------------



class TestTrustPlacement:
    """`trust` 决定位置，`kind` 不参与判定（组装器的规则 2）。"""

    async def test_operator_instructions_are_not_system_trusted(self) -> None:
        fragments = await provide(make_provider(instructions="你只说中文。"), snapshot_with("a"))
        operator = next(item for item in fragments if item.trust is TrustLevel.OPERATOR)
        # 种类说的是「它是一段指令」，位置由 trust 决定——两者刻意不同。
        assert operator.kind is FragmentKind.SYSTEM
        assert operator.may_act_as_instruction is False

    async def test_operator_instructions_stay_out_of_the_system_message(self) -> None:
        """当前实现检查：配置文本不得取得系统指令级别的优先级。"""
        assembled = await assemble_with(
            make_provider(instructions="你只说中文。"), snapshot_with("a")
        )
        system = assembled.messages[0]
        assert system.role is Role.SYSTEM
        assert "你只说中文。" not in system.content
        assert any(
            message.role is Role.USER and "你只说中文。" in message.content
            for message in assembled.messages[1:]
        )

    async def test_every_fragment_declares_the_builtin_source(self) -> None:
        """诊断里「这段是谁塞进来的」必须查得到。"""
        fragments = await provide(make_provider(instructions="x"), snapshot_with("a"))
        assert {item.source for item in fragments} == {FRAGMENT_SOURCE}
        assert len(fragments) == 3

    async def test_nothing_this_provider_emits_gets_wrapped_as_untrusted(self) -> None:
        """本内建不产出 `UNTRUSTED` 片段：它不引入任何外部内容。

        断言的是**包裹**而不是那句前缀——基线指令自己就引用了 `UNTRUSTED_DATA_PREFIX`
        （模型得认得这个暗号），拿前缀当判据会把那段刻意的引用误判成越界。
        """
        fragments = await provide(make_provider(instructions="x"), snapshot_with("a"))
        assert all(item.trust is not TrustLevel.UNTRUSTED for item in fragments)
        assert all("<untrusted-data" not in item.as_model_text() for item in fragments)
        assert all(item.as_model_text() == item.content for item in fragments)

    def test_the_baseline_teaches_the_model_the_untrusted_marker(self) -> None:
        """包裹只有在模型认得那句前缀时才有意义。"""
        assert UNTRUSTED_DATA_PREFIX in BASELINE_INSTRUCTIONS

    async def test_the_provider_does_not_replay_history_itself(self) -> None:
        """历史由组装器重放；再贡献一份就是把同一段对话讲两遍。"""
        fragments = await provide(make_provider(), snapshot_with("独一无二的历史内容"))
        assert all("独一无二的历史内容" not in item.content for item in fragments)
        assert all(item.kind is not FragmentKind.HISTORY for item in fragments)


  # ---------------------------------------------------------------------------



class TestTokenEstimate:
    """片段提示值与 Kernel 最终结构计量是两项不同职责。"""

    @pytest.mark.parametrize(
        "text", ["", "a", "ab", "abc", "abcd", "你好", BASELINE_INSTRUCTIONS, "x" * 5000]
    )
    def test_builtin_hint_uses_the_same_text_baseline(self, text: str) -> None:
        assert estimate_tokens(text) == kernel_estimate_tokens(text)

    async def test_each_fragment_reports_its_own_size(self) -> None:
        fragments = await provide(make_provider(instructions="你只说中文。"), snapshot_with("a"))
        for fragment in fragments:
            assert fragment.estimated_tokens == estimate_tokens(fragment.content)

    async def test_assembly_recounts_the_final_message_structure(self) -> None:
        snapshot = snapshot_with("历史一", "历史二")
        assembled = await assemble_with(make_provider(instructions="你只说中文。"), snapshot)
        assert assembled.estimated_tokens == estimate_messages_tokens(assembled.messages)

    async def test_builder_keeps_operator_instructions_and_history_intact(self) -> None:
        snapshot = snapshot_with("历史一", "历史二")
        assembled = await assemble_with(
            make_provider(instructions="你只说中文。", include_runtime_facts=False),
            snapshot,
        )
        assert assembled.dropped == ()
        assert any(item.trust is TrustLevel.OPERATOR for item in assembled.fragments)
        assert BASELINE_INSTRUCTIONS in assembled.messages[0].content
        assert any("历史一" in message.content for message in assembled.messages)

    async def test_builder_never_trims_system_instructions(self) -> None:
        assembled = await assemble_with(make_provider(), snapshot_with("历史"))
        assert BASELINE_INSTRUCTIONS in assembled.messages[0].content


# --------------------------------------------------------------------------- 配置


class TestSettings:
    """`resolve_settings()` 在 `setup` 时校验一次；一份写错的配置不该拖到第一次 turn 才炸。"""

    def test_defaults_need_no_configuration(self) -> None:
        settings = resolve_settings(FakePluginContext())
        assert settings.use_baseline is True
        assert settings.include_runtime_facts is True
        assert settings.instructions == ""

    def test_instructions_accept_a_plain_string(self) -> None:
        ctx = FakePluginContext(config={CONFIG_INSTRUCTIONS_KEY: "  你只说中文。  \n\n"})
        assert resolve_settings(ctx).instructions == "  你只说中文。"

    def test_instructions_accept_a_string_array(self) -> None:
        """JSON 里写多行提示词只有这两种写法，两种都得认。"""
        ctx = FakePluginContext(
            config={CONFIG_INSTRUCTIONS_KEY: ["", "第一行  ", "", "第三行", ""]}
        )
        assert resolve_settings(ctx).instructions == "第一行\n\n第三行"

    @pytest.mark.parametrize("configured", [123, {"a": 1}, ["ok", 5], True])
    def test_a_bad_instructions_type_is_a_config_error(self, configured: object) -> None:
        ctx = FakePluginContext(config={CONFIG_INSTRUCTIONS_KEY: configured})  # type: ignore[dict-item]
        with pytest.raises(KaryviaError) as caught:
            resolve_settings(ctx)
        assert caught.value.code is ErrorCode.CONFIG_INVALID

    @pytest.mark.parametrize("key", [CONFIG_USE_BASELINE_KEY, CONFIG_RUNTIME_FACTS_KEY])
    @pytest.mark.parametrize("configured", ["true", 1, 0, []])
    def test_a_bad_switch_type_is_a_config_error(self, key: str, configured: object) -> None:
        """`1` 不是 `True`：静默接受它，用户就永远不知道自己那行配置写错了。"""
        ctx = FakePluginContext(config={key: configured})  # type: ignore[dict-item]
        with pytest.raises(KaryviaError) as caught:
            resolve_settings(ctx)
        assert caught.value.code is ErrorCode.CONFIG_INVALID

    def test_disabling_the_baseline_without_instructions_is_rejected(self) -> None:
        """那等于要一个没有任何系统指令的 Agent；正规做法是禁用本内建。"""
        ctx = FakePluginContext(config={CONFIG_USE_BASELINE_KEY: False})
        with pytest.raises(KaryviaError) as caught:
            resolve_settings(ctx)
        assert caught.value.code is ErrorCode.CONFIG_INVALID

    async def test_disabling_the_baseline_with_instructions_is_allowed(self) -> None:
        ctx = FakePluginContext(
            config={CONFIG_USE_BASELINE_KEY: False, CONFIG_INSTRUCTIONS_KEY: "只说中文。"}
        )
        provider = BasicContextProvider(resolve_settings(ctx), clock=lambda: FIXED_NOW)
        fragments = await provider.provide(
            snapshot_with("a"), make_correlation(), ManualCancel()
        )
        assert all(BASELINE_INSTRUCTIONS not in item.content for item in fragments)

    def test_the_config_schema_lists_exactly_the_keys_the_code_reads(self) -> None:
        """manifest 的 `config_schema` 与实现读的键是同一组，不多不少。"""
        properties = CONTEXT_BASIC.config_schema["properties"]
        assert isinstance(properties, dict)
        assert set(properties) == {
            CONFIG_INSTRUCTIONS_KEY,
            CONFIG_USE_BASELINE_KEY,
            CONFIG_RUNTIME_FACTS_KEY,
        }
        assert CONTEXT_BASIC.config_schema["additionalProperties"] is False


# --------------------------------------------------------------------------- 注册


class TestRegistration:
    """内建的落地形态：一份普通 manifest + 一个 `setup(api)`，没有第二条路。"""

    def test_the_manifest_is_listed_as_a_builtin(self) -> None:
        assert CONTEXT_BASIC in BUILTIN_MANIFESTS
        assert CONTEXT_BASIC.id == "context-basic"
        declaration = CONTEXT_BASIC.capabilities[0]
        assert declaration.kind is CapabilityKind.CONTEXT
        assert declaration.name == CAPABILITY_NAME
        assert declaration.overrides is None
        # `priority` 不写：内建基准是 0，写了（哪怕写的是默认值 100）就会被原样采纳。
        assert "priority" not in declaration.model_fields_set

    def test_a_bad_configuration_fails_at_setup_rather_than_at_the_first_turn(self) -> None:
        class RecordingApi:
            ctx = FakePluginContext(config={CONFIG_USE_BASELINE_KEY: "no"})  # type: ignore[dict-item]

            def register_context_provider(self, name: str, provider: object) -> None:
                raise AssertionError("配置非法时不该注册任何东西")

        with pytest.raises(KaryviaError) as caught:
            setup(RecordingApi())  # type: ignore[arg-type]
        assert caught.value.code is ErrorCode.CONFIG_INVALID


def test_json_value_typing_is_satisfied_by_the_documented_config() -> None:
    """文档化的三个键都是 `JsonValue`——配置块会原样穿过 JSON。"""
    config: dict[str, JsonValue] = {
        CONFIG_INSTRUCTIONS_KEY: ["一", "二"],
        CONFIG_USE_BASELINE_KEY: True,
        CONFIG_RUNTIME_FACTS_KEY: False,
    }
    settings = resolve_settings(FakePluginContext(config=config))
    assert settings.instructions == "一\n二"
    assert settings.include_runtime_facts is False
