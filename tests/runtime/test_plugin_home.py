"""全局插件目录、安装事务与全实例停机闸门。"""

from __future__ import annotations

from importlib.metadata import EntryPoint
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from karyvia.contracts import ErrorCode, KaryviaError
from karyvia.kernel.config import InstanceLock
from karyvia.runtime import plugin_home as subject


def _fake_install(target: Path, *, version: str = "1.2.3") -> None:
    (target / "alpha_plugin.py").write_text(
        "MANIFEST = {\n"
        '  "id": "alpha",\n'
        f'  "version": "{version}",\n'
        '  "sdk_range": ">=0.1",\n'
        '  "setup": "alpha_plugin:setup",\n'
        '  "capabilities": [{"kind": "tool", "name": "alpha.ping"}],\n'
        "}\n",
        encoding="utf-8",
    )


def _distribution(version: str = "1.2.3") -> SimpleNamespace:
    return SimpleNamespace(
        metadata={"Name": "karyvia-plugin-alpha"},
        version=version,
        entry_points=[
            EntryPoint(
                name="alpha",
                value="alpha_plugin:MANIFEST",
                group="karyvia.plugins",
            )
        ],
    )


def _python_distribution(*requirements: str) -> SimpleNamespace:
    return SimpleNamespace(
        metadata={"Name": "karyvia-plugin-alpha"},
        version="1.2.3",
        requires=list(requirements),
        entry_points=[
            EntryPoint(
                name="alpha",
                value="alpha_plugin:MANIFEST",
                group="karyvia.plugins",
            )
        ],
    )


def test_layout_lives_directly_under_karyvia_home(tmp_path: Path) -> None:
    home = subject.GlobalPluginHome.resolve(home=tmp_path, env={})

    assert home.root == tmp_path / ".karyvia"
    assert home.catalog_path == home.root / "plugins.json"
    assert home.packages_dir == home.root / "plugin-packages"
    assert home.named_instances_dir == home.root / "instances"
    assert "global" not in home.catalog_path.parts


def test_environment_value_is_the_home_itself(tmp_path: Path) -> None:
    data_root = tmp_path / "karyvia-data"
    home = subject.GlobalPluginHome.resolve(env={"KARYVIA_HOME": str(data_root)})

    assert home.root == data_root.resolve()
    assert home.catalog_path == data_root.resolve() / "plugins.json"


def test_python_discovery_does_not_treat_other_backends_as_import_paths(tmp_path: Path) -> None:
    home = subject.GlobalPluginHome.resolve(home=tmp_path, env={})
    home.write_catalog(
        (
            subject.InstalledPlugin(
                plugin_id="remote",
                version="1.0.0",
                distribution="remote-package",
                entry_point="remote-command",
                source="registry:remote",
                backend="process",
            ),
        )
    )

    assert home.entry_points() == ()


def test_install_and_uninstall_are_owned_by_the_global_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = subject.GlobalPluginHome.resolve(home=tmp_path, env={})
    home.ensure()

    def run_pip(
        source: str, target: Path, *, upgrade: bool, with_dependencies: bool
    ) -> None:
        assert source == "demo-source" and not upgrade and with_dependencies
        _fake_install(target)

    monkeypatch.setattr(subject, "_run_pip", run_pip)
    monkeypatch.setattr(subject, "distributions", lambda **_: [_distribution()])

    with home.mutation():
        record = subject.install_plugin(home, "demo-source")

    assert record.plugin_id == "alpha"
    assert record.backend == "python"
    assert home.package_dir("alpha").is_dir()
    assert home.entry_points() == (("alpha", "alpha_plugin:MANIFEST"),)

    with home.mutation():
        subject.uninstall_plugin(home, "alpha")

    assert home.catalog() == ()
    assert not home.package_dir("alpha").exists()


def test_host_distribution_is_validated_but_not_installed_into_plugin_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = _python_distribution("karyvia>=0.3.0", "lark-oapi>=1.5.0,<2.0.0")
    monkeypatch.setattr(subject, "distributions", lambda **_: (plugin,))
    monkeypatch.setattr(
        subject, "find_distribution", lambda name: SimpleNamespace(version="0.3.0")
    )

    assert subject._stage_requirements(tmp_path) == ("lark-oapi<2.0.0,>=1.5.0",)


