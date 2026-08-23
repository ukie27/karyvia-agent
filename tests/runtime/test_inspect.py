"""只读诊断路径（`D29` 的 `runtime/inspect.py`）。

职责：验三条承诺——**不取实例锁**、**不装完整实例**、**插件的问题只记不抛**，
以及「已发现 = 真的会被加载的那一批」在诊断路径上同样成立。
不负责：验渲染（`tests/runtime/cli/test_plugins_cli.py`）、验启动路径
（`tests/runtime/test_bootstrap.py`、`test_plugin_plan.py`）。

外部插件复用 `test_plugin_plan.py` 的 `write_plugin()`：两条路必须看到同一批插件，
各写一套夹具就等于允许它们慢慢分叉。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nucleamind.kernel.config import InstanceLayout, InstanceLock
from nucleamind.kernel.observability import PluginState
from nucleamind.runtime.inspect import inspect_capabilities, inspect_plugins

from ._support import SCRIPT, TEST_MANIFESTS, text_response, write_config
from .test_plugin_plan import write_plugin


@pytest.fixture(autouse=True)
def _script() -> None:
    SCRIPT[:] = [text_response("好的。")]


def _instance(root: Path, **plugins: object) -> None:
    write_config(root, plugins=plugins)


def _states(root: Path) -> dict[str, PluginState]:
    inspection = inspect_plugins(instance_dir=root, manifests=TEST_MANIFESTS)
    return {str(row.plugin_id): row.state for row in inspection.statuses}


# ------------------------------------------------------------------------ inspect_plugins


def test_an_enabled_plugin_is_listed_as_discovered(tmp_path: Path) -> None:
    """只跑到阶段 A——`setup` 一次都没被导入，因此状态停在 `discovered`。"""
    write_plugin(tmp_path / "ext", "alpha")
    _instance(tmp_path, enabled=["alpha"])
    assert _states(tmp_path)["alpha"] is PluginState.DISCOVERED


def test_a_skipped_plugin_carries_the_reason_from_the_inventory(tmp_path: Path) -> None:
    """跳过原因的文案只有一份（`inventory._SKIP_REASONS`），CLI 侧不再写第二份。"""
    write_plugin(tmp_path / "ext", "alpha")
    _instance(tmp_path)
    inspection = inspect_plugins(instance_dir=tmp_path, manifests=TEST_MANIFESTS)
    row = next(item for item in inspection.statuses if str(item.plugin_id) == "alpha")
    assert row.state is PluginState.DISABLED
    assert row.reason == "未列入 plugins.enabled"


def test_a_phase_a_failure_is_recorded_instead_of_raised(tmp_path: Path) -> None:
    """依赖缺失的插件从 `discovered` 移进 `failures`（`D27` 的「已发现 = 会被加载的」）。"""
    write_plugin(tmp_path / "ext", "alpha", dependencies=("missing",))
    _instance(tmp_path, enabled=["alpha"])
    inspection = inspect_plugins(instance_dir=tmp_path, manifests=TEST_MANIFESTS)
    assert inspection.inventory.discovered == ()
    assert _states(tmp_path)["alpha"] is PluginState.FAILED


def test_a_critical_plugin_failing_does_not_kill_the_query(tmp_path: Path) -> None:
    """**这条命令的全部意义就是把失败印出来**，跟着它一起死掉是最没用的行为。

    同一份插件在 `bootstrap()` 那条路上会让启动失败（`test_plugin_plan.py` 钉着），
    差别只在 `plan_external(strict=...)`——判定本身两条路完全相同。
    """
    write_plugin(tmp_path / "ext", "alpha", dependencies=("missing",), critical=True)
    _instance(tmp_path, enabled=["alpha"])
    assert _states(tmp_path)["alpha"] is PluginState.FAILED


# --------------------------------------------------------------------- 三条承诺


def test_the_queries_do_not_take_the_instance_lock(tmp_path: Path) -> None:
    """看一眼装了什么，不该与正在跑的实例互斥（`nm config show` 立的规矩）。"""
    _instance(tmp_path)
    layout = InstanceLayout.resolve(instance_dir=tmp_path)
    layout.ensure()
    lock = InstanceLock(layout.lock_path).acquire()
    try:
        assert inspect_plugins(instance_dir=tmp_path, manifests=TEST_MANIFESTS).statuses == ()
    finally:
        lock.release()


async def test_capabilities_reports_the_active_providers(tmp_path: Path) -> None:
    """内建能力照常出现在 `active` 里，每条带 `builtin` / `plugin:<id>` 标识。"""
    _instance(tmp_path)
    inspection = await inspect_capabilities(instance_dir=tmp_path, manifests=TEST_MANIFESTS)
    assert inspection.report is not None
    active = {f"{ref.kind.value}:{ref.name}": str(ref.provider) for ref in inspection.report.active}
    assert active["session_store:jsonl"] == "builtin"
    assert active["cli_entry:stdio"] == "builtin"
    assert all(outcome.error is None for outcome in inspection.outcomes)


async def test_capabilities_sees_an_external_plugin(tmp_path: Path) -> None:
    """外部插件以 `plugin:<id>` 身份出现——与内建在报告里分得开（`PLG-006`）。"""
    write_plugin(tmp_path / "ext", "alpha")
    _instance(tmp_path, enabled=["alpha"])
    inspection = await inspect_capabilities(instance_dir=tmp_path, manifests=TEST_MANIFESTS)
    assert inspection.report is not None
    providers = {ref.name: str(ref.provider) for ref in inspection.report.active}
    assert providers["alpha.ping"] == "plugin:alpha"
