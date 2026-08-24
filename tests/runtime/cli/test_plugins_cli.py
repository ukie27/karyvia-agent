"""`nm plugins` 与 `nm capabilities`（`D29`；技术方案 §10.4、§10.5，需求 `EDG-505`、`NFR-502`）。

职责：逐条对着开发方案 `D29` 的验收表——`enable`/`disable` 只改配置不在当前进程生效、
`uninstall` 后状态目录仍在、`purge` 无 `--confirm` 拒绝执行且事先打印路径与体积、
`nm capabilities` 的 shadowed 关系可读且含 provider 标识。
不负责：验编辑器本身（`tests/runtime/test_config_edit.py`）、验只读路径的三条承诺
（`tests/runtime/test_inspect.py`）。

**退出码同样是主角**（`test_cli.py` 立的规矩）：0 成功、2 用法或配置错、
3「没事可做 / 没确认」。第三档与 `nm init` 已有的形态一致。
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping
from pathlib import Path

import pytest

from nucleamind.contracts import CapabilityKind, ErrorCode, JsonValue, NucleaError, ToolSpec
from nucleamind.runtime.cli.commands import plugins as plugins_subject
from nucleamind.runtime.cli.main import app
from nucleamind.runtime.plugin_home import GlobalPluginHome, InstalledPlugin
from nucleamind.sdk import NucleaAPI, parse_manifest
from nucleamind.sdk.testing import EchoTool, InMemorySessionStore

from .._support import SCRIPT, text_response, write_config
from ..test_plugin_plan import write_plugin

#: 覆盖内建会话存储的插件的 manifest。`overrides` 指向 `builtin:jsonl`——`SESSION_STORE`
#: 是 SINGLETON，不声明覆盖的话两份实现会双双出局（`D06` 的冲突语义）。
_OVERRIDE_MANIFEST = """
id = "shadow"
version = "2.0.0"
sdk_range = ">=0.1"
setup = "tests.runtime.cli.test_plugins_cli:setup_shadow"

