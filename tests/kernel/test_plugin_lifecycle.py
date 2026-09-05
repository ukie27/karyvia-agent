"""插件生命周期的测试（`D28`；技术方案 §7.4，需求 `NFR-201`、`PLG-005`、`EDG-104`）。

四条主线：

- **状态机只有一张转换表**：非法转换是错误而不是被静默接受，失败记得住「发生在哪个
  阶段」——「发现时就坏了」与「停的时候超时了」的补救动作毫无共同之处。
- **停止顺序是启动拓扑序的逆序**（`PLG-005`），且它取自 `plan_load_order()` 的产物，
  不是第二次拓扑排序。
- **一个停不下来的插件只连累它自己**（`EDG-104`）：超时即放弃等待，后面的插件照停，
  调用方在预算内拿到结果。
- **阶段是判定口径、`PluginState` 是显示口径**：投影表逐条覆盖，不存在第二套枚举。
"""

from __future__ import annotations

import asyncio

import pytest

from karyvia.contracts import ErrorCode, KaryviaError
from karyvia.kernel.config.defaults import DEFAULT_PLUGIN_STOP_TIMEOUT_MS
from karyvia.kernel.observability import PluginState
from karyvia.kernel.plugins import (
    DEFAULT_STOP_TIMEOUT_MS,
    PHASE_STATES,
    PHASE_TRANSITIONS,
    PlanNode,
    PluginLifecycle,
    PluginPhase,
    StopUnit,
    plan_load_order,
    stop_order,
    stop_plugins,
    units_for,
)

# ------------------------------------------------------------------------------ 状态机


def test_the_happy_path_walks_the_whole_state_machine() -> None:
    """`DISCOVERED → VALIDATED → LOADED → STARTED → STOPPING → STOPPED`（§7.4 的原文）。"""
    lifecycle = PluginLifecycle(plugin_id="acme")
    assert lifecycle.phase is PluginPhase.DISCOVERED
    for phase in (
        PluginPhase.VALIDATED,
        PluginPhase.LOADED,
        PluginPhase.STARTED,
        PluginPhase.STOPPING,
        PluginPhase.STOPPED,
    ):
        lifecycle.advance(phase)
    assert lifecycle.phase is PluginPhase.STOPPED
    assert lifecycle.terminal
    assert lifecycle.error is None


@pytest.mark.parametrize(
    ("phase", "target"),
    [
        (PluginPhase.DISCOVERED, PluginPhase.LOADED),  # 跳过校验
        (PluginPhase.DISCOVERED, PluginPhase.STARTED),
        (PluginPhase.VALIDATED, PluginPhase.STARTED),  # 跳过 setup
        (PluginPhase.STOPPED, PluginPhase.STARTED),  # 停完又起来了
        (PluginPhase.STOPPED, PluginPhase.STOPPING),
        (PluginPhase.STARTED, PluginPhase.STARTED),  # 原地不动也是错
    ],
)
def test_an_illegal_transition_is_refused(phase: PluginPhase, target: PluginPhase) -> None:
    """非法转换抛 `KERNEL_INVARIANT_VIOLATED` 并说清楚当前允许去哪。"""
    lifecycle = PluginLifecycle(plugin_id="acme", phase=phase)
    with pytest.raises(KaryviaError) as caught:
        lifecycle.advance(target)
    assert caught.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED
    assert caught.value.detail["from"] == phase.value
    assert caught.value.detail["to"] == target.value
    assert lifecycle.phase is phase


def test_a_failure_records_the_phase_it_happened_in() -> None:
    """`任意阶段可进 FAILED 并记录阶段与原因`（§7.4 的原文）。"""
    lifecycle = PluginLifecycle(plugin_id="acme")
    lifecycle.advance(PluginPhase.VALIDATED)
    error = KaryviaError(ErrorCode.PLUGIN_LOAD_FAILED, "setup 执行失败。")
    lifecycle.fail(error)
    assert lifecycle.phase is PluginPhase.FAILED
    assert lifecycle.failed_phase is PluginPhase.VALIDATED
    assert lifecycle.error is error


