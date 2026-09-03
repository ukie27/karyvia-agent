"""默认 Turn Context Compactor 的插件边界与确定性算法验收。"""

from __future__ import annotations

from nucleamind.builtins.context_compact_basic import BasicTurnContextCompactor
from nucleamind.builtins.registry import BUILTIN_MANIFESTS, CONTEXT_COMPACT_BASIC
from nucleamind.contracts import CapabilityKind
from nucleamind.sdk.testing import TurnContextCompactorContract


class TestBasicTurnContextCompactorContract(TurnContextCompactorContract):
    def make_compactor(self) -> BasicTurnContextCompactor:
        return BasicTurnContextCompactor()


def test_manifest_registers_the_default_required_capability() -> None:
    assert CONTEXT_COMPACT_BASIC in BUILTIN_MANIFESTS
    assert CONTEXT_COMPACT_BASIC.id == "context-compact-basic"
    assert [(item.kind, item.name) for item in CONTEXT_COMPACT_BASIC.capabilities] == [
        (CapabilityKind.TURN_COMPACTOR, "basic")
    ]