[[capabilities]]
kind = "session_store"
name = "memory"
overrides = "builtin:jsonl"
"""


def setup_shadow(api: NucleaAPI) -> None:
    api.register_session_store("memory", InMemorySessionStore())


def setup_extra_tool(api: NucleaAPI) -> None:
    api.register_tool(
        ToolSpec(
            name="alpha.ping",
            description="外部插件的探针工具。",
            parameters={"type": "object", "properties": {}},
        ),
        EchoTool(),
    )


@pytest.fixture(autouse=True)
def _script() -> None:
    SCRIPT[:] = [text_response("好的。")]


@pytest.fixture
def instance(tmp_path: Path) -> Path:
    """一份带凭据引用的最小配置。`nm capabilities` 会真的跑内建的 `setup()`。"""
    write_config(
        tmp_path,
        plugins={
            "model-openai": {"secrets": {"api_key": "${NM_TEST_KEY}"}},
        },
    )
    GlobalPluginHome.resolve().register_instance(tmp_path)
    return tmp_path


def _args(root: Path, *rest: str) -> list[str]:
    return [*rest, "--instance-dir", str(root)]


def _config(root: Path) -> dict[str, object]:
    return json.loads((root / "config.json").read_text(encoding="utf-8"))


def _register_manifest(path: Path) -> None:
    """把测试 manifest 放进全局目录；实例目录不再兼任代码搜索路径。"""
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    manifest = parse_manifest(raw, origin=str(path))
    attribute = f"_MANIFEST_{manifest.id.replace('-', '_').upper()}"
    globals()[attribute] = manifest
    home = GlobalPluginHome.resolve()
    home.package_dir(manifest.id).mkdir(parents=True, exist_ok=True)
    rows = [item for item in home.catalog() if item.plugin_id != manifest.id]
    home.write_catalog(
        (
            *rows,
            InstalledPlugin(
                manifest.id,
                manifest.version,
                "test",
                f"{__name__}:{attribute}",
                str(path),
                dependencies=manifest.dependencies,
            ),
        )
    )


def _install_test_plugin(root: Path, plugin_id: str, **kwargs: object) -> Path:
    package = write_plugin(root / "ext", plugin_id, **kwargs)
    path = package / "plugin.toml"
    _register_manifest(path)
    return package


# ------------------------------------------------------------------------------ list


def test_usage_and_unknown_subcommands(instance: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert app(_args(instance, "plugins")) == 0
    assert "nm plugins" in capsys.readouterr().out
    assert app(_args(instance, "plugins", "nope")) == 2
    assert app(_args(instance, "plugins", "list", "--wat")) == 2
    assert app(["plugins", "install", "demo", "--instance", "work"]) == 2


def test_list_says_so_when_there_are_no_plugins(
    instance: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """零插件是一等形态（`EDG-101`）：一句确认，而不是一张让人怀疑查询失败了的空表。"""
    assert app(_args(instance, "plugins", "list")) == 0
    assert "没有发现任何外部插件" in capsys.readouterr().out


def test_list_shows_state_reason_and_capabilities(
    instance: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_test_plugin(instance, "alpha")
    assert app(_args(instance, "plugins", "list")) == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "disabled" in out
    # 原因直接取自 `inventory._SKIP_REASONS`，CLI 侧不写第二份文案。
    assert "未列入 plugins.enabled" in out

    assert app(_args(instance, "plugins", "enable", "alpha")) == 0
    capsys.readouterr()
    assert app(_args(instance, "plugins", "list")) == 0
    out = capsys.readouterr().out
    assert "discovered" in out and "tool:alpha.ping" in out


def test_list_json_is_machine_readable(instance: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _install_test_plugin(instance, "alpha")
    assert app(_args(instance, "plugins", "list", "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [row["plugin_id"] for row in payload["plugins"]] == ["alpha"]


# --------------------------------------------------------------- enable / disable


def test_enable_only_touches_the_config(instance: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """**首版不热更新**（需求 §4.2）：写进 `plugins.enabled`，当前进程什么都没变。"""
    _install_test_plugin(instance, "alpha")
    assert app(_args(instance, "plugins", "enable", "alpha")) == 0
    assert "下次启动" in capsys.readouterr().out
    assert _config(instance)["plugins"]["enabled"] == ["alpha"]  # type: ignore[index]


def test_enabling_twice_reports_nothing_to_do(
    instance: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_test_plugin(instance, "alpha")
    assert app(_args(instance, "plugins", "enable", "alpha")) == 0
    capsys.readouterr()
    assert app(_args(instance, "plugins", "enable", "alpha")) == 3
    assert "本来就已启用" in capsys.readouterr().out


def test_enable_lifts_an_existing_disable(
    instance: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`disable` 压过 `enabled`（`D25`），不摘掉它就等于让「启用」静默失效。

    摘掉这件事印在输出里——这条命令不做用户看不见的改动。
    """
    _install_test_plugin(instance, "alpha")
    assert app(_args(instance, "plugins", "disable", "alpha")) == 0
    capsys.readouterr()
    assert app(_args(instance, "plugins", "enable", "alpha")) == 0
    assert "从 plugins.disable 移除" in capsys.readouterr().out
    plugins = _config(instance)["plugins"]
    assert plugins["disable"] == [] and plugins["enabled"] == ["alpha"]  # type: ignore[index]


def test_disable_leaves_enabled_alone(instance: Path) -> None:
    """`enable` 是 `disable` 的逆操作，因此后者不动 `enabled`。"""
    _install_test_plugin(instance, "alpha")
    assert app(_args(instance, "plugins", "enable", "alpha")) == 0
    assert app(_args(instance, "plugins", "disable", "alpha")) == 0
    plugins = _config(instance)["plugins"]
    assert plugins["enabled"] == ["alpha"] and plugins["disable"] == ["alpha"]  # type: ignore[index]


