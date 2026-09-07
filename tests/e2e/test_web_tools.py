"""`web` 插件的端到端用例：两件工具在**真实装配的实例**上跑一次 turn。

职责：验「装上 web 插件之后，模型真的能搜、真的能抓，而抓的那一下真的过了 SSRF 守卫」。
不负责：插件自身的线格式与纯函数（`plugins/karyvia-plugin-web/tests/`）、
装配链内部结构（`tests/runtime/`）。

**这里唯一的替身仍然是传输层**（`conftest.recorder`），与 `test_plugin_runtime.py` 同一条
理由：模型供应商、注册路径、`ToolExecutor`、`runtime/access/net.py` 的守卫全是生产实现。
因此本文件要求 web 插件已经装进当前环境：

    karyvia plugins install --no-deps plugins/karyvia-plugin-web

**`web.fetch` 打不到公网，这是刻意的。** `conftest.py` 的网络闸门只放行回环，而
`GuardedHttpAccess` 恰恰**拒绝**回环——两条规则合起来意味着这套用例里没有任何地址是
「既解析得到又允许访问」的。于是这里验的是那条真正值得验的路：**模型给一个内网地址，
守卫把它挡下来，而实例照常继续**。搜索那一侧不受影响：它直接用 httpx，
被 `recorder` 换掉的传输层根本不做名字解析。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from karyvia.kernel.turn import CancelToken
from karyvia.runtime.bootstrap import bootstrap
from karyvia.runtime.first_run import MODEL_API_KEY_ENV, MODEL_PLUGIN_ID, MODEL_SECRET_NAME
from karyvia.runtime.inspect import inspect_capabilities
from karyvia.runtime.plugin_home import GlobalPluginHome

from ._support import say, use_tool
from .conftest import Recorder

WEB_PLUGIN = "web"
FETCH_TOOL = "web.fetch"
SEARCH_TOOL = "web.search"

#: 与 `test_out_of_box.py` 同一个哨兵：必须长得像密钥，否则「没泄漏」可能只是因为它
#: 压根不匹配 `contracts/errors.py` 的脱敏形状。
SENTINEL_KEY = "sk-web0123456789abcdefghij"

#: DuckDuckGo 的结果页片段。默认后端是 HTML 抓取，因此录制的也是 HTML。
_SEARCH_PAGE = (
    '<div class="result"><a class="result__a" href="https://example.org/cats">Cats</a>'
    '<a class="result__snippet">All about cats.</a></div>'
)


@pytest.fixture
def instance_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一台「全新机器」：空实例目录 + 临时 HOME + 没有导出任何凭据。

    与 `test_plugin_runtime.py` 里那份逐字相同。**刻意不 import 它**：跨测试模块 import
    夹具会让两份用例的前提悄悄耦合，而这十行的重复比那种耦合便宜（`AGENTS.md` 原则 5）。
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv(MODEL_API_KEY_ENV, raising=False)
    return tmp_path / "instance"


def write_config(instance_dir: Path, plugins: dict[str, object]) -> None:
    """写一份最小可用配置。`plugins` 那一段由每条用例自己给。"""
    instance_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "model": {"name": "gpt-4o-mini", "provider": "openai"},
        "plugins": {
            MODEL_PLUGIN_ID: {"secrets": {MODEL_SECRET_NAME: f"${{{MODEL_API_KEY_ENV}}}"}},
            **plugins,
        },
    }
    (instance_dir / "config.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )


async def run_prompt(instance_dir: Path, prompt: str) -> int:
    """装配一次真实例并跑一条单次执行（`karyvia run -p` 的正文）。"""
    instance = await bootstrap(instance_dir=instance_dir)
    try:
        return await instance.run_cli(["-p", prompt], CancelToken())
    finally:
        await instance.stop()


def _enable_web(instance_dir: Path, config: dict[str, object] | None = None) -> None:
    write_config(
        instance_dir,
        {"enabled": [WEB_PLUGIN], WEB_PLUGIN: {"config": config or {}}},
    )


def _search_page(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text=_SEARCH_PAGE, headers={"content-type": "text/html"})


def _last_tool_message(request: httpx.Request) -> str:
    """取出一次模型请求里最后一条 `role=tool` 消息的正文。

    工具失败是**一等结果**：它作为一条 tool 消息回到模型，而不是一次 turn 失败。
    因此「守卫真的挡住了」的证据就在这条消息里。
    """
    payload = json.loads(request.content)
    tools = [m for m in payload["messages"] if m.get("role") == "tool"]
    assert tools, "这次模型请求里没有工具结果"
    return str(tools[-1]["content"])


def test_the_web_plugin_is_installed_as_an_entry_point() -> None:
    """整套用例的前提。**单独成一条**：装漏了要看到一句能照做的话。"""
    names = {item.plugin_id for item in GlobalPluginHome.resolve().catalog()}
    assert WEB_PLUGIN in names, (
        "web 插件没装。请先跑 `karyvia plugins install --no-deps plugins/karyvia-plugin-web`"
    )


async def test_both_tools_reach_the_registry(instance_dir: Path) -> None:
    """manifest 声明两条、`setup()` 注册两条，因此生效集合里必须恰好有这两条。"""
    _enable_web(instance_dir)

    report = (await inspect_capabilities(instance_dir=instance_dir)).report
    active = {entry["name"] for entry in report.to_json()["active"]}

    assert {FETCH_TOOL, SEARCH_TOOL} <= active


async def test_the_model_can_search_and_gets_urls_back(
    instance_dir: Path, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一次完整 turn：模型调 `web.search` → 插件打搜索后端 → 结果回给模型 → 模型作答。

    脚本有三条，顺序即真实请求顺序（模型 → 搜索后端 → 模型）。**超出即失败**，
    因此「工具结果没回给模型、于是它又问一遍」这种情况会被看见而不是被吞掉。
    """
    monkeypatch.setenv(MODEL_API_KEY_ENV, SENTINEL_KEY)
    _enable_web(instance_dir)
    recorder.script(
        use_tool(SEARCH_TOOL, {"query": "cats"}),
        _search_page,
        say("找到了一条关于猫的结果。"),
    )

    assert await run_prompt(instance_dir, "搜一下猫") == 0

    bodies = [request.content for request in recorder.requests]
    # 第三次请求是模型的第二轮，里面必须带着工具结果——URL 是这个工具的产出重点。
    assert b"example.org/cats" in bodies[2]


