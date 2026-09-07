"""外部插件的两阶段加载。

这套用例走的是**真实装配链**：真的 `plugin.toml` 落在真的搜索路径上，真的被发现、校验、
排序，再经与内建**同一条**注册路径（`wire_capabilities` → `load_into` →
`RegistrationBatch`）跑进 registry。只有模型是 Fake（`_support.TEST_MANIFESTS`），
理由与 `test_bootstrap.py` 相同。

本文件覆盖五条主线：

- **加载成功**：外部插件以 `plugin:<id>` 身份注册，配置块与内建同一条路交下去。
- **加载前校验 的三种落榜**（依赖缺失 / 成环 / 配置不合 schema）都不打掉实例，
  但都在 `/plugins` 的数据源里留下 `FAILED`。
- **失败隔离**：插件不能决定中断宿主。
- **事务性**：`setup` 中途抛异常时 registry 不留半注册状态。
- **零外部插件是一等路径**：内建基线照常启动。

`setup` 指向本模块的函数：`import_setup()` 接受任何 `module:func`，外部插件与内建在这
一点上没有区别。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from karyvia.contracts import ErrorCode, Plugin, PluginId, ToolSpec
from karyvia.kernel.observability import PluginState
from karyvia.kernel.plugins import STATE_FILE
from karyvia.runtime.bootstrap import bootstrap
from karyvia.runtime.instance import AgentInstance
from karyvia.sdk import KaryviaAPI
from karyvia.sdk.testing import EchoTool

from ._support import (
    SCRIPT,
    TEST_MANIFESTS,
    register_test_manifest,
    text_response,
    write_config,
)

#: 每个 `setup` 被调用时把自己的 id 追加进来。拓扑序的断言读它——顺序是**执行**顺序，
#: 不是清单顺序，两者只有在真的排过序时才一致。
SETUP_ORDER: list[str] = []
MANIFEST_FILENAME = "plugin.toml"


@pytest.fixture(autouse=True)
def _reset() -> None:
    SCRIPT[:] = [text_response("好的。")]
    SETUP_ORDER.clear()


# ------------------------------------------------------------------------ 外部插件的 setup


def _register(api: KaryviaAPI, plugin_id: str) -> None:
    SETUP_ORDER.append(plugin_id)
    api.register_tool(
        ToolSpec(
            name=f"{plugin_id}.ping",
            description=f"{plugin_id} 的探针工具。",
            parameters={"type": "object", "properties": {}},
        ),
        EchoTool(),
    )


def setup_alpha(api: KaryviaAPI) -> None:
    _register(api, "alpha")


def setup_beta(api: KaryviaAPI) -> None:
    _register(api, "beta")


def setup_gamma(api: KaryviaAPI) -> None:
    _register(api, "gamma")


def setup_explodes(api: KaryviaAPI) -> None:
    """先注册、再抛：这正是  要挡住的那半个批次。"""
    _register(api, "boom")
    raise RuntimeError("插件自己炸了")


# ------------------------------------------------------------------------------ 夹具


_MANIFEST = """
id = "{plugin_id}"
version = "1.0.0"
sdk_range = ">=0.1"
setup = "tests.runtime.test_plugin_plan:setup_{setup}"
dependencies = [{dependencies}]
{extra}

