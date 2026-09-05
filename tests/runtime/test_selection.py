"""运行时能力选择：显式配置统一 Turn Compactor。"""

from __future__ import annotations

import pytest

from karyvia.contracts import Builtin, CapabilityKind, ErrorCode, KaryviaError
from karyvia.kernel.config import validate_config
from karyvia.kernel.plugins import RegisteredTurnContextCompactor
from karyvia.kernel.registry import CapabilityRegistry
from karyvia.runtime.selection import select_turn_compactor
from karyvia.sdk.testing import StaticTurnContextCompactor


def registry_with_turn_compactor() -> tuple[CapabilityRegistry, StaticTurnContextCompactor]:
    registry = CapabilityRegistry()
    compactor = StaticTurnContextCompactor()
    with registry.batch(Builtin()) as batch:
        batch.add(
            CapabilityKind.TURN_COMPACTOR,
            "basic",
            RegisteredTurnContextCompactor(compactor=compactor),
        )
    registry.freeze(registry.registrations)
    return registry, compactor


def test_default_turn_compactor_is_selected_with_timeout() -> None:
    registry, compactor = registry_with_turn_compactor()
    selected = select_turn_compactor(registry, validate_config({}))

    assert selected.compactor is compactor
    assert selected.name == "basic"
    assert selected.timeout_ms == 120_000


def test_missing_turn_compactor_fails_startup_without_fallback() -> None:
    registry, _ = registry_with_turn_compactor()

    with pytest.raises(KaryviaError) as caught:
        select_turn_compactor(
            registry,
            validate_config({"context": {"turn_compactor": "missing"}}),
        )

    assert caught.value.code is ErrorCode.CAPABILITY_MISSING
    assert caught.value.detail["pointer"] == "/context/turn_compactor"


def test_no_turn_compactor_reports_the_required_kind() -> None:
    registry = CapabilityRegistry()
    registry.freeze(())

    with pytest.raises(KaryviaError) as caught:
        select_turn_compactor(registry, validate_config({}))

    assert caught.value.code is ErrorCode.CAPABILITY_MISSING
    assert caught.value.detail["kind"] == "TURN_COMPACTOR"
