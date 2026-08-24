"""插件清单的测试（`D25`；技术方案 §7.1–§7.3，需求 `DST-002`、`CMP-001`、`SDK-005`）。

四条主线：

- **启用判定发生在读取之前**。未启用的候选连 manifest 都不读——用一份「读了就炸」的插件
  把这条变成可断言的事实，而不是一句承诺。
- **校验失败一律带字段路径与来源**（`CMP-001`）：缺必填字段 / id 非法 / 版本非 PEP 440
  各有一条。
- **不兼容与不匹配是两回事**：`sdk_range` 对不上是失败（不带病加载，`SDK-005`），
  平台不匹配只是跳过（用户什么都不用改）。
- **产出直接就是 `/plugins` 的数据源**，因此 `statuses()` 的投影逐条断言。
"""

from __future__ import annotations

import sys
import time
import tomllib
from pathlib import Path
from types import ModuleType

from nucleamind.contracts import CapabilityKind, ErrorCode
from nucleamind.kernel.observability import PluginState
from nucleamind.runtime.inventory import SkipReason, build_inventory
from nucleamind.sdk import SDK_VERSION, CapabilityDecl, PluginManifest

_VALID = """
id = "{plugin_id}"
version = "1.2.3"
sdk_range = ">=0.1"
setup = "acme.plugin:setup"
platforms = [{platforms}]

[[capabilities]]
kind = "tool"
name = "acme.ping"
"""

_POINTS: dict[Path, list[tuple[str, str]]] = {}


def _manifest_text(plugin_id: str, *, platforms: str = "") -> str:
    return _VALID.format(plugin_id=plugin_id, platforms=platforms)


def _plugin(root: Path, plugin_id: str, body: str | None = None) -> Path:
    package = root / plugin_id
    package.mkdir(parents=True, exist_ok=True)
    text = _manifest_text(plugin_id) if body is None else body
    try:
        raw: object = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raw = text
    _candidate(root, plugin_id, raw)
    return package


def _candidate(root: Path, plugin_id: str, raw: object) -> None:
    module_name = f"_inventory_{abs(hash(root))}_{plugin_id.replace('-', '_')}"
    module = ModuleType(module_name)
    module.MANIFEST = raw  # type: ignore[attr-defined]
    sys.modules[module_name] = module
    _POINTS.setdefault(root, []).append((plugin_id, f"{module_name}:MANIFEST"))


def _none() -> tuple[tuple[str, str], ...]:
    return ()


def _inventory(tmp_path: Path, **kwargs: object):  # type: ignore[no-untyped-def]
    kwargs.setdefault("entry_points", lambda: tuple(_POINTS.get(tmp_path, ())))
    return build_inventory(**kwargs)  # pyright: ignore[reportArgumentType]


# --------------------------------------------------------------------------- 启用


def test_an_enabled_directory_plugin_is_discovered(tmp_path: Path) -> None:
    _plugin(tmp_path, "acme")
    inventory = _inventory(tmp_path, enabled=["acme"])
    (item,) = inventory.discovered
    assert item.manifest.id == "acme"
    assert item.manifest.version == "1.2.3"
    assert not inventory.skipped and not inventory.failures


def test_an_unenabled_plugin_is_never_read(tmp_path: Path) -> None:
    """「安装 ≠ 启用」不是纪律而是没有路径：这份 manifest 读了就会失败。"""
    _plugin(tmp_path, "acme", "id = ")  # 语法错误的 TOML
    inventory = _inventory(tmp_path)
    assert not inventory.failures
    (skipped,) = inventory.skipped
    assert skipped.reason is SkipReason.NOT_ENABLED
    assert skipped.manifest is None


def test_disable_beats_enabled(tmp_path: Path) -> None:
    _plugin(tmp_path, "acme")
    (skipped,) = _inventory(tmp_path, enabled=["acme"], disabled=["acme"]).skipped
    assert skipped.reason is SkipReason.DISABLED


def test_an_enabled_but_disabled_plugin_is_not_read(tmp_path: Path) -> None:
    """`disable` 是最终闸门；即使仍在 `enabled`，也不读取插件声明。"""
    _plugin(tmp_path, "acme", "id = ")  # 读到就会产生 manifest 失败
    inventory = _inventory(tmp_path, enabled=["acme"], disabled=["acme"])
    assert not inventory.failures
    (skipped,) = inventory.skipped
    assert skipped.reason is SkipReason.DISABLED
    assert skipped.manifest is None