def test_requested_package_extra_is_preserved_for_dependency_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = _python_distribution('mcp>=1.9.0; extra == "client"')
    monkeypatch.setattr(subject, "distributions", lambda **_: (plugin,))

    assert subject._stage_requirements(tmp_path, extras=("client",)) == (
        "mcp>=1.9.0",
    )


def test_dependency_install_runs_after_plugin_build_and_excludes_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = _python_distribution("karyvia>=0.3.0", "lark-oapi>=1.5.0,<2.0.0")
    commands: list[list[str]] = []
    monkeypatch.setattr(subject, "distributions", lambda **_: (plugin,))
    monkeypatch.setattr(
        subject, "find_distribution", lambda name: SimpleNamespace(version="0.3.0")
    )
    monkeypatch.setattr(
        subject,
        "_python_installer_command",
        lambda target: ["uv", "pip", "install", "--target", str(target)],
    )
    monkeypatch.setattr(
        subject.subprocess,
        "run",
        lambda command, **kwargs: commands.append(list(command))
        or SimpleNamespace(returncode=0),
    )

    subject._run_pip("demo-source", tmp_path, upgrade=True, with_dependencies=True)

    assert commands == [
        [
            "uv",
            "pip",
            "install",
            "--target",
            str(tmp_path),
            "--upgrade",
            "--no-deps",
            "demo-source",
        ],
        [
            "uv",
            "pip",
            "install",
            "--target",
            str(tmp_path),
            "--upgrade",
            "lark-oapi<2.0.0,>=1.5.0",
        ],
    ]


def test_no_deps_keeps_the_single_plugin_install_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(
        subject,
        "_python_installer_command",
        lambda target: ["uv", "pip", "install", "--target", str(target)],
    )
    monkeypatch.setattr(
        subject.subprocess,
        "run",
        lambda command, **kwargs: commands.append(list(command))
        or SimpleNamespace(returncode=0),
    )

    subject._run_pip("demo-source", tmp_path, upgrade=False, with_dependencies=False)

    assert commands == [
        [
            "uv",
            "pip",
            "install",
            "--target",
            str(tmp_path),
            "--no-deps",
            "demo-source",
        ]
    ]


def test_host_distribution_version_must_satisfy_plugin_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = _python_distribution("karyvia>=0.3.0")
    monkeypatch.setattr(subject, "distributions", lambda **_: (plugin,))
    monkeypatch.setattr(
        subject, "find_distribution", lambda name: SimpleNamespace(version="0.2.0")
    )

    with pytest.raises(KaryviaError) as caught:
        subject._stage_requirements(tmp_path)

    assert caught.value.code is ErrorCode.PLUGIN_LOAD_FAILED
    assert caught.value.detail["installed_version"] == "0.2.0"


def test_mutation_rejects_a_running_registered_instance(tmp_path: Path) -> None:
    home = subject.GlobalPluginHome.resolve(home=tmp_path, env={})
    instance = tmp_path / "outside-home-instance"
    instance.mkdir()
    lock = InstanceLock(instance / "instance.lock").acquire()
    home.register_instance(instance)
    try:
        with pytest.raises(KaryviaError) as caught, home.mutation():
            pass
    finally:
        lock.release()

    assert caught.value.code is ErrorCode.CONFIG_INSTANCE_LOCKED
    assert str(instance) in caught.value.detail["instances"]


def test_named_instances_are_scanned_only_inside_their_container(tmp_path: Path) -> None:
    home = subject.GlobalPluginHome.resolve(home=tmp_path, env={})
    named = home.named_instances_dir / "work"
    named.mkdir(parents=True)
    (named / "config.json").write_text("{}", encoding="utf-8")
    unrelated = home.root / "looks-like-an-instance"
    unrelated.mkdir(parents=True)
    (unrelated / "config.json").write_text("{}", encoding="utf-8")

    assert named.resolve() in home.instances()
    assert unrelated.resolve() not in home.instances()


