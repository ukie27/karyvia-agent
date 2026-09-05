"""`echo-tool` 的测试：契约测试基类 + 本插件自己的行为。

职责：证明本插件的工具满足 `ToolContract`，以及 manifest 与实现对得上。
不负责：验证插件加载路径（那在宿主仓库的 `tests/e2e/test_plugin_runtime.py`）。

**先继承契约测试基类，再写自己的用例**：`sdk.testing` 的 5 个基类是「可替换性」的可执行
形态（`NFR-702`），一个第三方工具与内建 `fs.read` 因此被同一批断言检查。子类必须以
`Test` 开头，否则 pytest 不收集它。
"""

from __future__ import annotations

from karyvia_plugin_echo_tool import MANIFEST, TOOL_NAME, setup

from karyvia.contracts import (
    CapabilityKind,
    JsonValue,
    ToolCall,
    ToolHandler,
    ToolInvocation,
    ToolResult,
    ToolSpec,
)
from karyvia.sdk.testing import (
    FakePluginContext,
    ManualCancel,
    ToolContract,
    make_correlation,
)


class _Recorder:
    """最小的 `KaryviaAPI` 替身：只接住 `setup()` 会调的那一个注册方法。

    ctx 用 `sdk.testing.FakePluginContext`——那是 SDK 自己发布的夹具，插件作者不必为了
    测一个 `setup()` 去造一个受限运行时。
    """

    def __init__(self, config: dict[str, JsonValue] | None = None) -> None:
        self.ctx = FakePluginContext(plugin_id=MANIFEST.id, config=config or {})
        self.registered: list[tuple[ToolSpec, ToolHandler]] = []

    def register_tool(self, spec: ToolSpec, handler: ToolHandler) -> None:
        self.registered.append((spec, handler))


def registered(config: dict[str, JsonValue] | None = None) -> tuple[ToolSpec, ToolHandler]:
    """跑一次真的 `setup()`，交出它注册的那一对。"""
    recorder = _Recorder(config)
    setup(recorder)  # type: ignore[arg-type]  # 只用到 9 个注册方法里的一个。
    assert len(recorder.registered) == 1
    return recorder.registered[0]


async def call(handler: ToolHandler, spec: ToolSpec, text: JsonValue) -> ToolResult:
    return await handler.execute(
        ToolInvocation(
            call=ToolCall(call_id="call-1", name=spec.name, arguments={"text": text}),
            correlation=make_correlation(),
            timeout_ms=1_000,
        ),
        ManualCancel(),
    )


class TestEchoTool(ToolContract):
    """通用契约。夹具就是 `setup()` 会注册的那一对，不另造一个「测试用」的实现。"""

    def make_tool(self) -> tuple[ToolSpec, ToolHandler]:
        return registered()

    def valid_arguments(self) -> dict[str, JsonValue]:
        return {"text": "你好"}

    def invalid_arguments(self) -> dict[str, JsonValue] | None:
        return {"text": 42}


def test_the_manifest_declares_exactly_what_setup_registers() -> None:
    """`kernel/plugins/host.py` 会在加载时核对这件事；在插件自己的测试里先验一遍，
    失败信息比 `PLUGIN_LOAD_FAILED` 更靠近原因。"""
    declared = {(decl.kind, decl.name) for decl in MANIFEST.capabilities}
    spec, _ = registered()
    assert declared == {(CapabilityKind.TOOL, spec.name)} == {(CapabilityKind.TOOL, TOOL_NAME)}
