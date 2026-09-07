"""`kernel/observability/diagnostics.py` 的行为测试。

三类验收点：`capabilities()` 转发的是真实的 `ResolutionReport`、`plugins()` 在插件运行时
落地前默认为空、`turn(turn_id)` 能按 `sequence` 完整重放单个 turn 且不串到别的 turn。

「单 turn 事件序列可完整重放」在这里做端到端：用真的 `EventBus` 驱动一段
含 turn / model / tool 事件的序列，再从诊断查回来。
"""

from __future__ import annotations

import json
from typing import Final

from karyvia.contracts import (
    Builtin,
    CapabilityKind,
    CapabilityRef,
    Correlation,
    ErrorCode,
    EventName,
    InstanceId,
    KaryviaError,
    Plugin,
    PluginId,
    SessionKey,
    TurnId,
)
from karyvia.kernel.observability import (
    Diagnostics,
    EventBus,
    MemoryRingSink,
    PluginState,
    PluginStatus,
)
from karyvia.kernel.registry import Registration, ResolutionReport, resolve

INSTANCE: Final = InstanceId("inst-1")
ACME: Final = Plugin(PluginId("acme"))


def _correlation(turn: str) -> Correlation:
    return Correlation(
        instance_id=INSTANCE, session_key=SessionKey("cli", "local"), turn_id=TurnId(turn)
    )


def _registration(kind: CapabilityKind, name: str, provider: object = None) -> Registration:
    ref = CapabilityRef(kind=kind, name=name, provider=provider or Builtin())  # pyright: ignore[reportArgumentType]
    return Registration(ref=ref, payload=ref.target, priority=0)


def _report() -> ResolutionReport:
    return resolve(
        [
            _registration(CapabilityKind.TOOL, "fs.read"),
            _registration(CapabilityKind.TOOL, "web.fetch", ACME),
        ]
    ).report


def _diagnostics(ring: MemoryRingSink | None = None, **kwargs: object) -> Diagnostics:
    base: dict[str, object] = {
        "events": ring if ring is not None else MemoryRingSink(capacity=64),
        "capabilities_source": _report,
    }
    base.update(kwargs)
    return Diagnostics(**base)  # pyright: ignore[reportArgumentType]


# ------------------------------------------------------------------ capabilities()


def test_capabilities_returns_the_live_resolution_report() -> None:
    report = _diagnostics().capabilities()
    assert [ref.target for ref in report.active] == ["builtin:fs.read", "plugin:acme:web.fetch"]
    assert report.ok


def test_capabilities_source_is_read_at_query_time() -> None:
    """覆盖解析在启动期才产出，诊断门面在此之前就已装配好，因此来源必须是 callable。"""
    reports = [ResolutionReport(), _report()]
    diagnostics = _diagnostics(capabilities_source=lambda: reports.pop(0))
    assert diagnostics.capabilities().active == ()
    assert len(diagnostics.capabilities().active) == 2


# ------------------------------------------------------------------ plugins()


def test_plugins_is_empty_before_the_plugin_runtime_lands() -> None:
    assert _diagnostics().plugins() == ()


def test_plugin_status_carries_state_capabilities_and_failure() -> None:
    failure = KaryviaError(
        ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED, "manifest 有问题。", detail={"field": "sdk_range"}
    )
    status = PluginStatus(
        plugin_id=PluginId("acme"),
        version="1.2.3",
        state=PluginState.FAILED,
        capabilities=("plugin:acme:web.fetch",),
        failure=failure,
        failed_phase="manifest",
    )
    diagnostics = _diagnostics(plugins_source=lambda: [status])

    assert diagnostics.plugins() == (status,)
    payload = status.to_json()
    assert payload["plugin_id"] == "acme"
    assert payload["version"] == "1.2.3"
    assert payload["state"] == "failed"
    assert payload["capabilities"] == ["plugin:acme:web.fetch"]
    assert payload["failed_phase"] == "manifest"
    assert json.loads(json.dumps(payload)) == payload


def test_plugin_states_mirror_the_frozen_event_names() -> None:
    """不发明第二套插件生命周期 taxonomy：状态取值全部能在 `EventName` 的 plugin 族里找到。"""
    event_suffixes = {
        name.value.split(".", 1)[1] for name in EventName if name.value.startswith("plugin.")
    }
    assert {state.value for state in PluginState} - {"disabled"} <= event_suffixes


# ------------------------------------------------------------------ turn


def test_a_single_turn_can_be_replayed_in_full() -> None:
    ring = MemoryRingSink(capacity=64)
    bus = EventBus(INSTANCE)
    bus.subscribe(ring, name="ring")
    diagnostics = _diagnostics(ring)

    mine = _correlation("turn-a")
    other = _correlation("turn-b")

    bus.publish(EventName.INSTANCE_READY)
    bus.publish(EventName.TURN_STARTED, correlation=mine)
    bus.publish(EventName.TURN_STARTED, correlation=other)
    bus.publish(EventName.MODEL_REQUEST_STARTED, correlation=mine)
    bus.publish(EventName.TOOL_CALL_STARTED, correlation=mine, payload={"tool": "fs.read"})
    bus.publish(EventName.TOOL_CALL_COMPLETED, correlation=mine, payload={"tool": "fs.read"})
    bus.publish(EventName.MODEL_RESPONSE_RECEIVED, correlation=mine)
    bus.publish(EventName.TURN_COMPLETED, correlation=other)
    bus.publish(EventName.TURN_COMPLETED, correlation=mine)

    replay = diagnostics.turn(TurnId("turn-a"))
    assert [event.name.value for event in replay] == [
        "turn.started",
        "model.request_started",
        "tool.call_started",
        "tool.call_completed",
        "model.response_received",
        "turn.completed",
    ]
    sequences = [event.sequence for event in replay]
    assert sequences == sorted(sequences)


def test_turn_query_returns_empty_for_an_unknown_turn() -> None:
    assert _diagnostics().turn(TurnId("nope")) == ()


def test_dropped_events_tells_missing_from_evicted() -> None:
    """查不到某个 turn 时，「从来没有」和「被挤出去了」是两个结论。"""
    ring = MemoryRingSink(capacity=2)
    bus = EventBus(INSTANCE)
    bus.subscribe(ring, name="ring")
    diagnostics = _diagnostics(ring)

    old = _correlation("turn-old")
    for _ in range(4):
        bus.publish(EventName.TURN_STARTED, correlation=old)

    assert diagnostics.dropped_events == 2
    assert len(diagnostics.turn(TurnId("turn-old"))) == 2


# ------------------------------------------------------------------ 快照


def test_to_json_is_a_real_json_document() -> None:
    diagnostics = _diagnostics()
    payload = diagnostics.to_json()
    assert set(payload) == {"capabilities", "plugins", "dropped_events"}
    assert payload["plugins"] == []
    assert payload["dropped_events"] == 0
    assert json.loads(json.dumps(payload)) == payload