def test_failed_install_does_not_publish_a_catalog_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = subject.GlobalPluginHome.resolve(home=tmp_path, env={})
    home.ensure()

    def fail(
        source: str, target: Path, *, upgrade: bool, with_dependencies: bool
    ) -> Any:
        del source, target, upgrade, with_dependencies
        raise KaryviaError(ErrorCode.PLUGIN_LOAD_FAILED, "broken")

    monkeypatch.setattr(subject, "_run_pip", fail)
    with pytest.raises(KaryviaError), home.mutation():
        subject.install_plugin(home, "broken-source")

    assert home.catalog() == ()


def test_install_set_rejects_missing_logical_dependencies() -> None:
    alpha = subject.InstalledPlugin(
        "alpha",
        "1.0.0",
        "alpha-dist",
        "alpha:MANIFEST",
        "alpha-source",
        dependencies=("missing",),
    )

    with pytest.raises(KaryviaError) as caught:
        subject._validate_install_set((alpha,))

    assert caught.value.code is ErrorCode.PLUGIN_LOAD_FAILED
    assert caught.value.detail["plugin_id"] == "alpha"


def test_install_set_rejects_two_versions_of_one_python_distribution() -> None:
    alpha = subject.InstalledPlugin(
        "alpha",
        "1.0.0",
        "alpha-dist",
        "alpha:MANIFEST",
        "alpha-source",
        resolved_distributions=("shared-lib==1.0",),
    )
    beta = subject.InstalledPlugin(
        "beta",
        "1.0.0",
        "beta-dist",
        "beta:MANIFEST",
        "beta-source",
        resolved_distributions=("shared_lib==2.0",),
    )

    with pytest.raises(KaryviaError) as caught:
        subject._validate_install_set((alpha, beta))

    assert caught.value.code is ErrorCode.PLUGIN_LOAD_FAILED
    assert caught.value.detail["distribution"] == "shared-lib"
    assert caught.value.detail["plugins"] == ["alpha", "beta"]


def test_install_set_accepts_the_same_resolved_version_across_plugin_roots() -> None:
    rows = tuple(
        subject.InstalledPlugin(
            plugin_id,
            "1.0.0",
            f"{plugin_id}-dist",
            f"{plugin_id}:MANIFEST",
            f"{plugin_id}-source",
            resolved_distributions=("shared-lib==1.0",),
        )
        for plugin_id in ("alpha", "beta")
    )

    subject._validate_install_set(rows)


def test_uninstall_refuses_to_break_an_installed_dependent(tmp_path: Path) -> None:
    home = subject.GlobalPluginHome.resolve(home=tmp_path, env={})
    home.write_catalog(
        (
            subject.InstalledPlugin(
                "alpha", "1.0.0", "alpha-dist", "alpha:MANIFEST", "alpha-source"
            ),
            subject.InstalledPlugin(
                "beta",
                "1.0.0",
                "beta-dist",
                "beta:MANIFEST",
                "beta-source",
                dependencies=("alpha",),
            ),
        )
    )
    home.package_dir("alpha").mkdir(parents=True)

    with pytest.raises(KaryviaError) as caught:
        subject.uninstall_plugin(home, "alpha")

    assert caught.value.detail["dependents"] == ["beta"]
    assert home.package_dir("alpha").is_dir()
    assert {item.plugin_id for item in home.catalog()} == {"alpha", "beta"}


def test_install_rejects_an_external_plugin_using_a_builtin_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = subject.GlobalPluginHome.resolve(home=tmp_path, env={})
    home.ensure()
    monkeypatch.setattr(subject, "_run_pip", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        subject,
        "_inspect_stage",
        lambda *args, **kwargs: subject.InstalledPlugin(
            "model-openai",
            "1.0.0",
            "external-model",
            "external:MANIFEST",
            "source",
        ),
    )

    with pytest.raises(KaryviaError) as caught:
        subject.install_plugin(home, "source")

    assert caught.value.code is ErrorCode.PLUGIN_REGISTRATION_CONFLICT
    assert home.catalog() == ()