def test_a_disabled_plugin_that_was_never_enabled_is_not_read(tmp_path: Path) -> None:
    """闸门仍然只有一个：`plugins.enabled` 决定「会不会被读」。

    只写在 `disable` 里、从没启用过的候选不可能覆盖过任何东西，读它没有意义——而这份
    manifest 读了就会失败，所以「没读」在这里是可断言的。
    """
    _plugin(tmp_path, "acme", "id = ")  # 语法错误的 TOML
    inventory = _inventory(tmp_path, disabled=["acme"])
    assert not inventory.failures
    (skipped,) = inventory.skipped
    assert skipped.reason is SkipReason.DISABLED
    assert skipped.manifest is None


def test_an_unenabled_module_plugin_is_not_imported(tmp_path: Path) -> None:
    (tmp_path / "landmine.py").write_text("raise AssertionError('不该被导入')", encoding="utf-8")
    assert _inventory(tmp_path).failures == ()
    assert not any("landmine" in name for name in sys.modules)


def test_twenty_unenabled_entry_points_import_nothing() -> None:
    """`NFR-401`：未启用的插件不产生启动开销。20 个候选、0 次导入。"""
    points = tuple((f"ghost{i}", f"nucleamind_ghost_{i}.plugin:MANIFEST") for i in range(20))
    started = time.perf_counter()
    inventory = build_inventory(entry_points=lambda: points)
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert len(inventory.skipped) == 20
    assert not inventory.failures
    assert not any(name.startswith("nucleamind_ghost_") for name in sys.modules)
    # 结构性保证（一次导入都没有）才是本条的内容；墙钟只是它的下游，因此阈值给得很松，
    # 松到只有「有人偷偷加了 import」才会撞上。
    assert elapsed_ms < 100


# --------------------------------------------------------------------------- 校验


def test_a_missing_required_field_reports_the_field_path(tmp_path: Path) -> None:
    _plugin(tmp_path, "acme", 'id = "acme"\nversion = "1.0.0"\n')
    (failure,) = _inventory(tmp_path, enabled=["acme"]).failures
    assert failure.error.code is ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED
    fields = {item["field"] for item in failure.error.detail["errors"]}
    assert {"sdk_range", "setup", "capabilities"} <= fields
    assert failure.plugin_id == "acme" and "nucleamind.plugins:acme" in failure.origin


def test_an_illegal_id_reports_the_field_path(tmp_path: Path) -> None:
    _plugin(tmp_path, "Acme", _manifest_text("Acme"))
    (failure,) = _inventory(tmp_path, enabled=["Acme"]).failures
    # 语义校验（id 形状）直接抛 `NucleaError`，字段路径在 `detail["field"]` 而不是
    # `errors` 列表里——那是 `sdk/manifest.py` 刻意收窄的错误面，两种形状都带得出字段。
    assert failure.error.code is ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED
    assert failure.error.detail["field"] == "id"


def test_a_non_pep440_version_reports_the_field_path(tmp_path: Path) -> None:
    _plugin(tmp_path, "acme", _manifest_text("acme").replace('"1.2.3"', '"one"'))
    (failure,) = _inventory(tmp_path, enabled=["acme"]).failures
    assert failure.error.code is ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED
    assert failure.error.detail["field"] == "version"


def test_an_id_that_disagrees_with_its_source_name_fails(tmp_path: Path) -> None:
    """`plugins.enabled` 写的是来源名；静默采纳 manifest 里的另一个 id 会让它指不到东西。"""
    _plugin(tmp_path, "acme", _manifest_text("somethingelse"))
    (failure,) = _inventory(tmp_path, enabled=["acme"]).failures
    assert failure.error.code is ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED
    assert failure.error.detail["manifest_id"] == "somethingelse"


def test_an_incompatible_sdk_range_is_a_failure(tmp_path: Path) -> None:
    """`SDK-005`：不带病加载。"""
    _plugin(tmp_path, "acme", _manifest_text("acme").replace(">=0.1", ">=99.0"))
    (failure,) = _inventory(tmp_path, enabled=["acme"]).failures
    assert failure.error.code is ErrorCode.PLUGIN_SDK_INCOMPATIBLE
    assert failure.error.detail["sdk_range"] == ">=99.0"
    assert SDK_VERSION not in str(failure.error.detail)