def test_a_failed_plugin_can_still_be_cleaned_up() -> None:
    """`setup()` 中途失败的插件可能已经订阅过事件或派生过任务，它欠一次清理。"""
    lifecycle = PluginLifecycle(plugin_id="acme", phase=PluginPhase.FAILED)
    assert not lifecycle.terminal
    lifecycle.advance(PluginPhase.STOPPING)
    lifecycle.advance(PluginPhase.STOPPED)
    assert lifecycle.phase is PluginPhase.STOPPED


def test_failing_twice_is_refused() -> None:
    """第二条错误会盖掉第一条，而第一条才是根因。"""
    lifecycle = PluginLifecycle(plugin_id="acme")
    lifecycle.fail(KaryviaError(ErrorCode.PLUGIN_LOAD_FAILED, "第一次"))
    with pytest.raises(KaryviaError):
        lifecycle.fail(KaryviaError(ErrorCode.PLUGIN_LOAD_FAILED, "第二次"))
    assert lifecycle.error is not None
    assert lifecycle.error.user_message == "第一次"


def test_every_phase_projects_to_a_plugin_state() -> None:
    """投影表逐条覆盖：阶段是判定口径，`PluginState` 是显示口径，没有第二套枚举。"""
    assert set(PHASE_STATES) == set(PluginPhase)
    assert set(PHASE_TRANSITIONS) == set(PluginPhase)
    assert PluginLifecycle("acme").state is PluginState.DISCOVERED
    assert PHASE_STATES[PluginPhase.STARTED] is PluginState.ACTIVATED
    # 停到一半的插件还没停下，说它已停用是在报一个尚未成立的结论。
    assert PHASE_STATES[PluginPhase.STOPPING] is PluginState.ACTIVATED
    assert PHASE_STATES[PluginPhase.STOPPED] is PluginState.DEACTIVATED


def test_the_stop_budget_default_matches_the_config_schema() -> None:
    """两处各写一份字面量（`kernel.config` 不能 import 本包），因此要有人对着。"""
    assert DEFAULT_STOP_TIMEOUT_MS == DEFAULT_PLUGIN_STOP_TIMEOUT_MS == 5_000


# ------------------------------------------------------------------------------ 停止顺序


def unit(plugin_id: str, log: list[str], *, hang: bool = False, boom: bool = False) -> StopUnit:
    async def stop() -> None:
        if hang:
            await asyncio.Event().wait()
        log.append(plugin_id)
        if boom:
            raise RuntimeError("清理失败了")

    return StopUnit(plugin_id=plugin_id, stop=stop)


def test_stop_order_is_the_reverse_of_the_load_order() -> None:
    """构造 A→B→C 依赖链，断言停止顺序 C→B→A（`PLG-005`）。"""
    plan = plan_load_order(
        [
            PlanNode("c", dependencies=("b",)),
            PlanNode("b", dependencies=("a",)),
            PlanNode("a"),
        ]
    )
    assert plan.order == ("a", "b", "c")
    assert stop_order(plan.order) == ("c", "b", "a")


async def test_units_are_stopped_in_the_given_order() -> None:
    """`units_for()` 交出的就是执行顺序，且逐个而不是并发。"""
    log: list[str] = []
    plan = plan_load_order([PlanNode("b", dependencies=("a",)), PlanNode("a")])
    actions = {"a": unit("a", log).stop, "b": unit("b", log).stop}
    outcomes = await stop_plugins(units_for(plan.order, actions))
    assert log == ["b", "a"]
    assert [item.plugin_id for item in outcomes] == ["b", "a"]
    assert all(item.ok for item in outcomes)


