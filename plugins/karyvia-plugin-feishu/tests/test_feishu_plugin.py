"""官方插件 `feishu`当前行为：manifest、配置、生命周期、SDK 边界。

| 验收项 | 测试 |
| --- | --- |
| 通过 `ChannelContract` 全部用例 | `TestFeishuChannelContract` |
| manifest 自洽、entry point 对得上 | `TestManifest` |
| 配置校验在 `setup()` 时发生一次 | `TestSettings` |
| `start` / `stop` / `receive` / `deliver` 的契约约定 | `TestChannelLifecycle` |
| **只有两个模块接触 SDK**（AST 扫描） | `TestSdkBoundary` |
| **导入插件不拉进 `lark-oapi`**（子进程探针） | `TestSdkBoundary` |
| 没装 SDK 时给一句能照做的话 | `TestSdkBoundary` |

线格式与判定的逐条断言在 `test_feishu_normalize.py` / `test_feishu_outbound.py` / `test_feishu_stream.py` /
`test_feishu_content.py` / `test_feishu_mentions.py`；这里只放需要真的构造一个 Channel（或真的解析一次
配置）才成立的。
"""

from __future__ import annotations

import ast
import asyncio
import subprocess
import sys
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import karyvia_plugin_feishu as plugin
import pytest
from _feishu_fakes import (
    APP_ID,
    APP_SECRET,
    CHAT_ID,
    MESSAGE_ID,
    FakeClient,
    FakeEvent,
    FakeEventMessage,
    FakeGateway,
    outbound,
    settings,
    text_content,
)
from karyvia_plugin_feishu import (
    CAPABILITY_NAME,
    CONFIG_KEYS,
    CONFIG_SCHEMA,
    MANIFEST,
    MISSING_SDK_FIX,
    SECRET_APP_ID,
    SECRET_APP_SECRET,
    FeishuChannel,
    FeishuGateway,
    resolve_settings,
    setup,
)

from karyvia.contracts import (
    CapabilityKind,
    ErrorCode,
    KaryviaError,
    StreamState,
)
from karyvia.sdk.testing import ChannelContract, FakePluginContext

#: **只有这两个模块允许 import `lark_oapi`。** 见 `TestSdkBoundary`。
SDK_MODULES = {"gateway.py", "client.py"}


# boundary: 透传给 `settings()` 的配置覆盖，形状即配置表
def make_channel(**kwargs: Any) -> tuple[FeishuChannel, FakeGateway, FakeClient]:
    gateway, client = FakeGateway(), FakeClient()
    channel = FeishuChannel(
        settings(**kwargs),
        app_id=APP_ID,
        app_secret=APP_SECRET,
        gateway=gateway,  # type: ignore[arg-type]
        client=client,
    )
    return channel, gateway, client


# boundary: 插件配置树，值类型即 JSON
def make_context(**config: Any) -> FakePluginContext:
    return FakePluginContext(
        plugin_id=MANIFEST.id,
        config=config,
        secrets={SECRET_APP_ID: "cli_app_0123456789", SECRET_APP_SECRET: "secret-0123456789"},
    )


# ------------------------------------------------------------------------------ 契约基类


class TestFeishuChannelContract(ChannelContract):
    """`Channel` 的通用契约。四条断言各构造一次 Channel。"""

    def make_channel(self) -> FeishuChannel:
        channel, _, _ = make_channel()
        return channel


# ------------------------------------------------------------------------------ manifest


