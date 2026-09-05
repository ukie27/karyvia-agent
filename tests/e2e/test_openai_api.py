"""OpenAI 兼容接口的端到端验收（`D31`）。

职责：验被删掉的 `legacy/api/server.py` 的两条对外承诺在新 Kernel 上仍然成立——
`POST /v1/chat/completions`（流式与非流式）与 `GET /v1/models` 能完成一次真实 turn。
不负责：插件内部形状（插件自己的 `tests/`）、装配链结构（`tests/runtime/`）。

**唯一的替身仍然是模型的传输层**（`conftest.recorder`）：会话存储、上下文组装、
装配根、Channel 泵与 HTTP 服务全是生产实现，请求真的走 TCP 打到 `127.0.0.1`
（`conftest.no_real_network` 放行回环、拦其余目标）。

这套用例**要求 `openai-api` 插件已经由全局插件管理器安装**：

    karyvia plugins install --no-deps plugins/karyvia-plugin-openai-api

没装时第一条用例会以一句能照做的话失败。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from karyvia.contracts import Channel
from karyvia.runtime.bootstrap import bootstrap
from karyvia.runtime.first_run import MODEL_API_KEY_ENV, MODEL_PLUGIN_ID, MODEL_SECRET_NAME
from karyvia.runtime.instance import AgentInstance
from karyvia.runtime.plugin_home import GlobalPluginHome

from ._support import say
from .conftest import Recorder

aiohttp = pytest.importorskip("aiohttp", reason="OpenAI 兼容接口需要 aiohttp")

PLUGIN_ID = "openai-api"
SENTINEL_KEY = "sk-api0123456789abcdefghij"
MODEL_NAME = "gpt-4o-mini"


@pytest.fixture
def instance_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv(MODEL_API_KEY_ENV, SENTINEL_KEY)
    return tmp_path / "instance"


def write_config(instance_dir: Path, api_config: Mapping[str, object]) -> None:
    """一份启用了 `openai-api` 的最小配置。端口固定为 0：由内核分配空闲端口。"""
    instance_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "model": {"name": MODEL_NAME, "provider": "openai"},
        "plugins": {
            "enabled": [PLUGIN_ID],
            MODEL_PLUGIN_ID: {"secrets": {MODEL_SECRET_NAME: f"${{{MODEL_API_KEY_ENV}}}"}},
            PLUGIN_ID: {"config": {"port": 0, "model": MODEL_NAME, **api_config}},
        },
    }
    (instance_dir / "config.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )


def api_channel(instance: AgentInstance) -> Channel:
    for channel_id, channel in instance.channels:
        if channel_id == "api":
            return channel
    raise AssertionError(f"实例里没有 api 通道：{[cid for cid, _ in instance.channels]}")


@asynccontextmanager
async def serving(instance_dir: Path) -> AsyncIterator[tuple[AgentInstance, str]]:
    """装配、启动、交出 base_url。用完停掉并释放实例锁。"""
    instance = await bootstrap(instance_dir=instance_dir)
    try:
        await instance.start()
        port = getattr(api_channel(instance), "bound_port", None)
        assert isinstance(port, int) and port > 0, "Channel 没有报出真实端口"
        yield instance, f"http://127.0.0.1:{port}"
    finally:
        await instance.stop()


def test_the_plugin_is_installed() -> None:
    """先证明它真的装着——否则后面每一条都在验一台不存在的机器。"""
    names = {item.plugin_id for item in GlobalPluginHome.resolve().catalog()}
    assert PLUGIN_ID in names, (
        "openai-api 插件没有全局安装，请先跑："
        "karyvia plugins install --no-deps plugins/karyvia-plugin-openai-api"
    )


async def test_non_streaming_completion_runs_a_real_turn(
    instance_dir: Path, recorder: Recorder
) -> None:
    recorder.script(say("四十二。"))
    write_config(instance_dir, {})
    async with serving(instance_dir) as (_, base_url), aiohttp.ClientSession() as client:
        async with client.post(
            f"{base_url}/v1/chat/completions",
            json={"model": MODEL_NAME, "messages": [{"role": "user", "content": "答案是多少"}]},
        ) as response:
            assert response.status == 200
            body = await response.json()

    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "四十二。"
    assert body["choices"][0]["finish_reason"] == "stop"
    # 用量来自 `model.response_received` 事件，不是任何私有属性。
    assert "usage" in body
    assert body["usage"]["total_tokens"] >= 0
    assert len(recorder.requests) == 1


async def test_streaming_completion_emits_sse_frames(
    instance_dir: Path, recorder: Recorder
) -> None:
    recorder.script(say("流式回答。"))
    write_config(instance_dir, {})
    frames: list[str] = []
    async with serving(instance_dir) as (_, base_url), aiohttp.ClientSession() as client:
        async with client.post(
            f"{base_url}/v1/chat/completions",
            json={
                "model": MODEL_NAME,
                "messages": [{"role": "user", "content": "说点什么"}],
                "stream": True,
            },
        ) as response:
            assert response.status == 200
            assert response.headers["Content-Type"].startswith("text/event-stream")
            async for raw in response.content:
                line = raw.decode("utf-8").strip()
                if line.startswith("data: "):
                    frames.append(line[len("data: ") :])

    assert frames[-1] == "[DONE]"
    payloads = [json.loads(frame) for frame in frames[:-1]]
    assert payloads[0]["choices"][0]["delta"] == {"role": "assistant"}
    text = "".join(
        payload["choices"][0]["delta"].get("content", "")
        for payload in payloads
        if payload["choices"]
    )
    assert text == "流式回答。"
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"


async def test_history_persists_across_requests(instance_dir: Path, recorder: Recorder) -> None:
    """第二次请求必须看得见第一次的历史——证明它走的是生产会话路径。"""
    recorder.script(say("第一次。"), say("第二次。"))
    write_config(instance_dir, {})
    async with serving(instance_dir) as (_, base_url), aiohttp.ClientSession() as client:
        for prompt in ("你好", "还记得吗"):
            async with client.post(
                f"{base_url}/v1/chat/completions",
                headers={"X-Karyvia-Conversation": "shared"},
                json={"model": MODEL_NAME, "messages": [{"role": "user", "content": prompt}]},
            ) as response:
                assert response.status == 200
                await response.json()

    second = recorder.requests[1]
    sent = json.loads(second.content.decode("utf-8"))
    contents = [message.get("content") for message in sent["messages"]]
    assert "你好" in contents, f"第二次请求没带上第一轮的历史：{contents}"
    assert "第一次。" in contents


async def test_models_lists_the_configured_model(instance_dir: Path) -> None:
    write_config(instance_dir, {})
    async with serving(instance_dir) as (_, base_url), aiohttp.ClientSession() as client:
        async with client.get(f"{base_url}/v1/models") as response:
            assert response.status == 200
            body = await response.json()
    assert [entry["id"] for entry in body["data"]] == [MODEL_NAME]


async def test_system_messages_and_client_tools_are_refused(
    instance_dir: Path, recorder: Recorder
) -> None:
    """静默忽略会让客户端相信自己设了一个没生效的东西，因此这两样都是 400。"""
    recorder.script()
    write_config(instance_dir, {})
    async with serving(instance_dir) as (_, base_url), aiohttp.ClientSession() as client:
        for body in (
            {"messages": [{"role": "system", "content": "你是海盗"}, {"role": "user", "content": "嗨"}]},
            {"messages": [{"role": "user", "content": "嗨"}], "tools": [{"type": "function"}]},
        ):
            async with client.post(f"{base_url}/v1/chat/completions", json=body) as response:
                assert response.status == 400, await response.text()
                payload = await response.json()
                assert payload["error"]["type"] == "invalid_request_error"
    assert recorder.requests == [], "被拒的请求不该驱动任何一次模型调用"


async def test_bearer_token_is_enforced_when_configured(
    instance_dir: Path, recorder: Recorder
) -> None:
    recorder.script(say("已鉴权。"))
    instance_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "model": {"name": MODEL_NAME, "provider": "openai"},
        "plugins": {
            "enabled": [PLUGIN_ID],
            MODEL_PLUGIN_ID: {"secrets": {MODEL_SECRET_NAME: f"${{{MODEL_API_KEY_ENV}}}"}},
            PLUGIN_ID: {
                "config": {"port": 0, "model": MODEL_NAME},
                "secrets": {"api_key": f"${{{MODEL_API_KEY_ENV}}}"},
            },
        },
    }
    (instance_dir / "config.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )

    async with serving(instance_dir) as (_, base_url), aiohttp.ClientSession() as client:
        payload = {"model": MODEL_NAME, "messages": [{"role": "user", "content": "嗨"}]}
        async with client.post(
            f"{base_url}/v1/chat/completions",
            headers={"Authorization": "Bearer wrong"},
            json=payload,
        ) as response:
            assert response.status == 401
        async with client.post(
            f"{base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {SENTINEL_KEY}"},
            json=payload,
        ) as response:
            assert response.status == 200
            body = await response.json()
    assert body["choices"][0]["message"]["content"] == "已鉴权。"