[[capabilities]]
kind = "tool"
name = "{plugin_id}.ping"
"""


def write_plugin(
    root: Path,
    plugin_id: str,
    *,
    setup: str | None = None,
    dependencies: tuple[str, ...] = (),
    extra: str = "",
) -> Path:
    """在搜索路径下放一个目录形态的插件。"""
    package = root / plugin_id
    package.mkdir(parents=True, exist_ok=True)
    manifest_path = package / MANIFEST_FILENAME
    manifest_path.write_text(
        _MANIFEST.format(
            plugin_id=plugin_id,
            setup=setup or plugin_id,
            dependencies=", ".join(f'"{item}"' for item in dependencies),
            extra=extra,
        ),
        encoding="utf-8",
    )
    register_test_manifest(manifest_path)
    return package


async def boot(root: Path, plugins: dict[str, object]) -> AgentInstance:
    """写一份带 `plugins` 小节的配置并装配。搜索路径固定为实例目录下的 `ext/`。"""
    write_config(root, plugins=plugins)
    return await bootstrap(instance_dir=root, manifests=TEST_MANIFESTS)


def tool_names(instance: AgentInstance) -> set[str]:
    return {spec.name for spec in instance.deps.tool_specs}


def statuses(instance: AgentInstance) -> dict[str, PluginState]:
    return {str(row.plugin_id): row.state for row in instance.diagnostics.plugins()}


# ------------------------------------------------------------------------------ 加载成功


async def test_an_enabled_plugin_is_loaded_and_registers_as_itself(tmp_path: Path) -> None:
    """外部插件以 `plugin:<id>` 身份注册——内建是 `Builtin()`，两者在报告里分得开。"""
    write_plugin(tmp_path / "ext", "alpha")
    instance = await boot(tmp_path, {"enabled": ["alpha"]})
    try:
        assert "alpha.ping" in tool_names(instance)
        providers = {ref.provider for ref in instance.report.active}
        assert Plugin(PluginId("alpha")) in providers
        # ：清单的状态叠上了生命周期的投影，因此一个跑完 `setup` 的插件不再显示
        # `discovered`（实例尚未 `start()`，所以还不是 `activated`）。
        assert statuses(instance)["alpha"] is PluginState.LOADED
    finally:
        await instance.stop()


async def test_an_unenabled_plugin_is_not_loaded(tmp_path: Path) -> None:
    """`plugins.enabled` 是外部插件的总开关，加载阶段不会绕过它。"""
    write_plugin(tmp_path / "ext", "alpha")
    instance = await boot(tmp_path, {})
    try:
        assert "alpha.ping" not in tool_names(instance)
        assert SETUP_ORDER == []
    finally:
        await instance.stop()


async def test_no_external_plugins_still_starts_on_the_builtin_baseline(tmp_path: Path) -> None:
    """、：外部发现为空时实例照常启动。"""
    write_config(tmp_path)
    instance = await bootstrap(instance_dir=tmp_path, manifests=TEST_MANIFESTS)
    try:
        assert "fs.read" in tool_names(instance)
    finally:
        await instance.stop()


# ------------------------------------------------------------------------------ 拓扑序


async def test_dependencies_are_set_up_first(tmp_path: Path) -> None:
    """`beta` 依赖 `alpha`，因此 `alpha.setup` 必须先跑完。"""
    write_plugin(tmp_path / "ext", "alpha")
    write_plugin(tmp_path / "ext", "beta", dependencies=("alpha",))
    instance = await boot(tmp_path, {"enabled": ["beta", "alpha"]})
    try:
        assert SETUP_ORDER == ["alpha", "beta"]
    finally:
        await instance.stop()


async def test_a_dependency_on_a_builtin_is_satisfied(tmp_path: Path) -> None:
    """内建在外部插件之前就注册完了，依赖它是合法的。"""
    write_plugin(tmp_path / "ext", "alpha", dependencies=("tools-fs",))
    instance = await boot(tmp_path, {"enabled": ["alpha"]})
    try:
        assert "alpha.ping" in tool_names(instance)
    finally:
        await instance.stop()


# ------------------------------------------------------------------------- 加载前校验 的落榜


async def test_a_missing_dependency_keeps_the_instance_up(tmp_path: Path) -> None:
    """插件落榜不打掉实例，但要在诊断里查得到。"""
    write_plugin(tmp_path / "ext", "alpha", dependencies=("nope",))
    instance = await boot(tmp_path, {"enabled": ["alpha"]})
    try:
        assert SETUP_ORDER == []
        assert "alpha.ping" not in tool_names(instance)
        assert statuses(instance)["alpha"] is PluginState.FAILED
    finally:
        await instance.stop()


async def test_a_dependency_cycle_fails_both_and_names_the_cycle(tmp_path: Path) -> None:
    """环路要指得出来，否则用户只知道「装不上」。"""
    write_plugin(tmp_path / "ext", "alpha", dependencies=("beta",))
    write_plugin(tmp_path / "ext", "beta", dependencies=("alpha",))
    instance = await boot(tmp_path, {"enabled": ["alpha", "beta"]})
    try:
        assert SETUP_ORDER == []
        state = statuses(instance)
        assert state["alpha"] is PluginState.FAILED and state["beta"] is PluginState.FAILED
        cycles = [
            row.failure.detail["cycle"]
            for row in instance.diagnostics.plugins()
            if row.failure is not None and "cycle" in row.failure.detail
        ]
        assert ["alpha", "beta", "alpha"] in cycles
    finally:
        await instance.stop()


async def test_a_config_that_breaks_the_schema_drops_the_plugin(tmp_path: Path) -> None:
    """配置不合 `config_schema` 时加载前校验失败，错误带 `config.json` 里的字段路径。"""
    schema = 'config_schema = { type = "object", properties = { retries = { type = "integer" } } }'
    write_plugin(tmp_path / "ext", "alpha", extra=schema)
    instance = await boot(
        tmp_path, {"enabled": ["alpha"], "alpha": {"config": {"retries": "三次"}}}
    )
    try:
        assert SETUP_ORDER == []
        (failure,) = [
            row.failure
            for row in instance.diagnostics.plugins()
            if str(row.plugin_id) == "alpha" and row.failure is not None
        ]
        assert failure.code is ErrorCode.CONFIG_INVALID
        problems = failure.detail["problems"]
        assert isinstance(problems, list)
        assert problems[0]["pointer"] == "/plugins/alpha/config/retries"
    finally:
        await instance.stop()


async def test_a_matching_config_reaches_setup(tmp_path: Path) -> None:
    """校验用的配置块与 `setup()` 拿到的必须是同一份，因此合法配置要真的放行。"""
    schema = 'config_schema = { type = "object", properties = { retries = { type = "integer" } } }'
    write_plugin(tmp_path / "ext", "alpha", extra=schema)
    instance = await boot(tmp_path, {"enabled": ["alpha"], "alpha": {"config": {"retries": 3}}})
    try:
        assert SETUP_ORDER == ["alpha"]
    finally:
        await instance.stop()


async def test_a_state_version_change_drops_the_plugin_and_keeps_the_state(
    tmp_path: Path,
) -> None:
    """状态目录记着 v1 而 manifest 声明 v2 时，旧状态原样保留。"""
    write_plugin(tmp_path / "ext", "alpha", extra="state_version = 2")
    state_dir = tmp_path / "plugins" / "alpha"
    state_dir.mkdir(parents=True)
    (state_dir / STATE_FILE).write_text(json.dumps({"state_version": 1}), encoding="utf-8")
    (state_dir / "data.jsonl").write_text("旧状态\n", encoding="utf-8")

    instance = await boot(tmp_path, {"enabled": ["alpha"]})
    try:
        assert SETUP_ORDER == []
        assert statuses(instance)["alpha"] is PluginState.FAILED
        assert (state_dir / "data.jsonl").read_text(encoding="utf-8") == "旧状态\n"
        assert json.loads((state_dir / STATE_FILE).read_text(encoding="utf-8"))["state_version"] == 1
    finally:
        await instance.stop()


# ------------------------------------------------------------------------------ 事务性


async def test_setup_raising_rolls_the_whole_batch_back(tmp_path: Path) -> None:
    """`boom` 在抛之前已经注册过一个工具，registry 里不许留下它。"""
    write_plugin(tmp_path / "ext", "boom", setup="explodes")
    instance = await boot(tmp_path, {"enabled": ["boom"]})
    try:
        assert SETUP_ORDER == ["boom"]  # setup 真的跑过
        assert "boom.ping" not in tool_names(instance)
        assert not any(
            ref.provider == Plugin(PluginId("boom")) for ref in instance.report.active
        )
        (outcome,) = [
            item for item in instance.outcomes if item.provider == Plugin(PluginId("boom"))
        ]
        assert outcome.error is not None
        # 只放类型名不放异常消息——第三方异常文本可能带着凭据。
        assert outcome.error.detail["exception"] == "RuntimeError"
        assert "插件自己炸了" not in json.dumps(dict(outcome.error.detail), ensure_ascii=False)
    finally:
        await instance.stop()


async def test_one_broken_plugin_does_not_take_the_others_down(tmp_path: Path) -> None:
    """不相干的插件照常加载。"""
    write_plugin(tmp_path / "ext", "boom", setup="explodes")
    write_plugin(tmp_path / "ext", "gamma")
    instance = await boot(tmp_path, {"enabled": ["boom", "gamma"]})
    try:
        assert "gamma.ping" in tool_names(instance)
    finally:
        await instance.stop()


# ------------------------------------------------------------------------------ 覆盖

async def test_an_override_target_that_does_not_exist_is_reported(tmp_path: Path) -> None:
    """目标不存在时该能力不生效，但插件不能借此中断实例启动。"""
    write_plugin(tmp_path / "ext", "alpha")
    manifest = tmp_path / "ext" / "alpha" / MANIFEST_FILENAME
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + 'overrides = "builtin:nope"\n', encoding="utf-8"
    )
    register_test_manifest(manifest)
    write_config(tmp_path, plugins={"enabled": ["alpha"]})
    instance = await bootstrap(instance_dir=tmp_path, manifests=TEST_MANIFESTS)
    try:
        assert [error.code for error in instance.report.failures] == [
            ErrorCode.CAPABILITY_OVERRIDE_TARGET_MISSING
        ]
        assert "alpha.ping" not in tool_names(instance)
    finally:
        await instance.stop()