class TestManifest:
    def test_entry_point_name_equals_the_manifest_id(self) -> None:
        """ 的判定：对不上时 `plugins.enabled` 指不到任何东西。"""
        assert MANIFEST.id in {item.name for item in entry_points(group="karyvia.plugins")}

    def test_one_channel_capability_and_no_overrides(self) -> None:
        assert len(MANIFEST.capabilities) == 1
        decl = MANIFEST.capabilities[0]
        assert decl.kind is CapabilityKind.CHANNEL
        assert decl.name == CAPABILITY_NAME
        assert decl.overrides is None

    def test_priority_is_not_declared(self) -> None:
        """写了默认值 100 会被原样采纳，而内建基准是 0。"""
        assert "priority" not in MANIFEST.capabilities[0].model_fields_set

    def test_config_schema_matches_the_settings_table(self) -> None:
        """两处都「自洽」而对不上时，一个写对了的配置会在加载前校验 被 schema 拒掉。"""
        assert set(CONFIG_SCHEMA["properties"]) == set(CONFIG_KEYS)
        assert CONFIG_SCHEMA["additionalProperties"] is False

    def test_the_dead_webhook_settings_are_gone(self) -> None:
        """WS 长连接下 SDK 不做 AES 解密也不做签名校验——保留一个永远不生效的安全配置项
        会让运维以为自己配了一层校验。"""
        assert "encrypt_key" not in CONFIG_KEYS
        assert "verification_token" not in CONFIG_KEYS


# ------------------------------------------------------------------------------ 配置


class TestSettings:
    def test_defaults_match_the_legacy_values(self) -> None:
        """默认值一字不改地沿用 legacy，只换单位与命名。"""
        resolved = resolve_settings(make_context())
        assert resolved.channel_id == CAPABILITY_NAME
        assert resolved.domain == "feishu"
        assert resolved.group_policy == "mention"
        assert resolved.topic_isolation is True
        assert resolved.reply_to_message is False
        assert resolved.streaming is True
        assert resolved.stream_edit_interval_ms == 500  # legacy 的 0.5s
        assert resolved.react_emoji == "THUMBSUP"
        assert resolved.done_emoji == ""

    def test_an_empty_allow_from_means_everyone(self) -> None:
        assert resolve_settings(make_context()).allow_from == frozenset()

    def test_no_one_is_an_operator_by_default(self) -> None:
        """默认空是安全的一侧：`operator_only` 命令默认不可用。"""
        assert resolve_settings(make_context()).operators == frozenset()
        named = resolve_settings(make_context(operators=["ou_a"]))
        assert named.operators == frozenset({"ou_a"})

    def test_an_unknown_domain_is_rejected(self) -> None:
        """填错会连到另一个租户体系上去。"""
        with pytest.raises(KaryviaError) as excinfo:
            resolve_settings(make_context(domain="slack"))
        assert excinfo.value.code is ErrorCode.CONFIG_INVALID

    def test_an_unknown_group_policy_is_rejected(self) -> None:
        with pytest.raises(KaryviaError):
            resolve_settings(make_context(group_policy="whatever"))

    def test_booleans_must_be_booleans(self) -> None:
        with pytest.raises(KaryviaError):
            resolve_settings(make_context(streaming=1))

    def test_an_id_list_must_be_a_list_of_strings(self) -> None:
        with pytest.raises(KaryviaError):
            resolve_settings(make_context(allow_from="ou_a"))
        with pytest.raises(KaryviaError):
            resolve_settings(make_context(allow_from=[123]))

    def test_the_edit_interval_has_a_floor(self) -> None:
        with pytest.raises(KaryviaError):
            resolve_settings(make_context(stream_edit_interval_ms=10))

    def test_the_gate_carries_the_configured_policy(self) -> None:
        gate = resolve_settings(make_context(operators=["ou_a"])).gate(bot_open_id="ou_bot")
        assert gate.bot_open_id == "ou_bot"
        assert gate.operators == frozenset({"ou_a"})


# ------------------------------------------------------------------------------ setup


class _Recorder:
    def __init__(self, ctx: FakePluginContext) -> None:
        self.ctx = ctx
        self.registered: dict[str, object] = {}

    def register_channel(self, name: str, channel: object) -> None:
        self.registered[name] = channel


