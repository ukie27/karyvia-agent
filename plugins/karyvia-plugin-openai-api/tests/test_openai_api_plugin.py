"""`openai-api` 插件的自测：契约基类 + 配置校验 + 线格式。

职责：用 `sdk.testing.ChannelContract` 验 Channel 的四个成员，并单独验配置校验、
会话标识解析与终态映射——这些不需要起一个真实例。
不负责：端到端（那在宿主仓库的 `tests/e2e/test_openai_api.py`）。
"""

from __future__ import annotations

import pytest
from karyvia_plugin_openai_api import setup
from karyvia_plugin_openai_api.channel import ApiChannel
from karyvia_plugin_openai_api.hub import SessionHub
from karyvia_plugin_openai_api.settings import ApiSettings, resolve_settings

from karyvia.contracts import (
    ErrorCode,
    InstanceId,
    KaryviaError,
    OutboundMessage,
    SessionKey,
    StreamState,
    TurnId,
)
from karyvia.sdk.testing import FakePluginContext
from karyvia.sdk.testing.contracts import ChannelContract

pytest.importorskip("aiohttp", reason="Channel 需要 aiohttp")


def make_settings(**overrides: object) -> ApiSettings:
    base: dict[str, object] = {
        "host": "127.0.0.1",
        "port": 0,
        "model_id": "test-model",
        "instance_id": InstanceId("test"),
    }
    base.update(overrides)
    return ApiSettings(**base)  # type: ignore[arg-type]


class TestApiChannelContract(ChannelContract):
    """`Channel` 的四个成员必须满足与其它 Channel 完全相同的契约。"""

    def make_channel(self) -> ApiChannel:
        return ApiChannel(SessionHub(make_settings()))


# ---------------------------------------------------------------- 配置


def test_defaults_bind_loopback_and_need_no_credential() -> None:
    settings = resolve_settings(FakePluginContext(plugin_id="openai-api", config={}))
    assert settings.host == "127.0.0.1"
    assert settings.requires_auth is False


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "192.168.1.10", "example.internal", "::"],
)
def test_non_loopback_hosts_require_a_credential(host: str) -> None:
    """主机名也算「需要鉴权」：解析结果取决于 DNS，猜它等于替用户赌一把。"""
    assert make_settings(host=host).requires_auth is True


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_hosts_do_not_require_a_credential(host: str) -> None:
    assert make_settings(host=host).requires_auth is False


@pytest.mark.parametrize(
    ("key", "value"),
    [("port", 70000), ("port", "8080"), ("host", 1), ("show_reasoning", 1)],
)
def test_bad_config_fails_with_a_pointer(key: str, value: object) -> None:
    ctx = FakePluginContext(plugin_id="openai-api", config={key: value})
    with pytest.raises(KaryviaError) as caught:
        resolve_settings(ctx)
    assert caught.value.code is ErrorCode.CONFIG_INVALID
    assert caught.value.detail["pointer"] == f"/plugins/openai-api/config/{key}"


def test_binding_a_public_host_without_a_key_is_refused() -> None:
    """一个能执行 shell 的端点不该在没有凭据的情况下暴露到回环之外。"""

    class _Api:
        def __init__(self, ctx: FakePluginContext) -> None:
            self.ctx = ctx
            self.registered: list[str] = []

        def register_channel(self, name: str, channel: object) -> None:
            self.registered.append(name)

    api = _Api(
        FakePluginContext(
            plugin_id="openai-api",
            config={"host": "0.0.0.0"},
        )
    )
    with pytest.raises(KaryviaError) as caught:
        setup(api)  # type: ignore[arg-type]
    assert caught.value.code is ErrorCode.CONFIG_INVALID
    assert api.registered == []


def outbound(conversation: str, turn: str, text: str, state: StreamState) -> OutboundMessage:
    key = SessionKey(channel_id="api", conversation_id=conversation)
    return OutboundMessage(
        session_key=key,
        channel_id=key.channel_id,
        conversation_id=key.conversation_id,
        turn_id=TurnId(turn),
        content=text,
        stream_state=state,
    )


async def test_a_waiter_receives_its_conversations_messages() -> None:
    hub = SessionHub(make_settings())
    waiter = hub.open("c1")
    hub.route(outbound("c1", "t1", "半句", StreamState.DELTA))
    hub.route(outbound("c1", "t1", "整句", StreamState.FINAL))

    seen = [message async for message in waiter.stream(timeout_ms=1000)]
    assert [m.content for m in seen] == ["半句", "整句"]
    assert waiter.turn_id == TurnId("t1")


async def test_messages_for_an_unknown_conversation_are_dropped_quietly() -> None:
    """`deliver()` 约定不抛：一条投不出去的消息不该让 Kernel 记一次失败。"""
    hub = SessionHub(make_settings())
    channel = ApiChannel(hub)
    await channel.deliver(outbound("nobody", "t9", "x", StreamState.FINAL))


async def test_closing_the_hub_releases_in_flight_waiters() -> None:
    hub = SessionHub(make_settings())
    waiter = hub.open("c1")
    hub.close()
    assert [message async for message in waiter.stream(timeout_ms=1000)] == []


async def test_reasoning_deltas_are_hidden_unless_configured() -> None:
    hub = SessionHub(make_settings())
    waiter = hub.open("c1")
    channel = ApiChannel(hub)
    key = SessionKey(channel_id="api", conversation_id="c1")
    reasoning = OutboundMessage(
        session_key=key,
        channel_id=key.channel_id,
        conversation_id=key.conversation_id,
        turn_id=TurnId("t1"),
        content="想一想",
        stream_state=StreamState.DELTA,
        metadata={"reasoning": True},
    )
    await channel.deliver(reasoning)
    await channel.deliver(outbound("c1", "t1", "答案", StreamState.FINAL))

    seen = [message async for message in waiter.stream(timeout_ms=1000)]
    assert [m.content for m in seen] == ["答案"]


async def test_usage_is_absent_rather_than_zero_when_unseen() -> None:
    """没看到用量与「真的没花」是两个结论，不能塌成一个 0。"""
    hub = SessionHub(make_settings())
    assert hub.usage.take(TurnId("never-seen")) is None
