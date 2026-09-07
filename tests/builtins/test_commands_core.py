"""内建命令集 `commands_core`当前行为。

| 验收项 | 测试 |
| --- | --- |
| 6 个命令各有测试 | `TestHelp` … `TestCancel` |
| `/capabilities` 无插件时列出全部内建能力及提供方（§16.1 第 2 条） | `TestCapabilities` |
| `/config` 输出哨兵扫描无泄漏 | `TestConfigRedaction` |
| 命令失败返回可诊断错误且会话可用 | `TestFailureIsDiagnostic` |
| 声明的名称/参数/说明/操作员限制可被 registry 统一列出 | `TestRegistration` |
| 单命令禁用后 `/help` 与 registry 同步消失 | `TestSingleCommandDisable` |

三条写这些用例时的取舍：

- **全部经真实 dispatcher 调用，而不是直接 `await handler.handle(...)`**。`operator_only`
  与参数个数由 dispatcher 前置校验，命令自己不抄——那意味着「`/config` 只有管理员
  能敲」这条只有走真实分流路径才验得到。直接调 handler 会让这些用例在校验被误删时照样通过。
- **装配走真实 `wire_capabilities` + `build_command_index`**，与
  `test_tools_fs.py::TestSingleToolDisable` 同一套做法： 新加的 `ctx.instance` /
  `ctx.turns` 是否真的到得了 handler，只有整条链跑一遍才算数。
- **哨兵是真的**（`sk-` + ≥16 字符，匹配 `errors.py::_SECRET_VALUE_PATTERNS`）：
  与  同一条做法。断言它不出现在输出、`repr` 与序列化里。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

import pytest

from karyvia.builtins.commands_core import (
    COMMAND_NAMES,
    CONFIG_DISABLE_KEY,
    CONFIG_MAX_OUTPUT_CHARS_KEY,
    DEFAULT_MAX_OUTPUT_CHARS,
    SPECS,
    enabled_command_names,
    render_help,
    resolve_settings,
    setup,
    truncate,
)
from karyvia.builtins.registry import BUILTIN_MANIFESTS, COMMANDS_CORE
from karyvia.contracts import (
    Correlation,
    Disposition,
    ErrorCode,
    InboundMessage,
    InstanceId,
    KaryviaError,
    Role,
    Sender,
    SessionKey,
    SessionMessage,
    SessionSnapshot,
    TurnId,
)
from karyvia.kernel.routing import Dispatcher, build_command_index
from karyvia.runtime.wiring import wire_capabilities
from karyvia.sdk.testing import (
    FakeInstanceView,
    FakePluginContext,
    FakeTurnControl,
    ManualCancel,
)

#: 哨兵：形状匹配 `errors.py` 的已知令牌，因此脱敏一旦失灵就会被这几条用例抓到。
SECRET: Final = "sk-d22commandscoresentinel0001"

INSTANCE: Final = InstanceId("default")
TURN: Final = TurnId("turn-under-test")


# --------------------------------------------------------------------------- 夹具


def make_message(content: str, *, operator: bool = True) -> InboundMessage:
    return InboundMessage(
        message_id="m-1",
        instance_id=INSTANCE,
        channel_id="cli",
        conversation_id="local",
        sender=Sender(user_id="u-1", is_operator=operator),
        content=content,
        timestamp=datetime(2026, 8, 13, tzinfo=UTC),
    )


def make_snapshot() -> SessionSnapshot:
    key = SessionKey("cli", "local")
    messages = tuple(
        SessionMessage(
            message_id=f"sm-{i}",
            role=Role.USER,
            content=f"m{i}",
            created_at=datetime(2026, 8, 13, tzinfo=UTC),
        )
        for i in range(4)
    )
    return SessionSnapshot(session_key=key, messages=messages, compacted_through=2)


def make_view(**overrides: object) -> FakeInstanceView:
    """一个内容齐全的只读视图。各用例按需覆盖单项。"""
    defaults: dict[str, object] = {
        "capabilities": {
            "active": [
                {"kind": "command", "name": "help", "provider": "builtin"},
                {"kind": "tool", "name": "fs.read", "provider": "builtin"},
                {"kind": "model", "name": "openai", "provider": "plugin:acme"},
            ],
            "shadowed": [],
            "disabled": [],
            "failures": [],
        },
        "config_document": {"model": {"model_id": "gpt-4o", "api_key": "${OPENAI_API_KEY}"}},
        "snapshots": {make_snapshot().session_key.storage_id(): make_snapshot()},
    }
    defaults.update(overrides)
    return FakeInstanceView(**defaults)  # type: ignore[arg-type]


async def wire(
    config: dict[str, object] | None = None,
    *,
    view: FakeInstanceView | None = None,
    turns: FakeTurnControl | None = None,
) -> tuple[Dispatcher, FakePluginContext]:
    """走真实装配链：manifest → wire_capabilities(keep=…) → build_command_index → Dispatcher。"""
    block = config or {}
    ctx = FakePluginContext(
        "commands-core",
        config=block,  # type: ignore[arg-type]
        instance=view if view is not None else make_view(),
        turns=turns if turns is not None else FakeTurnControl(),
    )
    wiring = await wire_capabilities(
        manifests=(COMMANDS_CORE,),
        context_for=lambda _provider: ctx,
        keep=lambda _manifest, decl: decl.name in enabled_command_names(block),  # type: ignore[arg-type]
    )
    assert wiring.report.ok, wiring.report.failures
    index = build_command_index(wiring.registry)
    # 命令索引要等全部注册完才建得出来，而 ctx 必须在 `setup()` 之前就交出去——生产实现
    # 因此持有一个 callable（`KernelInstanceView.commands_source`）。这里事后填是同一件事。
    ctx.instance.set_commands(index.specs())
    return Dispatcher(index), ctx


async def run(dispatcher: Dispatcher, text: str, *, operator: bool = True) -> object:
    message = make_message(text, operator=operator)
    correlation = Correlation(
        instance_id=INSTANCE, session_key=message.session_key(), turn_id=TURN
    )
    return await dispatcher.dispatch(message, correlation, ManualCancel())


# --------------------------------------------------------------------------- /help


class TestHelp:
    async def test_lists_every_registered_command(self) -> None:
        dispatcher, _ = await wire()
        outcome = await run(dispatcher, "/help")
        assert outcome.disposition is Disposition.COMMAND_HANDLED  # type: ignore[attr-defined]
        content = outcome.result.content  # type: ignore[attr-defined]
        for name in COMMAND_NAMES:
            assert f"/{name}" in content

    async def test_shows_the_four_declared_facets(self) -> None:
        """ 点名的四样：名称、参数形式、说明、权限需求。

        只印名字和说明，用户就只能靠敲一次来发现自己没权限。
        """
        text = render_help([SPECS["cancel"], SPECS["config"]], "/")
        assert "/cancel [turn-id]" in text          # 名称 + 参数形式
        assert SPECS["cancel"].description in text  # 说明
        assert "[管理员]" in text                    # 权限需求（operator_only）

    async def test_lists_aliases(self) -> None:
        text = render_help([SPECS["help"]], "/")
        assert "/h" in text

    async def test_empty_index_says_so_instead_of_printing_a_blank(self) -> None:
        assert render_help([], "/") == "当前没有可用的命令。"

# --------------------------------------------------------------------------- /config


class TestConfigRedaction:
    async def test_renders_the_full_document(self) -> None:
        dispatcher, _ = await wire()
        outcome = await run(dispatcher, "/config")
        assert outcome.disposition is Disposition.COMMAND_HANDLED  # type: ignore[attr-defined]
        assert "gpt-4o" in outcome.result.content  # type: ignore[attr-defined]

    async def test_secret_sentinel_never_reaches_the_output(self) -> None:
        """哨兵扫描。

         的结构性保证是配置树里只有 `${VAR}` 字面量，因此正常路径上根本没有明文。
        这条用例把明文**硬塞进**文档，验证 `redact()` + `scrub()` 那道纵深防御真的在。
        """
        view = make_view(config_document={"model": {"api_key": SECRET, "note": f"用 {SECRET}"}})
        dispatcher, _ = await wire(view=view)
        outcome = await run(dispatcher, "/config")
        content = outcome.result.content  # type: ignore[attr-defined]
        assert SECRET not in content
        assert SECRET not in repr(outcome)

    async def test_is_operator_only(self) -> None:
        """`/config` 是「实例怎么装的」，不该由随便谁在群聊里敲出来。

        判定由 dispatcher 前置做，命令自己不抄——因此这条必须走真实分流路径。
        """
        dispatcher, _ = await wire()
        outcome = await run(dispatcher, "/config", operator=False)
        assert outcome.disposition is Disposition.REJECTED  # type: ignore[attr-defined]
        assert outcome.result.error.code is ErrorCode.PERMISSION_DENIED  # type: ignore[attr-defined]

    async def test_empty_config_says_so(self) -> None:
        dispatcher, _ = await wire(view=make_view(config_document={}))
        outcome = await run(dispatcher, "/config")
        assert "没有生效的配置项" in outcome.result.content  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- /session


class TestSession:
    async def test_reports_counts_and_compaction_watermark(self) -> None:
        dispatcher, _ = await wire()
        outcome = await run(dispatcher, "/session")
        content = outcome.result.content  # type: ignore[attr-defined]
        assert "记录数：4" in content
        assert "未压缩 2" in content
        assert "压缩水位 2" in content

    async def test_unknown_session_yields_an_empty_snapshot_not_an_error(self) -> None:
        """「没有历史」是正常状态，不是失败（与 `SessionStore.load()` 一致）。"""
        dispatcher, _ = await wire(view=make_view(snapshots={}))
        outcome = await run(dispatcher, "/session")
        assert outcome.disposition is Disposition.COMMAND_HANDLED  # type: ignore[attr-defined]
        assert "记录数：0" in outcome.result.content  # type: ignore[attr-defined]

    async def test_uses_the_session_key_of_this_message(self) -> None:
        """实例级的「当前会话」在多 session 并发下没有定义。"""
        dispatcher, _ = await wire()
        outcome = await run(dispatcher, "/session")
        assert make_snapshot().session_key.storage_id() in outcome.result.content  # type: ignore[attr-defined]

    async def test_shows_timestamps_when_present(self) -> None:
        stamped = SessionSnapshot(
            session_key=SessionKey("cli", "local"),
            created_at=datetime(2026, 8, 1, tzinfo=UTC),
            updated_at=datetime(2026, 8, 13, tzinfo=UTC),
        )
        view = make_view(snapshots={stamped.session_key.storage_id(): stamped})
        dispatcher, _ = await wire(view=view)
        content = (await run(dispatcher, "/session")).result.content  # type: ignore[attr-defined]
        assert "创建于：2026-08-01" in content
        assert "更新于：2026-08-13" in content


# --------------------------------------------------------------------------- /plugins


class TestPlugins:
    async def test_no_plugins_is_a_statement_not_a_blank(self) -> None:
        """零插件是可用形态，用户该看到一句确认。"""
        dispatcher, _ = await wire()
        outcome = await run(dispatcher, "/plugins")
        assert "没有加载任何外部插件" in outcome.result.content  # type: ignore[attr-defined]

    async def test_lists_state_version_and_capabilities(self) -> None:
        view = make_view(
            plugins=[{
                "plugin_id": "acme",
                "version": "1.2.3",
                "state": "activated",
                "capabilities": ["model:openai"],
            }]
        )
        dispatcher, _ = await wire(view=view)
        content = (await run(dispatcher, "/plugins")).result.content  # type: ignore[attr-defined]
        assert "acme" in content and "1.2.3" in content
        assert "activated" in content and "model:openai" in content

    async def test_reports_the_failure_and_its_phase(self) -> None:
        """一个加载失败的插件必须说得出「哪一步失败的」。"""
        view = make_view(
            plugins=[{
                "plugin_id": "broken",
                "version": "0.1.0",
                "state": "failed",
                "capabilities": [],
                "failure": {"code": "plugin.load_failed", "message": "setup 抛了异常"},
                "failed_phase": "setup",
            }]
        )
        dispatcher, _ = await wire(view=view)
        content = (await run(dispatcher, "/plugins")).result.content  # type: ignore[attr-defined]
        assert "broken" in content
        assert "失败（setup）" in content
        assert "plugin.load_failed" in content


# --------------------------------------------------------------------------- /capabilities


class TestCapabilities:
    async def test_lists_every_capability_with_its_provider(self) -> None:
        """§16.1 第 2 条：无插件时列出全部内建能力及提供方。"""
        dispatcher, _ = await wire()
        content = (await run(dispatcher, "/capabilities")).result.content  # type: ignore[attr-defined]
        assert "command:help" in content
        assert "tool:fs.read" in content
        assert "builtin" in content
        assert "plugin:acme" in content

    async def test_prints_all_four_sections_even_when_empty(self) -> None:
        """`failures` 为空是一条有价值的结论；只在非空时才提会让人不确定查没查。"""
        dispatcher, _ = await wire()
        content = (await run(dispatcher, "/capabilities")).result.content  # type: ignore[attr-defined]
        for label in ("被覆盖：", "已禁用：", "冲突："):
            assert label in content

    async def test_alias_caps_works(self) -> None:
        dispatcher, _ = await wire()
        outcome = await run(dispatcher, "/caps")
        assert outcome.disposition is Disposition.COMMAND_HANDLED  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- /cancel


class TestCancel:
    async def test_without_arguments_lists_instead_of_cancelling_everything(self) -> None:
        """一次误敲把所有并发 turn 全掐掉是不可撤销的；列出来再敲一次只多一次往返。"""
        turns = FakeTurnControl([TurnId("t-1"), TurnId("t-2")])
        dispatcher, _ = await wire(turns=turns)
        content = (await run(dispatcher, "/cancel")).result.content  # type: ignore[attr-defined]
        assert "t-1" in content and "t-2" in content
        assert turns.requested == []

    async def test_cancels_the_named_turn(self) -> None:
        turns = FakeTurnControl([TurnId("t-1")])
        dispatcher, _ = await wire(turns=turns)
        outcome = await run(dispatcher, "/cancel t-1")
        assert outcome.disposition is Disposition.COMMAND_HANDLED  # type: ignore[attr-defined]
        assert [t for t, _ in turns.requested] == [TurnId("t-1")]

    async def test_refuses_to_cancel_its_own_turn(self) -> None:
        """`/cancel` 正持有 session 槽位在跑，取消自己既没意义也会让输出发不出去。"""
        turns = FakeTurnControl([TURN])
        dispatcher, _ = await wire(turns=turns)
        outcome = await run(dispatcher, f"/cancel {TURN}")
        assert outcome.disposition is Disposition.REJECTED  # type: ignore[attr-defined]
        assert turns.requested == []

    async def test_unknown_turn_is_a_diagnosable_rejection(self) -> None:
        """「已经结束了」对用户是好结果，对诊断不是——因此仍然报得出来。"""
        dispatcher, _ = await wire(turns=FakeTurnControl())
        outcome = await run(dispatcher, "/cancel t-gone")
        assert outcome.disposition is Disposition.REJECTED  # type: ignore[attr-defined]
        assert outcome.result.error.code is ErrorCode.CAPABILITY_MISSING  # type: ignore[attr-defined]

    async def test_empty_live_list_says_so(self) -> None:
        dispatcher, _ = await wire(turns=FakeTurnControl())
        content = (await run(dispatcher, "/cancel")).result.content  # type: ignore[attr-defined]
        assert "没有正在执行的 turn" in content


# --------------------------------------------------------- 失败可诊断 / 会话可用


class TestFailureIsDiagnostic:
    async def test_handler_failure_does_not_break_the_session(self) -> None:
        """命令失败后，同一个 dispatcher 紧接着还能正常分流。"""
        dispatcher, _ = await wire()
        bad = await run(dispatcher, "/cancel nope")
        assert bad.disposition is Disposition.REJECTED  # type: ignore[attr-defined]
        good = await run(dispatcher, "/help")
        assert good.disposition is Disposition.COMMAND_HANDLED  # type: ignore[attr-defined]
        plain = await run(dispatcher, "就是一句普通的话")
        assert plain.disposition is Disposition.MODEL_TURN  # type: ignore[attr-defined]

    async def test_unexpected_exception_is_folded_without_leaking_its_message(self) -> None:
        """折出来的错误**只放类型名不放异常消息**——自由文本可能带着凭据。"""
        class Exploding(FakeInstanceView):
            def commands(self) -> tuple[object, ...]:  # type: ignore[override]
                raise RuntimeError(f"泄漏了 {SECRET}")

        dispatcher, _ = await wire(view=Exploding())
        outcome = await run(dispatcher, "/help")
        assert outcome.disposition is Disposition.REJECTED  # type: ignore[attr-defined]
        error = outcome.result.error  # type: ignore[attr-defined]
        assert error.code is ErrorCode.KERNEL_INVARIANT_VIOLATED
        assert SECRET not in repr(error)
        assert error.detail["error_type"] == "RuntimeError"

    async def test_karyvia_error_passes_through_unchanged(self) -> None:
        """实现方给的诊断比 Kernel 能编的更准，原样带出。"""
        class Failing(FakeInstanceView):
            def plugins(self) -> tuple[object, ...]:  # type: ignore[override]
                raise KaryviaError(ErrorCode.PERSISTENCE_READ_FAILED, "插件状态读不出来。")

        dispatcher, _ = await wire(view=Failing())
        outcome = await run(dispatcher, "/plugins")
        assert outcome.result.error.code is ErrorCode.PERSISTENCE_READ_FAILED  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- 注册


class TestRegistration:
    def test_manifest_declares_exactly_the_six_commands(self) -> None:
        declared = {decl.name for decl in COMMANDS_CORE.capabilities}
        assert declared == set(COMMAND_NAMES) == set(SPECS)

    def test_manifest_leaves_priority_unset(self) -> None:
        """写了就会被原样采纳（默认 100），而内建基准是 0。"""
        for decl in COMMANDS_CORE.capabilities:
            assert "priority" not in decl.model_fields_set

    def test_is_part_of_the_builtin_manifest_list(self) -> None:
        assert COMMANDS_CORE in BUILTIN_MANIFESTS

    async def test_registers_through_the_ordinary_builtin_path(self) -> None:
        """普通 manifest + `setup(api)`，没有内建专用注册通道。"""
        dispatcher, _ = await wire()
        for name in COMMAND_NAMES:
            outcome = await run(dispatcher, f"/{name}")
            assert outcome.disposition is not Disposition.MODEL_TURN  # type: ignore[attr-defined]

    def test_setup_is_the_documented_entry_point(self) -> None:
        assert COMMANDS_CORE.setup == "karyvia.builtins.commands_core:setup"
        assert callable(setup)


# --------------------------------------------------------------------- 单命令禁用


class TestSingleCommandDisable:
    async def test_disabled_command_disappears_from_registry_and_help(self) -> None:
        """可见列表与可执行集合同源。"""
        dispatcher, _ = await wire({CONFIG_DISABLE_KEY: ["config"]})
        assert (await run(dispatcher, "/config")).disposition is Disposition.REJECTED  # type: ignore[attr-defined]
        content = (await run(dispatcher, "/help")).result.content  # type: ignore[attr-defined]
        assert "/config" not in content
        assert "/help" in content

    def test_enabled_names_drops_only_the_disabled_one(self) -> None:
        assert enabled_command_names({CONFIG_DISABLE_KEY: ["cancel"]}) == set(COMMAND_NAMES) - {
            "cancel"
        }

    def test_unknown_name_in_disable_is_rejected(self) -> None:
        """静默忽略拼错的名字，用户会以为自己关掉了 `/config` 而它其实还在。"""
        with pytest.raises(KaryviaError) as caught:
            enabled_command_names({CONFIG_DISABLE_KEY: ["cnofig"]})
        assert caught.value.code is ErrorCode.CONFIG_INVALID

    def test_disable_must_be_a_list(self) -> None:
        with pytest.raises(KaryviaError):
            enabled_command_names({CONFIG_DISABLE_KEY: "config"})

    async def test_forgetting_keep_makes_the_load_fail_loudly(self) -> None:
        """不传 `keep` 而用户禁用了命令时，`CapabilityHost.finish()` 必须挡下——那个报错是对的。

        与 `test_tools_fs.py` / `test_tools_shell.py` 的同名用例一致。
        """
        block = {CONFIG_DISABLE_KEY: ["config"]}
        ctx = FakePluginContext("commands-core", config=block, instance=make_view())  # type: ignore[arg-type]
        wiring = await wire_capabilities(
            manifests=(COMMANDS_CORE,), context_for=lambda _p: ctx
        )
        outcome = wiring.outcomes[0]
        assert outcome.error is not None
        assert outcome.error.code is ErrorCode.PLUGIN_LOAD_FAILED


# --------------------------------------------------------------------------- 配置


class TestSettings:
    def test_defaults(self) -> None:
        settings = resolve_settings({})
        assert settings.enabled == set(COMMAND_NAMES)
        assert settings.max_output_chars == DEFAULT_MAX_OUTPUT_CHARS

    @pytest.mark.parametrize("bad", [0, -1, True, "16384", 1.5])
    def test_max_output_chars_rejects_bad_values(self, bad: object) -> None:
        """`bool` 是 `int` 的子类，`True` 会被当成 1——那不是用户的意思。"""
        with pytest.raises(KaryviaError) as caught:
            resolve_settings({CONFIG_MAX_OUTPUT_CHARS_KEY: bad})
        assert caught.value.code is ErrorCode.CONFIG_INVALID

    def test_config_is_validated_at_setup_not_at_first_use(self) -> None:
        """一份写错的配置应当在启动时被指出来。"""
        api = _RecordingApi(FakePluginContext("commands-core", config={CONFIG_DISABLE_KEY: ["x"]}))  # type: ignore[arg-type]
        with pytest.raises(KaryviaError):
            setup(api)
        assert api.registered == []


class _RecordingApi:
    """最小的 `KaryviaAPI` 替身：只记下注册了什么。"""

    def __init__(self, ctx: FakePluginContext) -> None:
        self._ctx = ctx
        self.registered: list[str] = []

    @property
    def ctx(self) -> FakePluginContext:
        return self._ctx

    def register_command(self, spec: object, handler: object) -> None:
        del handler
        self.registered.append(spec.name)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- 截断


class TestTruncation:
    def test_short_text_is_untouched(self) -> None:
        assert truncate("hi", 100) == "hi"

    def test_result_never_exceeds_the_limit(self) -> None:
        """截断标记**算在上限内**（做法）：先按最坏情况算能留多少，再渲染标记。"""
        for limit in (60, 120, 500):
            assert len(truncate("x" * 5000, limit)) <= limit

    def test_marker_reports_the_real_lengths(self) -> None:
        out = truncate("x" * 5000, 200)
        assert "5000" in out
        assert out.count("x") > 0
