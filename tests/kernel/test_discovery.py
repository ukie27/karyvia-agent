"""全局安装目录候选的 Kernel 发现机制。"""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

from nucleamind.contracts import ErrorCode, NucleaError
from nucleamind.kernel.plugins import ENTRY_POINT_GROUP, PluginCandidate, discover, read_candidate


def test_entry_point_metadata_becomes_a_candidate_without_importing() -> None:
    found = discover(entry_points=lambda: (("acme", "acme.plugin:MANIFEST"),))

    assert found.candidates == (PluginCandidate("acme", "acme.plugin:MANIFEST"),)
    assert ENTRY_POINT_GROUP in found.candidates[0].origin
    assert "acme.plugin" not in sys.modules


def test_default_discovery_does_not_scan_the_python_environment() -> None:
    assert discover().candidates == ()


def test_duplicate_global_ids_are_all_rejected() -> None:
    found = discover(
        entry_points=lambda: (
            ("acme", "first.plugin:MANIFEST"),
            ("acme", "second.plugin:MANIFEST"),
        )
    )

    assert found.candidates == ()
    (failure,) = found.failures
    assert failure.code is ErrorCode.PLUGIN_REGISTRATION_CONFLICT
    assert len(failure.detail["origins"]) == 2


def test_read_candidate_returns_the_manifest_object(monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType("acme_plugin")
    manifest = {"id": "acme"}
    module.MANIFEST = manifest  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "acme_plugin", module)

    found = read_candidate(PluginCandidate("acme", "acme_plugin:MANIFEST"))

    assert found is manifest


@pytest.mark.parametrize("location", ["", "module", ":MANIFEST", "module:"])
def test_entry_point_shape_is_validated(location: str) -> None:
    with pytest.raises(NucleaError) as caught:
        read_candidate(PluginCandidate("acme", location))

    assert caught.value.code is ErrorCode.PLUGIN_LOAD_FAILED
    assert caught.value.detail["source"] == "global_entry_point"


def test_import_failure_exposes_only_the_exception_type() -> None:
    with pytest.raises(NucleaError) as caught:
        read_candidate(PluginCandidate("acme", "module_that_does_not_exist:MANIFEST"))

    assert caught.value.detail["exception"] == "ModuleNotFoundError"
    assert "module_that_does_not_exist" not in caught.value.user_message


def test_missing_manifest_attribute_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "empty_plugin", ModuleType("empty_plugin"))

    with pytest.raises(NucleaError) as caught:
        read_candidate(PluginCandidate("acme", "empty_plugin:MANIFEST"))

    assert caught.value.code is ErrorCode.PLUGIN_LOAD_FAILED
    assert caught.value.detail["attribute"] == "MANIFEST"