def test_enable_restores_a_disabled_builtin_without_marking_it_external(
    instance: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """内建本来就存在；恢复它只需撤销 disable，不能伪造一条外部安装引用。"""
    assert app(_args(instance, "plugins", "disable", "model-openai")) == 0
    capsys.readouterr()

    assert app(_args(instance, "plugins", "enable", "model-openai")) == 0
    assert "内建插件恢复启用" in capsys.readouterr().out
    plugins = _config(instance)["plugins"]
    assert plugins["disable"] == []  # type: ignore[index]
    assert "enabled" not in plugins  # type: ignore[operator]


def test_disabling_twice_reports_nothing_to_do(instance: Path) -> None:
    assert app(_args(instance, "plugins", "disable", "alpha")) == 0
    assert app(_args(instance, "plugins", "disable", "alpha")) == 3


def test_a_missing_config_points_at_nm_init(tmp_path: Path) -> None:
    """还没 `nm init` 就 `enable`：报错指路，而不是造一份半截配置。"""
    assert app(_args(tmp_path, "plugins", "enable", "alpha")) == 2


def test_a_plugin_id_is_required(instance: Path) -> None:
    assert app(_args(instance, "plugins", "enable")) == 2
    assert app(_args(instance, "plugins", "enable", "a", "b")) == 2


# ------------------------------------------------------------------------ uninstall


def test_uninstall_keeps_the_state_directory(
    instance: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`EDG-505`：默认保留 `<instance>/plugins/<id>/`，并说清楚怎么删掉它。"""
    state = instance / "plugins" / "alpha"
    state.mkdir(parents=True)
    (state / "state.json").write_text("{}", encoding="utf-8")
    _install_test_plugin(instance, "alpha")
    assert app(_args(instance, "plugins", "enable", "alpha")) == 0
    capsys.readouterr()

    assert app(["plugins", "uninstall", "alpha"]) == 0
    out = capsys.readouterr().out
    assert _config(instance)["plugins"]["enabled"] == []  # type: ignore[index]
    assert state.is_dir()
    assert "状态目录仍保留" in out and "purge alpha --confirm" in out
    assert not GlobalPluginHome.resolve().package_dir("alpha").exists()


def test_uninstalling_something_absent_reports_nothing_to_do(instance: Path) -> None:
    assert app(["plugins", "uninstall", "alpha"]) == 3


def test_global_uninstall_clears_every_known_instance(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root in (first, second):
        write_config(
            root,
            plugins={
                "enabled": ["alpha"],
                "disable": ["alpha"],
                "alpha": {
                    "config": {},
                    "on_disable": "restore_builtin",
                },
            },
        )
        GlobalPluginHome.resolve().register_instance(root)
    _install_test_plugin(first, "alpha")

    assert app(["plugins", "uninstall", "alpha"]) == 0

    for root in (first, second):
        plugins = _config(root)["plugins"]
        assert plugins["enabled"] == [] and plugins["disable"] == []  # type: ignore[index]
        assert "alpha" not in plugins  # type: ignore[operator]


def test_global_uninstall_rolls_back_configs_when_a_later_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root in (first, second):
        write_config(root, plugins={"enabled": ["alpha"]})
        GlobalPluginHome.resolve().register_instance(root)
    _install_test_plugin(first, "alpha")

    real_write = plugins_subject.write_document
    failed = False

    def fail_second_once(path: Path, document: Mapping[str, JsonValue]) -> None:
        nonlocal failed
        if path == second / "config.json" and not failed:
            failed = True
            raise NucleaError(
                ErrorCode.PERSISTENCE_WRITE_FAILED,
                "simulated",
            )
        real_write(path, document)

    monkeypatch.setattr(plugins_subject, "write_document", fail_second_once)

    assert app(["plugins", "uninstall", "alpha"]) == 2
    assert _config(first)["plugins"]["enabled"] == ["alpha"]  # type: ignore[index]
    assert _config(second)["plugins"]["enabled"] == ["alpha"]  # type: ignore[index]
    home = GlobalPluginHome.resolve()
    assert {item.plugin_id for item in home.catalog()} == {"alpha"}
    assert home.package_dir("alpha").is_dir()


# ---------------------------------------------------------------------------- purge


def test_purge_prints_paths_and_size_before_asking_for_confirmation(
    instance: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`EDG-505` 的核心：**先看见将要失去什么**，再确认。没有 `--confirm` 就一个字节不删。"""
    state = instance / "plugins" / "alpha"
    state.mkdir(parents=True)
    (state / "blob.bin").write_bytes(b"x" * 2048)

    assert app(_args(instance, "plugins", "purge", "alpha")) == 3
    out = capsys.readouterr().out
    assert str(state) in out
    assert "1 个文件" in out and "2.0 KiB" in out
    assert "未删除任何东西" in out
    assert state.is_dir()


def test_purge_with_confirm_removes_the_state_directory(
    instance: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = instance / "plugins" / "alpha"
    (state / "nested").mkdir(parents=True)
    (state / "nested" / "blob.bin").write_bytes(b"x" * 16)

    assert app(_args(instance, "plugins", "purge", "alpha", "--confirm")) == 0
    # 路径与体积在确认之后同样印出来——那是回执，不是它取代了确认前的那一次。
    assert str(state) in capsys.readouterr().out
    assert not state.exists()


def test_purging_without_a_state_directory_reports_nothing_to_do(instance: Path) -> None:
    assert app(_args(instance, "plugins", "purge", "alpha", "--confirm")) == 3


# --------------------------------------------------------------------- capabilities


def test_capabilities_lists_active_providers(
    instance: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NM_TEST_KEY", "sk-0123456789abcdef")
    assert app(_args(instance, "capabilities")) == 0
    out = capsys.readouterr().out
    assert "session_store:jsonl ← builtin" in out
    assert "model:openai ← builtin" in out
    # 四段都印，哪怕为空——「零条冲突」是一条有价值的结论。
    assert "被覆盖：无" in out and "冲突：无" in out


def test_capabilities_answers_even_without_credentials(
    instance: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """凭据没导出时仍应展示能力表，并把模型提供方列为加载失败。"""
    monkeypatch.delenv("NM_TEST_KEY", raising=False)
    assert app(_args(instance, "capabilities")) == 0
    out = capsys.readouterr().out
    assert "加载失败的提供方" in out and "config.secret_missing" in out
    assert "session_store:jsonl ← builtin" in out


def test_capabilities_prints_the_shadowed_relation(
    instance: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """**覆盖不静默**（§8.3 第 4 条、`NFR-502`）：两侧都带 provider 标识。"""
    monkeypatch.setenv("NM_TEST_KEY", "sk-0123456789abcdef")
    package = instance / "ext" / "shadow"
    package.mkdir(parents=True)
    (package / "plugin.toml").write_text(_OVERRIDE_MANIFEST, encoding="utf-8")
    _register_manifest(package / "plugin.toml")
    assert app(_args(instance, "plugins", "enable", "shadow")) == 0
    capsys.readouterr()

    assert app(_args(instance, "capabilities")) == 0
    out = capsys.readouterr().out
    assert "session_store:jsonl ← builtin" in out
    assert "被覆盖 → session_store:memory ← plugin:shadow" in out
    # 被覆盖的那一份不再生效，覆盖它的那一份才在 active 段里。
    assert "生效能力" in out


def test_disabling_an_overriding_plugin_says_a_choice_is_needed(
    instance: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`D30`：禁用一个覆盖者之后还欠一个 `on_disable`，提前说出来。

    不说的话用户看到的是「已写入」然后下一次启动以 `CONFIG_INVALID` 失败。判定仍然只有
    `runtime/plugin_disable.py` 一处，这里只是提前一步告诉他。
    """
    monkeypatch.setenv("NM_TEST_KEY", "sk-0123456789abcdef")
    package = instance / "ext" / "shadow"
    package.mkdir(parents=True)
    (package / "plugin.toml").write_text(_OVERRIDE_MANIFEST, encoding="utf-8")
    _register_manifest(package / "plugin.toml")
    assert app(_args(instance, "plugins", "enable", "shadow")) == 0
    capsys.readouterr()

    assert app(_args(instance, "plugins", "disable", "shadow")) == 0

    out = capsys.readouterr().out
    assert "on_disable" in out
    assert "session_store:jsonl ← builtin" in out
    assert "restore_builtin" in out and "leave_missing" in out


def test_disabling_a_plugin_without_overrides_says_nothing_extra(
    instance: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """没覆盖过任何东西时不提——这个键对它没有意义，提了就是噪声。"""
    package = instance / "ext" / "shadow"
    package.mkdir(parents=True)
    (package / "plugin.toml").write_text(
        "\n".join(
            line for line in _OVERRIDE_MANIFEST.splitlines() if not line.startswith("overrides")
        ),
        encoding="utf-8",
    )
    _register_manifest(package / "plugin.toml")
    assert app(_args(instance, "plugins", "enable", "shadow")) == 0
    capsys.readouterr()

    assert app(_args(instance, "plugins", "disable", "shadow")) == 0
    assert "on_disable" not in capsys.readouterr().out


def test_capabilities_json_round_trips(
    instance: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NM_TEST_KEY", "sk-0123456789abcdef")
    assert app(_args(instance, "capabilities", "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {"active", "shadowed", "disabled", "failures"} == set(payload)
    assert any(
        row["kind"] == CapabilityKind.SESSION_STORE.value for row in payload["active"]
    )


def test_capabilities_rejects_unknown_options(instance: Path) -> None:
    assert app(_args(instance, "capabilities", "--wat")) == 2


def test_capabilities_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert app(["capabilities", "--help"]) == 0
    assert "nm capabilities" in capsys.readouterr().out


def test_list_prints_a_failed_plugin_with_its_reason(
    instance: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """阶段 A 落榜的插件在这里显示 `failed` 并带上 detail——那正是「为什么没加载」。"""
    _install_test_plugin(instance, "alpha", dependencies=("missing",))
    assert app(_args(instance, "plugins", "enable", "alpha")) == 0
    capsys.readouterr()
    assert app(_args(instance, "plugins", "list")) == 0
    out = capsys.readouterr().out
    assert "failed" in out and "missing" in out


def test_capabilities_prints_conflicts(
    instance: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """两份 SINGLETON 实现且无人声明覆盖 → 双方都不生效（`D06`），这条命令印出冲突。

    **冲突印出来而不是抛出去**：`raise_if_failed()` 是启动路径的语义，而这条命令的用户
    恰恰是来查「为什么起不来」的。
    """
    monkeypatch.setenv("NM_TEST_KEY", "sk-0123456789abcdef")
    package = instance / "ext" / "shadow"
    package.mkdir(parents=True)
    # 去掉 `overrides` 那一行：这就是「两个提供方抢同一个槽位」。
    manifest = "\n".join(
        line for line in _OVERRIDE_MANIFEST.splitlines() if not line.startswith("overrides")
    )
    (package / "plugin.toml").write_text(manifest, encoding="utf-8")
    _register_manifest(package / "plugin.toml")
    assert app(_args(instance, "plugins", "enable", "shadow")) == 0
    capsys.readouterr()

    assert app(_args(instance, "capabilities")) == 0
    out = capsys.readouterr().out
    assert "无法启动" in out
    assert "plugin.registration_conflict" in out
    # detail 里逐条列出抢同一槽位的双方，缺了它用户不知道该关掉哪一个。
    assert "claimants" in out