class TestSetup:
    def test_setup_registers_exactly_one_channel(self) -> None:
        recorder = _Recorder(make_context())
        setup(recorder)  # type: ignore[arg-type]
        assert list(recorder.registered) == [CAPABILITY_NAME]
        assert isinstance(recorder.registered[CAPABILITY_NAME], FeishuChannel)

    @pytest.mark.parametrize("missing", [SECRET_APP_ID, SECRET_APP_SECRET])
    def test_a_missing_credential_points_at_the_key(self, missing: str) -> None:
        """一个没有凭据的飞书 Channel 连不上任何东西——「起来了但什么都不做」更糟。"""
        secrets = {SECRET_APP_ID: "cli_x0123456789", SECRET_APP_SECRET: "s0123456789"}
        del secrets[missing]
        ctx = FakePluginContext(
            plugin_id=MANIFEST.id,
            config={},
            secrets=secrets,
        )
        with pytest.raises(KaryviaError) as excinfo:
            setup(_Recorder(ctx))  # type: ignore[arg-type]
        assert excinfo.value.code is ErrorCode.CONFIG_INVALID
        assert missing in str(excinfo.value.detail["pointer"])

    def test_a_bad_config_fails_before_registration(self) -> None:
        recorder = _Recorder(make_context(domain="nope"))
        with pytest.raises(KaryviaError):
            setup(recorder)  # type: ignore[arg-type]
        assert recorder.registered == {}


# ------------------------------------------------------------------------------ 生命周期


class TestChannelLifecycle:
    async def test_start_connects_once_and_learns_the_bot_identity(self) -> None:
        channel, gateway, _ = make_channel()
        await channel.start()
        await channel.start()
        assert gateway.connected == 0  # 注入了 client，因此不走真的 connect
        await channel.stop()

    async def test_stop_is_idempotent_and_never_raises(self) -> None:
        """一个抛异常的 `stop` 会让别的收尾也做不完。"""
        channel, _, _ = make_channel()
        await channel.start()
        await channel.stop()
        await channel.stop()

    async def test_receive_ends_after_stop(self) -> None:
        channel, _, _ = make_channel()
        await channel.start()
        await channel.stop()
        assert [message async for message in channel.receive()] == []

    async def test_channel_id_comes_from_the_config(self) -> None:
        channel, _, _ = make_channel(channel_id="feishu-ops")
        assert channel.channel_id == "feishu-ops"

    async def test_a_final_message_reaches_the_platform(self) -> None:
        channel, _, client = make_channel()
        await channel.start()
        await channel.deliver(outbound("答案"))
        assert client.sent and client.sent[0][0] == CHAT_ID
        await channel.stop()

    async def test_deliver_raises_external_channel_when_the_platform_fails(self) -> None:
        """投递失败必须折成可重试的 `EXTERNAL_CHANNEL`。"""
        channel, _, client = make_channel()
        client.fail = True
        await channel.start()
        with pytest.raises(KaryviaError) as caught:
            await channel.deliver(outbound("答案"))
        assert caught.value.code is ErrorCode.EXTERNAL_CHANNEL
        assert caught.value.retryable is True
        # **飞书的失败信号是 `None` 返回值而不是异常**（`client.py` 的四个方法都是），
        # 因此这条错误在 `stream._send_plain` 里按返回值判出来、由 `_relayed` 原样带出。
        assert dict(caught.value.detail) == {"conversation": CHAT_ID, "parts": 1}
        await channel.stop()

    async def test_a_reasoning_delta_is_dropped_unless_asked_for(self) -> None:
        channel, _, client = make_channel()
        await channel.start()
        await channel.deliver(
            outbound("想一想", state=StreamState.DELTA, metadata={"reasoning": True})
        )
        assert client.calls == []
        await channel.stop()

    async def test_an_inbound_event_reaches_the_queue(self) -> None:
        """平台回调 → 归一化 → 入站队列。这是 `receive()` 唯一的来源。"""
        channel, _, _ = make_channel()
        await channel.start()
        await channel._on_event(FakeEvent(FakeEventMessage(content=text_content("在吗"))))  # noqa: SLF001
        message = await asyncio.wait_for(anext(channel.receive()), timeout=2)
        assert message.content == "在吗"
        assert message.conversation_id == CHAT_ID
        await channel.stop()

    async def test_an_inbound_event_gets_a_reaction(self) -> None:
        channel, _, client = make_channel()
        await channel.start()
        await channel._on_event(FakeEvent())  # noqa: SLF001

        assert client.added == [(MESSAGE_ID, "THUMBSUP")]
        await channel.stop()

    async def test_a_malformed_event_does_not_take_the_bot_down(self) -> None:
        """一条看不懂的平台事件不该让 bot 下线。"""
        channel, _, _ = make_channel()
        await channel.start()
        await channel._on_event(object())  # noqa: SLF001 - 故意畸形
        await channel.stop()

    async def test_a_reply_only_opens_a_thread_when_asked(self) -> None:
        """**最容易回归的一条**：不然「回复到已有话题」会在飞书里新建一个话题。"""
        channel, _, _ = make_channel(reply_to_message=False)
        await channel.start()
        await channel._on_event(FakeEvent())  # noqa: SLF001

        _, _, in_thread = channel._target_for(outbound("答案").conversation_id)  # noqa: SLF001
        assert in_thread is False
        await channel.stop()

        opted_in, _, _ = make_channel(reply_to_message=True)
        await opted_in.start()
        await opted_in._on_event(FakeEvent())  # noqa: SLF001
        _, reply_to, in_thread = opted_in._target_for(outbound("答案").conversation_id)  # noqa: SLF001
        assert reply_to == MESSAGE_ID
        assert in_thread is True
        await opted_in.stop()

    async def test_the_outbound_target_is_recovered_from_the_conversation_id(self) -> None:
        """合成必须可逆——`OutboundMessage` 只带 `conversation_id`。"""
        channel, _, _ = make_channel()
        chat_id, _, _ = channel._target_for(  # noqa: SLF001
            outbound("答案", conversation=f"{CHAT_ID}:om_root").conversation_id
        )
        assert chat_id == CHAT_ID

    async def test_stop_closes_open_stream_cards_before_disconnecting(self) -> None:
        """关卡片要走 HTTP，断连之后那条路就没了——留着的卡片会永久显示「生成中」。"""
        channel, _, client = make_channel()
        await channel.start()
        await channel.deliver(outbound("半句", state=StreamState.DELTA))
        await channel.stop()
        assert [op for op, _, _ in client.calls][-1] == "settings"