def test_a_platform_mismatch_is_a_skip_not_a_failure(tmp_path: Path) -> None:
    """用户什么都不用改——换个平台它就生效了。"""
    body = _manifest_text("acme", platforms='"nonesuch"')
    _plugin(tmp_path, "acme", body)
    inventory = _inventory(tmp_path, enabled=["acme"])
    assert not inventory.failures
    (skipped,) = inventory.skipped
    assert skipped.reason is SkipReason.PLATFORM_MISMATCH
    assert skipped.manifest is not None and skipped.manifest.version == "1.2.3"


def test_the_platform_can_be_supplied_for_matrix_tests(tmp_path: Path) -> None:
    body = _manifest_text("acme", platforms='"win32"')
    _plugin(tmp_path, "acme", body)
    assert _inventory(tmp_path, enabled=["acme"], platform="win32").discovered
    assert _inventory(tmp_path, enabled=["acme"], platform="linux").skipped


def test_a_manifest_object_of_the_wrong_type_is_rejected(tmp_path: Path) -> None:
    _candidate(tmp_path, "solo", 42)
    (failure,) = _inventory(tmp_path, enabled=["solo"]).failures
    assert failure.error.code is ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED
    assert failure.error.detail["type"] == "int"


def test_a_real_manifest_object_is_taken_as_is(tmp_path: Path) -> None:
    """插件作者在自己的模块里就该直接构造 `PluginManifest`——它已经过 pydantic 校验。"""
    _candidate(
        tmp_path,
        "solo",
        PluginManifest(
            id="solo",
            version="0.1.0",
            sdk_range=">=0.1",
            setup="solo:setup",
            capabilities=(CapabilityDecl(kind="tool", name="solo.ping"),),
        ),
    )
    (item,) = _inventory(tmp_path, enabled=["solo"]).discovered
    assert isinstance(item.manifest, PluginManifest)
    assert item.manifest.capabilities == (
        CapabilityDecl(kind=CapabilityKind.TOOL, name="solo.ping"),
    )


def test_an_enabled_but_uninstalled_plugin_is_a_failure() -> None:
    inventory = build_inventory(enabled=["missing"], entry_points=_none)
    (failure,) = inventory.failures
    assert failure.plugin_id == "missing" and failure.error.code is ErrorCode.PLUGIN_LOAD_FAILED
    assert inventory.statuses()[0].state is PluginState.FAILED


# --------------------------------------------------------------------------- 诊断投影


def test_statuses_project_all_three_segments(tmp_path: Path) -> None:
    _plugin(tmp_path, "acme")
    _plugin(tmp_path, "sleepy")
    _plugin(tmp_path, "broken", "id = ")
    statuses = {
        row.plugin_id: row
        for row in _inventory(tmp_path, enabled=["acme", "broken"]).statuses()
    }

    assert statuses["acme"].state is PluginState.DISCOVERED
    assert statuses["acme"].version == "1.2.3"
    assert statuses["acme"].capabilities == ("tool:acme.ping",)

    # 未启用的那个**没被读过**，因此版本是空串——那不是漏填，是这条设计的证据。
    assert statuses["sleepy"].state is PluginState.DISABLED
    assert statuses["sleepy"].version == ""
    assert statuses["sleepy"].reason == "未列入 plugins.enabled"

    assert statuses["broken"].state is PluginState.FAILED
    assert statuses["broken"].failed_phase == "discovery"
    assert statuses["broken"].failure is not None


def test_the_inventory_serialises(tmp_path: Path) -> None:
    _plugin(tmp_path, "acme")
    _plugin(tmp_path, "sleepy")
    document = _inventory(tmp_path, enabled=["acme"]).to_json()
    assert document == {
        "discovered": ["acme"],
        "skipped": [{"plugin_id": "sleepy", "reason": "not_enabled"}],
        "failures": [],
    }


def test_nothing_configured_yields_an_empty_inventory() -> None:
    """默认形态：全局安装目录为空，实例也没启用任何插件。"""
    inventory = build_inventory(entry_points=_none)
    assert inventory == build_inventory(entry_points=_none, enabled=[])
    assert inventory.statuses() == ()
