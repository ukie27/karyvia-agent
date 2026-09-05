from __future__ import annotations

from io import StringIO
from types import SimpleNamespace

from karyvia.contracts import (
    CapabilityKind,
    CapabilityRef,
    ErrorCode,
    KaryviaError,
    Plugin,
    PluginId,
)
from karyvia.kernel.observability import PluginState, PluginStatus
from karyvia.kernel.registry import ResolutionReport
from karyvia.runtime.cli.plugin_warnings import write_plugin_failures


def _instance(
    *statuses: PluginStatus, report: ResolutionReport | None = None
) -> object:
    diagnostics = SimpleNamespace(plugins=lambda: statuses)
    return SimpleNamespace(
        diagnostics=diagnostics,
        report=report or ResolutionReport(),
    )


def test_no_warning_when_every_enabled_plugin_is_healthy() -> None:
    stream = StringIO()
    instance = _instance(
        PluginStatus(
            plugin_id=PluginId("healthy"),
            version="1.0.0",
            state=PluginState.ACTIVATED,
        )
    )

    write_plugin_failures(instance, stream=stream)  # type: ignore[arg-type]

    assert stream.getvalue() == ""


def test_failed_enabled_plugins_are_printed_once_as_a_summary() -> None:
    stream = StringIO()
    failure = KaryviaError(ErrorCode.PLUGIN_LOAD_FAILED, "插件 setup 执行失败。")
    instance = _instance(
        PluginStatus(
            plugin_id=PluginId("broken"),
            version="1.0.0",
            state=PluginState.FAILED,
            failure=failure,
            failed_phase="loaded",
        )
    )

    write_plugin_failures(instance, stream=stream)  # type: ignore[arg-type]

    assert stream.getvalue() == (
        "karyvia: 1 个已启用插件未能加载或启动：\n"
        "  broken: [plugin.load_failed] 插件 setup 执行失败。\n"
    )


def test_capability_resolution_failure_names_the_external_plugin() -> None:
    stream = StringIO()
    plugin = Plugin(PluginId("broken"))
    failure = KaryviaError(
        ErrorCode.CAPABILITY_OVERRIDE_TARGET_MISSING,
        "覆盖目标不存在。",
        capability=CapabilityRef(
            kind=CapabilityKind.TOOL,
            name="broken.read",
            provider=plugin,
        ),
    )
    instance = _instance(report=ResolutionReport(failures=(failure,)))

    write_plugin_failures(instance, stream=stream)  # type: ignore[arg-type]

    assert stream.getvalue() == (
        "karyvia: 1 个已启用插件未能加载或启动：\n"
        "  broken: [capability.override_target_missing] 覆盖目标不存在。\n"
    )


def test_resolution_conflict_names_every_external_claimant_once() -> None:
    stream = StringIO()
    failure = KaryviaError(
        ErrorCode.CAPABILITY_OVERRIDE_CONFLICT,
        "多个插件声明覆盖同一目标。",
        detail={
            "claimants": [
                "plugin:zulu:read",
                "builtin:read",
                "plugin:acme:read",
            ]
        },
    )
    instance = _instance(report=ResolutionReport(failures=(failure,)))

    write_plugin_failures(instance, stream=stream)  # type: ignore[arg-type]

    assert stream.getvalue() == (
        "karyvia: 2 个已启用插件未能加载或启动：\n"
        "  acme: [capability.override_conflict] 多个插件声明覆盖同一目标。\n"
        "  zulu: [capability.override_conflict] 多个插件声明覆盖同一目标。\n"
    )