def test_units_for_skips_ids_without_an_action() -> None:
    """阶段 A 落榜的插件在 `order` 里根本不存在，但装配根的表可能少于顺序表。"""
    units = units_for(("a", "b", "c"), {"a": unit("a", []).stop, "c": unit("c", []).stop})
    assert [item.plugin_id for item in units] == ["c", "a"]


# ------------------------------------------------------------------------------ 超时与失败


async def test_a_hanging_plugin_does_not_hold_up_the_rest() -> None:
    """`EDG-104`：超时即放弃等待、记一条 `TIMEOUT_PLUGIN_STOP`、继续停其余插件。"""
    log: list[str] = []
    units = (unit("slow", log, hang=True), unit("fast", log))
    outcomes = await asyncio.wait_for(stop_plugins(units, timeout_ms=50), timeout=5)
    assert log == ["fast"]  # 挂住的那个从没走到自己的记录点
    slow, fast = outcomes
    assert slow.timed_out and slow.error is not None
    assert slow.error.code is ErrorCode.TIMEOUT_PLUGIN_STOP
    assert slow.error.detail["timeout_ms"] == 50
    assert fast.ok and not fast.timed_out


async def test_a_hanging_plugin_leaves_its_lifecycle_in_failed() -> None:
    """超时不是 `STOPPED`——那会声称清理干净了。"""
    lifecycle = PluginLifecycle(plugin_id="slow", phase=PluginPhase.STARTED)
    hanging = unit("slow", [], hang=True)
    units = (StopUnit(plugin_id="slow", stop=hanging.stop, lifecycle=lifecycle),)
    await stop_plugins(units, timeout_ms=30)
    assert lifecycle.phase is PluginPhase.FAILED
    assert lifecycle.failed_phase is PluginPhase.STOPPING


async def test_a_raising_stop_action_is_folded_and_does_not_leak_its_message() -> None:
    """第三方插件的异常文本可能带凭据，因此只放类型名（`D13` 的先例）。"""
    log: list[str] = []
    lifecycle = PluginLifecycle(plugin_id="boom", phase=PluginPhase.STARTED)
    broken = unit("boom", log, boom=True)
    units = (
        StopUnit(plugin_id="boom", stop=broken.stop, lifecycle=lifecycle),
        unit("next", log),
    )
    outcomes = await stop_plugins(units)
    assert log == ["boom", "next"]
    assert outcomes[0].error is not None
    assert not outcomes[0].timed_out
    assert outcomes[0].error.detail["exception"] == "RuntimeError"
    assert "清理失败了" not in str(outcomes[0].error.detail)
    assert lifecycle.phase is PluginPhase.FAILED
    assert outcomes[1].ok


async def test_an_already_stopped_plugin_is_not_stopped_twice() -> None:
    """停止流程幂等：`AgentInstance.stop()` 之后再调一次不该重跑清理。"""
    log: list[str] = []
    lifecycle = PluginLifecycle(plugin_id="acme", phase=PluginPhase.STOPPED)
    once = unit("acme", log)
    units = (StopUnit(plugin_id="acme", stop=once.stop, lifecycle=lifecycle),)
    outcomes = await stop_plugins(units)
    assert log == []
    assert outcomes[0].ok


async def test_a_loaded_but_never_started_plugin_can_be_stopped() -> None:
    """装配失败时已经 `setup()` 过的插件没经过 `STARTED`，但仍然要被清理。"""
    log: list[str] = []
    lifecycle = PluginLifecycle(plugin_id="acme", phase=PluginPhase.LOADED)
    action = unit("acme", log)
    units = (StopUnit(plugin_id="acme", stop=action.stop, lifecycle=lifecycle),)
    await stop_plugins(units)
    assert log == ["acme"]
    assert lifecycle.phase is PluginPhase.STOPPED


async def test_no_units_is_an_ordinary_path() -> None:
    """未启用任何插件时停止流程什么都不做（`PLG-007`、`EDG-101` 的收尾侧）。"""
    assert await stop_plugins(()) == ()
