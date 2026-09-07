"""禁用全部内建实现后 Kernel 仍能跑通一次 turn。

职责：证明 Kernel 不依赖 `builtins/` 的任何一项——用 Fake 能力经**生产装配路径**
（`runtime.wiring` + 唯一 Host）跑完一次带工具调用的 turn。
不负责：断言事件序列与 Hook 顺序（那在 `test_skeleton_turn.py`）。

**为什么在 `tests/integration/` 而不是 `tests/architecture/`**：这条验收必须
真的跑一次 turn，而 `tests/architecture/` 的既定职责是「只做 AST 与文本静态检查，不导入被测
模块」（由 `test_guard_integrity.py` 守着）。把一条需要事件循环、Fake 模型和完整编排的用例
放进那个包，会毁掉它唯一的、也是它有价值的那条性质。

两种「没有内建」的形态各测一遍，因为它们的失败模式不同：

- **一个 manifest 都不加载**（`manifests=`）：装配链本身要能跑完并冻结。 起
  `BUILTIN_MANIFESTS` 不再是空元组，因此这条改为显式传空清单——要证明的性质是「零内建可
  装配」，而不是「默认清单恰好是空的」。
- **有内建但被全部禁用**：`resolve(disabled=...)` 按提供方索引，
  被禁用的项进 `disabled` 段而不是消失——「它去哪了」必须查得到。
"""

from __future__ import annotations

from karyvia.contracts import (
    Builtin,
    CapabilityKind,
    Plugin,
    PluginId,
    ProviderId,
    ToolCall,
    TurnStatus,
)
from karyvia.kernel.plugins import (
    cli_entry_from,
    model_providers_from,
    session_store_from,
)
from karyvia.runtime.wiring import wire_capabilities
from karyvia.sdk import PluginContext, parse_manifest
from karyvia.sdk.testing import (
    EchoTool,
    FakeCliEntry,
    FakeModelProvider,
    FakePluginContext,
    text_response,
    tool_call_response,
)

from ._support import tool, wire


def context_for(provider: ProviderId) -> PluginContext:
    del provider
    return FakePluginContext()


# ------------------------------------------------------------ 形态一：一个 manifest 都不加载


async def test_wiring_without_any_manifest_still_freezes() -> None:
    """零内建照常装配完。"""
    result = await wire_capabilities(manifests=(), context_for=context_for)
    assert result.registry.frozen
    assert result.report.ok
    assert result.report.active == ()
    # 三项必需能力都没有实现—— 会在装配根上把这件事变成明确的启动错误，
    # 本层只如实回答「没有」。
    assert model_providers_from(result.registry) == ()
    assert session_store_from(result.registry) is None
    assert cli_entry_from(result.registry) is None


async def test_a_turn_runs_end_to_end_with_only_fake_capabilities() -> None:
    """ 的正题：Kernel 用内存型 Fake 独立跑通一次带工具调用的 turn。

    这里刻意用 `_support.wire()`——它走的是同一个生产 Host，而 `builtins/` 一行代码都没有
    参与。turn 能跑完，就说明 Kernel 的机制层不依赖任何内建实现。
    """
    skeleton = wire(
        [
            tool_call_response(
                ToolCall(call_id="c1", name="fs.read", arguments={"path": "notes.md"})
            ),
            text_response("读完了"),
        ],
        tools=[tool("fs.read", EchoTool())],
    )

    receipt = await skeleton.send("看一下文件")

    assert receipt.admitted
    assert receipt.outcome is not None
    assert receipt.outcome.status is TurnStatus.COMPLETED
    assert receipt.outcome.tool_calls == 1
    # 会话历史照常落库：没有内建 SessionStore，用的是 SDK 发布的内存实现。
    snapshot = await skeleton.sessions.load(receipt.outcome.correlation.session_key)
    assert snapshot.messages


# ------------------------------------------------- 形态二：有内建但被配置全部禁用


def probe_manifest(plugin_id: str) -> object:
    return parse_manifest(
        {
            "id": plugin_id,
            "version": "1.0.0",
            "sdk_range": ">=0.1.0",
            "setup": f"{plugin_id}:setup",
            "capabilities": [
                {"kind": "model", "name": "probe-model"},
                {"kind": "cli_entry", "name": "probe-cli"},
            ],
        },
        origin="test",
    )


def probe_setup(api: object) -> None:
    api.register_model_provider("probe-model", FakeModelProvider())  # type: ignore[attr-defined]
    api.register_cli_entry("probe-cli", FakeCliEntry())  # type: ignore[attr-defined]


async def test_disabling_every_provider_leaves_the_registry_usable_and_auditable() -> None:
    """`resolve(disabled=...)` 按**提供方**索引：被禁用的能力进 `disabled` 段而不是消失。

     要求「它去哪了」查得到——塌成「查不到」会让用户去错的方向排查。
    """
    manifests = [probe_manifest("probe")]

    def resolve_setup(target: str) -> object:
        del target
        return probe_setup

    enabled = await wire_capabilities(
        manifests=manifests,  # type: ignore[arg-type]
        context_for=context_for,
        resolve_setup=resolve_setup,  # type: ignore[arg-type]
    )
    assert len(enabled.report.active) == 2

    disabled = await wire_capabilities(
        manifests=manifests,  # type: ignore[arg-type]
        context_for=context_for,
        resolve_setup=resolve_setup,  # type: ignore[arg-type]
        disabled={Builtin(): "用户在配置里禁用了全部内建"},
    )
    assert disabled.report.active == ()
    assert len(disabled.report.disabled) == 2
    assert all(reason for _, reason in disabled.report.disabled)
    # 禁用不是失败：实例照常启动。
    assert disabled.report.ok
    assert disabled.registry.frozen


async def test_disabling_one_provider_does_not_touch_another() -> None:
    """禁用按提供方精确生效——否则「禁用一个插件」会顺手带走别人的能力。"""

    def resolve_setup(target: str) -> object:
        del target
        return probe_setup

    result = await wire_capabilities(
        manifests=[probe_manifest("a"), probe_manifest("b")],  # type: ignore[arg-type]
        context_for=context_for,
        provider_for=lambda m: Plugin(PluginId(m.id)),
        resolve_setup=resolve_setup,  # type: ignore[arg-type]
        disabled={Plugin(PluginId("a")): "禁用 a"},
    )
    survivors = {ref.provider for ref in result.report.active}
    assert survivors == {Plugin(PluginId("b"))}
    assert {ref.kind for ref in result.report.active} == {
        CapabilityKind.MODEL,
        CapabilityKind.CLI_ENTRY,
    }