async def test_a_private_address_is_refused_by_the_real_guard(
    instance_dir: Path, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型给一个内网地址，`ctx.net` 的守卫把它挡下来。

    这是本插件走 `ctx.net` 而不是自建守卫的全部理由——判定在
    `runtime/access/net.py`，这里验的是那条路真的被走到了。**实例照常继续**：
    一次被拒的抓取是一条工具失败，不是一次 turn 失败。
    """
    monkeypatch.setenv(MODEL_API_KEY_ENV, SENTINEL_KEY)
    _enable_web(instance_dir)
    recorder.script(
        use_tool(FETCH_TOOL, {"url": "http://127.0.0.1:8080/admin"}),
        say("那个地址访问不了。"),
    )

    assert await run_prompt(instance_dir, "看看本机管理页") == 0

    # 守卫在解析之后拒绝，因此**一次 HTTP 请求都没发出去**：录制器只看到两次模型请求。
    assert len(recorder.requests) == 2
    tool_message = _last_tool_message(recorder.requests[1])
    assert "守卫拒绝" in tool_message


async def test_a_missing_search_credential_does_not_take_fetch_down(
    instance_dir: Path, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """凭据不在 `setup()` 里取，因此配了 tavily 却没给 key 时，插件照样加载完成、
    `web.fetch` 照样在生效集合里——只有那一次搜索失败。"""
    monkeypatch.setenv(MODEL_API_KEY_ENV, SENTINEL_KEY)
    _enable_web(instance_dir, {"search": {"provider": "tavily"}})

    report = (await inspect_capabilities(instance_dir=instance_dir)).report
    active = {entry["name"] for entry in report.to_json()["active"]}
    assert {FETCH_TOOL, SEARCH_TOOL} <= active

    recorder.script(use_tool(SEARCH_TOOL, {"query": "cats"}), say("搜不了。"))
    assert await run_prompt(instance_dir, "搜一下猫") == 0

    # 工具结果里必须说清是凭据的问题，而不是一句「失败了」。
    tool_message = _last_tool_message(recorder.requests[1])
    assert "环境变量" in tool_message or "凭据" in tool_message


@pytest.mark.parametrize("tool", [FETCH_TOOL, SEARCH_TOOL])
async def test_neither_tool_is_registered_when_the_plugin_is_not_enabled(
    instance_dir: Path, tool: str
) -> None:
    """装上不等于启用。"""
    write_config(instance_dir, {})

    report = (await inspect_capabilities(instance_dir=instance_dir)).report
    assert tool not in {entry["name"] for entry in report.to_json()["active"]}