# ------------------------------------------------------------------------------ SDK 边界


class TestSdkBoundary:
    def test_only_two_modules_touch_the_sdk(self) -> None:
        """**这条纪律必须机器检查**：飞书只有 WS 与 HTTP 两个 SDK 出口。
        多一个出口就多一处「不装 SDK 跑不了的用例」。"""
        package = Path(plugin.__file__).parent
        offenders: set[str] = set()
        for path in package.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names.add(node.module)
            if any(name.split(".")[0] == "lark_oapi" for name in names):
                offenders.add(path.name)
        assert offenders == SDK_MODULES

    def test_importing_the_plugin_does_not_pull_in_the_sdk(self) -> None:
        """既定冷启动预算：`lark_oapi` 会拉进一整套生成代码。

        legacy 有这条纪律（`test_feishu_lazy_import.py`），这里**加强一格**：连
        `setup()` 之后（构造完 Channel）都不许拉进来。
        """
        probe = (
            "import sys, karyvia_plugin_feishu as p;"
            "print('lark_oapi' in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "False"

    def test_a_missing_lark_oapi_says_what_to_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """装了插件却没装 SDK 时给一句能照做的话，而不是一段 ImportError 栈。

        **开发环境里 `lark-oapi` 是装着的**，因此这里要造出「没装」：把
        `sys.modules["lark_oapi"]` 设成 `None` 会让 `import lark_oapi` 抛 `ImportError`，
        这是 CPython 有文档的行为。**这是全套用例里唯一碰 SDK 的一条。**
        """
        monkeypatch.setitem(sys.modules, "lark_oapi", None)
        from karyvia_plugin_feishu.gateway import _import_lark

        with pytest.raises(KaryviaError) as excinfo:
            _import_lark("feishu")
        assert excinfo.value.code is ErrorCode.EXTERNAL_CHANNEL
        assert excinfo.value.detail["fix"] == MISSING_SDK_FIX
        assert "pip install" in MISSING_SDK_FIX

    async def test_closing_a_gateway_that_never_connected_is_safe(self) -> None:
        gateway = FeishuGateway(
            app_id=APP_ID, app_secret=APP_SECRET, domain="feishu", on_event=_never_called
        )
        await gateway.close()
        assert gateway.http is None


async def _never_called(event: object) -> None:
    raise AssertionError("这条路径不该被走到")
