"""实例目录布局的测试（`D10` 验收：路径推导、目录结构、`ensure()` 幂等）。

| 验收项 | 测试 |
| --- | --- |
| 实例目录解析优先级 | `TestResolution` |
| 目录名与文件名固定 | `test_layout_names_are_frozen` |
| 只读 `KARYVIA_*` | `test_legacy_nanobot_env_is_ignored` |
| `ensure()` 建目录且幂等 | `TestEnsure` |
| 派生路径都落在实例目录内 | `test_all_derived_paths_stay_inside_root` |

目录名以**字面量**断言：它们是持久化契约，一旦发布就有用户的数据落在那些名字下面。
从实现反推等于让改名无声通过。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from karyvia.contracts import ErrorCode, KaryviaError, PluginId
from karyvia.kernel.config import (
    CONFIG_FILENAME,
    DEFAULT_INSTANCE_NAME,
    INSTANCE_DIR_ENV,
    INSTANCE_NAME_ENV,
    INSTANCES_DIRNAME,
    KARYVIA_HOME_ENV,
    LOCK_FILENAME,
    LOGS_DIRNAME,
    PLUGINS_DIRNAME,
    SESSIONS_DIRNAME,
    WORKSPACE_DIRNAME,
    InstanceLayout,
)


def test_layout_names_are_frozen() -> None:
    """名字是持久化契约，逐个按字面量固定。"""
    assert CONFIG_FILENAME == "config.json"
    assert LOCK_FILENAME == "instance.lock"
    assert SESSIONS_DIRNAME == "sessions"
    assert PLUGINS_DIRNAME == "plugins"
    assert LOGS_DIRNAME == "logs"
    assert WORKSPACE_DIRNAME == "workspace"
    assert DEFAULT_INSTANCE_NAME == "default"
    assert INSTANCE_DIR_ENV == "KARYVIA_INSTANCE_DIR"
    assert INSTANCE_NAME_ENV == "KARYVIA_INSTANCE"
    assert INSTANCES_DIRNAME == "instances"
    assert KARYVIA_HOME_ENV == "KARYVIA_HOME"


class TestResolution:
    """`resolve()` 的优先级：显式目录 > 显式名 > 目录环境变量 > 名环境变量 > 默认。"""

    def test_explicit_dir_wins_over_everything(self, tmp_path: Path) -> None:
        layout = InstanceLayout.resolve(
            instance_dir=tmp_path / "explicit",
            instance="ignored",
            env={INSTANCE_DIR_ENV: str(tmp_path / "env-dir"), INSTANCE_NAME_ENV: "env-name"},
            home=tmp_path / "home",
        )
        assert layout.root == (tmp_path / "explicit").resolve()

    def test_explicit_name_beats_env(self, tmp_path: Path) -> None:
        layout = InstanceLayout.resolve(
            instance="work",
            env={INSTANCE_DIR_ENV: str(tmp_path / "env-dir"), INSTANCE_NAME_ENV: "env-name"},
            home=tmp_path,
        )
        assert layout.root == (tmp_path / ".karyvia" / "instances" / "work").resolve()

    def test_env_dir_beats_env_name(self, tmp_path: Path) -> None:
        layout = InstanceLayout.resolve(
            env={INSTANCE_DIR_ENV: str(tmp_path / "env-dir"), INSTANCE_NAME_ENV: "env-name"},
            home=tmp_path,
        )
        assert layout.root == (tmp_path / "env-dir").resolve()

    def test_env_name_used_when_no_dir(self, tmp_path: Path) -> None:
        layout = InstanceLayout.resolve(env={INSTANCE_NAME_ENV: "staging"}, home=tmp_path)
        assert layout.root == (tmp_path / ".karyvia" / "instances" / "staging").resolve()

    def test_falls_back_to_default_instance(self, tmp_path: Path) -> None:
        layout = InstanceLayout.resolve(env={}, home=tmp_path)
        assert layout.root == (
            tmp_path / ".karyvia" / "instances" / DEFAULT_INSTANCE_NAME
        ).resolve()

    def test_karyvia_home_is_the_data_root_itself(self, tmp_path: Path) -> None:
        """环境变量直接指向数据根，不能再偷偷追加 `.karyvia` 或 `global`。"""
        data_root = tmp_path / "karyvia-data"
        layout = InstanceLayout.resolve(env={KARYVIA_HOME_ENV: str(data_root)})
        assert layout.root == data_root.resolve() / "instances" / DEFAULT_INSTANCE_NAME

    def test_named_instance_uses_karyvia_home_directly(self, tmp_path: Path) -> None:
        data_root = tmp_path / "karyvia-data"
        layout = InstanceLayout.resolve(
            instance="work",
            env={KARYVIA_HOME_ENV: str(data_root)},
        )
        assert layout.root == data_root.resolve() / "instances" / "work"

    def test_relative_explicit_dir_becomes_absolute(self, tmp_path: Path) -> None:
        """相对路径必须变成绝对路径，否则后续 cwd 变化会改变实例位置。"""
        layout = InstanceLayout.resolve(instance_dir="rel-instance", env={}, home=tmp_path)
        assert layout.root.is_absolute()

    def test_legacy_nanobot_env_is_ignored(self, tmp_path: Path) -> None:
        """新层只读 `KARYVIA_*`（AGENTS.md）：旧名字不得有任何效果。"""
        layout = InstanceLayout.resolve(
            env={"NANOBOT_INSTANCE_DIR": str(tmp_path / "legacy"), "NANOBOT_INSTANCE": "legacy"},
            home=tmp_path,
        )
        assert layout.root == (
            tmp_path / ".karyvia" / "instances" / DEFAULT_INSTANCE_NAME
        ).resolve()


class TestInstanceNameValidation:
    """实例名会变成一段路径，因此必须挡住穿越与分隔符。"""

    @pytest.mark.parametrize("name", ["..", ".", "a/b", "a\\b", "   ", "x\x00y"])
    def test_rejects_unsafe_names(self, name: str, tmp_path: Path) -> None:
        """路径分量特有的形状由本模块判定，报 `CONFIG_INVALID`。"""
        with pytest.raises(KaryviaError) as caught:
            InstanceLayout.resolve(instance=name, env={}, home=tmp_path)
        assert caught.value.code in {ErrorCode.CONFIG_INVALID, ErrorCode.INPUT_MALFORMED}

    def test_rejects_empty_name(self, tmp_path: Path) -> None:
        """空名字由共用的 `validate_identifier` 拦下，因此码是 `INPUT_MALFORMED`。"""
        with pytest.raises(KaryviaError) as caught:
            InstanceLayout.resolve(instance="", env={}, home=tmp_path)
        assert caught.value.code is ErrorCode.INPUT_MALFORMED

    def test_rejects_names_too_long_for_a_path_component(self, tmp_path: Path) -> None:
        """实例名下面还要接 `sessions/<storage_id>.json`，Windows 上 260 字符就到顶。"""
        with pytest.raises(KaryviaError) as caught:
            InstanceLayout.resolve(instance="x" * 200, env={}, home=tmp_path)
        assert caught.value.code is ErrorCode.INPUT_TOO_LARGE

    @pytest.mark.parametrize("name", ["plugin-packages", "plugins.json", "plugin-manager.lock"])
    def test_global_names_are_valid_inside_the_instance_namespace(
        self, name: str, tmp_path: Path
    ) -> None:
        layout = InstanceLayout.resolve(instance=name, env={}, home=tmp_path)
        assert layout.root == (tmp_path / ".karyvia" / "instances" / name).resolve()

    @pytest.mark.parametrize("name", ["default", "work", "my-instance", "inst_2", "a1"])
    def test_accepts_reasonable_names(self, name: str, tmp_path: Path) -> None:
        layout = InstanceLayout.resolve(instance=name, env={}, home=tmp_path)
        assert layout.root.name == name


class TestEnsure:
    def test_creates_the_documented_directories(self, tmp_path: Path) -> None:
        layout = InstanceLayout.resolve(instance_dir=tmp_path / "inst", env={})
        layout.ensure()
        for path in (
            layout.root,
            layout.sessions_dir,
            layout.plugins_dir,
            layout.logs_dir,
            layout.workspace_dir,
        ):
            assert path.is_dir()

    def test_writes_no_files(self, tmp_path: Path) -> None:
        """`ensure()` 只建目录。生成 `config.json` 是 `D24` 的 `karyvia init`，不是加载路径。"""
        layout = InstanceLayout.resolve(instance_dir=tmp_path / "inst", env={})
        layout.ensure()
        assert not layout.config_path.exists()
        assert not layout.lock_path.exists()

    def test_is_idempotent(self, tmp_path: Path) -> None:
        layout = InstanceLayout.resolve(instance_dir=tmp_path / "inst", env={})
        layout.ensure()
        (layout.sessions_dir / "keep.json").write_text("{}", encoding="utf-8")
        layout.ensure()
        assert (layout.sessions_dir / "keep.json").exists()


def test_all_derived_paths_stay_inside_root(tmp_path: Path) -> None:
    """没有一个派生路径可以逃出实例目录。"""
    layout = InstanceLayout.resolve(instance_dir=tmp_path / "inst", env={})
    derived = [
        layout.config_path,
        layout.lock_path,
        layout.sessions_dir,
        layout.plugins_dir,
        layout.logs_dir,
        layout.workspace_dir,
        layout.events_log_path(date(2026, 8, 11)),
        layout.config_error_log_path(date(2026, 8, 11)),
        layout.plugin_state_dir(PluginId("acme.demo")),
        *layout.session_paths("s-1"),
    ]
    for path in derived:
        assert layout.root in path.parents or path == layout.root


def test_events_log_path_is_dated(tmp_path: Path) -> None:
    layout = InstanceLayout.resolve(instance_dir=tmp_path / "inst", env={})
    path = layout.events_log_path(date(2026, 8, 11))
    assert path.parent == layout.logs_dir
    assert "2026-08-11" in path.name


def test_to_json_is_serialisable(tmp_path: Path) -> None:
    """诊断视图要能直接打印或写盘。"""
    import json

    layout = InstanceLayout.resolve(instance_dir=tmp_path / "inst", env={})
    assert json.loads(json.dumps(layout.to_json()))
