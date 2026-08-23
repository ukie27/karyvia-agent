"""`on_disable` 判定的测试（`D30`；技术方案 §10.4，需求 `BAS-004`）。

三条主线：

- **只有声明过 `overrides` 的插件被禁用时才要求表态**。没覆盖过任何东西的插件被关掉只是
  少一项能力，为它也要求一次表态会让这个键变成噪声。
- **没表态即拒绝启动**，且错误指向那**一个**要写的键——不是「配置有问题」。
- **`leave_missing` 翻译成按能力抑制表，`restore_builtin` 什么都不做**。

`tests/e2e/test_plugin_runtime.py` 验的是这三条在一台真实例上的后果（内建回没回来、
实例起不起得来）；这里只验判定本身，因此不装任何东西。
"""

from __future__ import annotations

import pytest

from nucleamind.contracts import Builtin, CapabilityKind, ErrorCode, NucleaError
from nucleamind.kernel.config import NucleaConfig, validate_config
from nucleamind.kernel.plugins import PluginCandidate
from nucleamind.runtime.inventory import (
    DiscoveredPlugin,
    PluginInventory,
    SkippedPlugin,
    SkipReason,
)
from nucleamind.runtime.plugin_disable import (
    LEAVE_MISSING_REASON,
    override_targets,
    suppressed_capabilities,
)
from nucleamind.sdk import CapabilityDecl, PluginManifest

PLUGIN_ID = "session-memory"
SUPPRESSED_KEY = (CapabilityKind.SESSION_STORE, Builtin(), "jsonl")


def _manifest(*, overrides: str | None = "builtin:jsonl", plugin_id: str = PLUGIN_ID):
    return PluginManifest(
        id=plugin_id,
        version="0.1.0",
        sdk_range=">=0.1",
        setup="acme.plugin:setup",
        capabilities=(
            CapabilityDecl(
                kind=CapabilityKind.SESSION_STORE, name="memory", overrides=overrides
            ),
        ),
    )


def _candidate(plugin_id: str = PLUGIN_ID) -> PluginCandidate:
    return PluginCandidate(
        plugin_id=plugin_id, location="acme.plugin:MANIFEST"
    )


def _inventory(
    *, reason: SkipReason = SkipReason.DISABLED, manifest: PluginManifest | None = None
) -> PluginInventory:
    return PluginInventory(
        skipped=(
            SkippedPlugin(
                candidate=_candidate(),
                reason=reason,
                manifest=_manifest() if manifest is None else manifest,
            ),
        )
    )


def _config(entry: dict[str, object] | None = None) -> NucleaConfig:
    plugins: dict[str, object] = {"disable": [PLUGIN_ID]}
    if entry is not None:
        plugins[PLUGIN_ID] = entry
    return validate_config({"plugins": plugins})


def test_a_missing_choice_points_at_the_one_key_to_write() -> None:
    """`BAS-004`：不做判定的话内建会自动复活，那是被禁止的隐式恢复。"""
    with pytest.raises(NucleaError) as caught:
        suppressed_capabilities(_inventory(), _config())

    error = caught.value
    assert error.code is ErrorCode.CONFIG_INVALID
    assert error.detail["pointer"] == f"/plugins/{PLUGIN_ID}/on_disable"
    # 出错信息要同时说出**谁**覆盖了**什么**——只印目标的话用户不知道去改哪个插件条目。
    assert PLUGIN_ID in str(error.detail["overridden"])
    assert "session_store:jsonl" in str(error.detail["overridden"])


def test_restore_builtin_suppresses_nothing() -> None:
    assert suppressed_capabilities(_inventory(), _config({"on_disable": "restore_builtin"})) == {}


def test_leave_missing_suppresses_the_override_target() -> None:
    suppressed = suppressed_capabilities(
        _inventory(), _config({"on_disable": "leave_missing"})
    )
    assert suppressed == {SUPPRESSED_KEY: LEAVE_MISSING_REASON}


def test_a_plugin_without_overrides_needs_no_choice() -> None:
    """没有「要不要回来」这个问题时，这个键写不写都一样。"""
    assert suppressed_capabilities(_inventory(manifest=_manifest(overrides=None)), _config()) == {}


def test_an_unreadable_manifest_is_not_a_start_failure() -> None:
    """manifest 读不出来的被禁用插件（`manifest is None`）不参与判定。

    读不出来时我们不知道它覆盖过什么，也就没法要求一次有意义的表态。那条失败已经在
    `inventory.failures` 里留了记录——为一个已经被关掉的插件让实例起不来更糟。
    """
    inventory = PluginInventory(
        skipped=(
            SkippedPlugin(candidate=_candidate(), reason=SkipReason.DISABLED, manifest=None),
        )
    )
    assert suppressed_capabilities(inventory, _config()) == {}


@pytest.mark.parametrize("reason", [SkipReason.NOT_ENABLED, SkipReason.PLATFORM_MISMATCH])
def test_only_explicitly_disabled_plugins_are_considered(reason: SkipReason) -> None:
    """从没启用过、或平台不匹配的候选不可能覆盖过任何东西。

    尤其是 `NOT_ENABLED`：它连 manifest 都没被读过，这里之所以拿得到一份，只是因为夹具
    直接塞了进去。判定按**跳过原因**筛，而不是「有 manifest 就算」。
    """
    assert suppressed_capabilities(_inventory(reason=reason), _config()) == {}


def test_a_loaded_plugin_is_not_considered() -> None:
    """跑着的插件当然不用回答「你被关掉之后怎么办」。"""
    inventory = PluginInventory(
        discovered=(DiscoveredPlugin(candidate=_candidate(), manifest=_manifest()),)
    )
    assert suppressed_capabilities(inventory, validate_config({})) == {}


def test_override_targets_decodes_kind_from_the_declaring_side() -> None:
    """覆盖目标串里不带 kind——它取自声明覆盖的那一方（技术方案 §7.2）。"""
    (override,) = override_targets(_manifest())
    assert override.key == SUPPRESSED_KEY
    assert override.target == "session_store:jsonl ← builtin"
