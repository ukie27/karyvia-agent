"""全局插件安装目录与实例登记。

职责：定义 ``~/.nucleamind/`` 下由插件管理器拥有的路径，持久化已安装插件目录与已知
实例目录，并在全局插件变更前确认没有实例正在运行。
不负责：解析插件 manifest、决定实例启用哪些插件、执行插件代码。

全局数据直接放在 NucleaMind home 下，不增加一层 ``global/``。插件代码与实例状态分开：
前者在 ``plugin-packages/``，后者仍在各实例的 ``plugins/`` 目录。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.metadata import distributions
from pathlib import Path
from typing import Final, cast

from nucleamind.contracts import ErrorCode, JsonValue, NucleaError, validate_identifier
from nucleamind.kernel.config import NUCLEAMIND_HOME_ENV, InstanceLock
from nucleamind.kernel.plugins import ENTRY_POINT_GROUP, PluginCandidate, read_candidate
from nucleamind.sdk import PluginManifest, parse_manifest

__all__ = [
    "NUCLEAMIND_HOME_ENV",
    "GlobalPluginHome",
    "InstalledPlugin",
    "install_plugin",
    "uninstall_plugin",
    "update_plugin",
]

_HOME_DIRNAME: Final = ".nucleamind"
_CATALOG_VERSION: Final = 1


@dataclass(frozen=True, slots=True)
class InstalledPlugin:
    """一份由 ``nm`` 管理的插件安装记录。

    ``backend`` 是安装与执行形态的显式判别，不让目录格式暗中等同于 Python。当前只实现
    ``python``；以后增加进程外宿主时可新增分支，而不必改变实例的启用语义。
    """

    plugin_id: str
    version: str
    distribution: str
    entry_point: str
    source: str
    backend: str = "python"
    with_dependencies: bool = True

    @classmethod
    def from_json(cls, raw: object) -> InstalledPlugin:
        if not isinstance(raw, Mapping):
            raise _catalog_error("插件安装记录必须是对象。")
        data = cast("Mapping[object, object]", raw)
        fields = {
            name: data.get(name)
            for name in (
                "plugin_id",
                "version",
                "distribution",
                "entry_point",
                "source",
                "backend",
            )
        }
        if not all(isinstance(value, str) and value for value in fields.values()):
            raise _catalog_error("插件安装记录缺少必需的字符串字段。")
        with_dependencies = data.get("with_dependencies")
        if not isinstance(with_dependencies, bool):
            raise _catalog_error(  # noqa: TRY003 - 损坏安装记录的稳定诊断。
                "插件安装记录缺少 with_dependencies 布尔字段。"
            )
        return cls(**cast("dict[str, str]", fields), with_dependencies=with_dependencies)

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "plugin_id": self.plugin_id,
            "version": self.version,
            "distribution": self.distribution,
            "entry_point": self.entry_point,
            "source": self.source,
            "backend": self.backend,
            "with_dependencies": self.with_dependencies,
        }


@dataclass(frozen=True, slots=True)
class GlobalPluginHome:
    """NucleaMind home 下的全局插件路径。"""

    root: Path

    @classmethod
    def resolve(
        cls,
        *,
        env: Mapping[str, str] | None = None,
        home: Path | None = None,
    ) -> GlobalPluginHome:
        environ = os.environ if env is None else env
        explicit = environ.get(NUCLEAMIND_HOME_ENV) if home is None else None
        if explicit is not None:
            return cls(Path(explicit).expanduser().resolve())
        base = Path.home() if home is None else Path(home)
        return cls((base / _HOME_DIRNAME).expanduser().resolve())

    @property
    def packages_dir(self) -> Path:
        return self.root / "plugin-packages"

    @property
    def catalog_path(self) -> Path:
        return self.root / "plugins.json"

    @property
    def instances_path(self) -> Path:
        return self.root / "instances.json"

    @property
    def manager_lock_path(self) -> Path:
        return self.root / "plugin-manager.lock"

    def package_dir(self, plugin_id: str) -> Path:
        validate_identifier("plugin", plugin_id)
        if plugin_id in {".", ".."} or "/" in plugin_id or "\\" in plugin_id:
            raise NucleaError(
                ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED,
                "插件 id 不能用作安全的安装目录名。",
                detail={"plugin_id": plugin_id},
            )
        return self.packages_dir / plugin_id

    def ensure(self) -> None:
        try:
            self.packages_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise NucleaError(
                ErrorCode.PERSISTENCE_WRITE_FAILED,
                "无法创建全局插件目录。",
                detail={"path": str(self.packages_dir), "errno": exc.errno},
            ) from exc

    def catalog(self) -> tuple[InstalledPlugin, ...]:
        if not self.catalog_path.exists():
            return ()
        raw = _read_json(self.catalog_path)
        if not isinstance(raw, Mapping):
            raise _catalog_error("插件安装目录版本不受支持。", path=self.catalog_path)
        document = cast("Mapping[str, JsonValue]", raw)
        if document.get("version") != _CATALOG_VERSION:
            raise _catalog_error("插件安装目录版本不受支持。", path=self.catalog_path)
        rows = document.get("plugins")
        if not isinstance(rows, list):
            raise _catalog_error(  # noqa: TRY003 - 目录损坏的稳定用户诊断。
                "插件安装目录的 plugins 必须是数组。", path=self.catalog_path
            )
        installed = tuple(
            InstalledPlugin.from_json(item) for item in cast("list[object]", rows)
        )
        ids = [item.plugin_id for item in installed]
        if len(ids) != len(set(ids)):
            raise _catalog_error(  # noqa: TRY003 - 目录损坏的稳定用户诊断。
                "插件安装目录含有重复 id。", path=self.catalog_path
            )
        return tuple(sorted(installed, key=lambda item: item.plugin_id))

    def write_catalog(self, installed: Sequence[InstalledPlugin]) -> None:
        self.ensure()
        _write_json(
            self.catalog_path,
            {
                "version": _CATALOG_VERSION,
                "plugins": [item.to_json() for item in sorted(installed, key=lambda x: x.plugin_id)],
            },
        )

    def entry_points(self) -> tuple[tuple[str, str], ...]:
        """返回受 ``nm`` 管理的候选，并把其私有安装根加入模块搜索路径。

        这里只加入字符串路径，不执行 ``.pth``，也不导入模块；未启用插件仍然没有代码
        执行。真正读取 manifest 仍受 ``plugins.enabled`` 控制。其他后端以后由各自的执行桥
        接入，不能把它们误当成 Python 路径。
        """
        rows = tuple(item for item in self.catalog() if item.backend == "python")
        for item in reversed(rows):
            root = str(self.package_dir(item.plugin_id))
            if root not in sys.path:
                sys.path.insert(0, root)
        return tuple((item.plugin_id, item.entry_point) for item in rows)

    @contextmanager
    def mutation(self) -> Generator[None, None, None]:
        """串行化全局插件变更，并拒绝在任一实例运行时改变代码。"""
        self.ensure()
        lock = InstanceLock(self.manager_lock_path).acquire()
        try:
            running = self.running_instances()
            if running:
                raise NucleaError(
                    ErrorCode.CONFIG_INSTANCE_LOCKED,
                    "有实例正在运行，不能修改全局插件。请先停止所有实例。",
                    detail={"instances": [str(path) for path in running]},
                )
            yield
        finally:
            lock.release()

    @contextmanager
    def registration(self) -> Generator[None, None, None]:
        """启动期短暂占用管理锁，避免与安装、更新、卸载交错。"""
        self.ensure()
        lock = InstanceLock(self.manager_lock_path).acquire()
        try:
            yield
        finally:
            lock.release()

    def register_instance(self, instance_dir: Path) -> None:
        roots = {path.resolve() for path in self.instances()}
        roots.add(instance_dir.resolve())
        _write_json(self.instances_path, {"version": 1, "instances": [str(p) for p in sorted(roots)]})

    def instances(self) -> tuple[Path, ...]:
        registered: set[Path] = set()
        if self.instances_path.exists():
            raw = _read_json(self.instances_path)
            if not isinstance(raw, Mapping):
                raise _catalog_error("实例目录索引版本不受支持。", path=self.instances_path)
            document = cast("Mapping[str, JsonValue]", raw)
            if document.get("version") != 1:
                raise _catalog_error("实例目录索引版本不受支持。", path=self.instances_path)
            rows = document.get("instances")
            if not isinstance(rows, list) or not all(isinstance(item, str) for item in rows):
                raise _catalog_error("实例目录索引格式错误。", path=self.instances_path)
            registered.update(Path(item).resolve() for item in cast("list[str]", rows))
        if self.root.is_dir():
            for child in self.root.iterdir():
                if child.is_dir() and (child / "config.json").is_file():
                    registered.add(child.resolve())
        return tuple(sorted(registered))

    def running_instances(self) -> tuple[Path, ...]:
        running: list[Path] = []
        for root in self.instances():
            path = root / "instance.lock"
            if not path.exists():
                continue
            probe = InstanceLock(path)
            try:
                probe.acquire()
            except NucleaError:
                running.append(root)
            else:
                probe.release()
        return tuple(running)


def install_plugin(
    home: GlobalPluginHome,
    source: str,
    *,
    upgrade: bool = False,
    expected_id: str | None = None,
    with_dependencies: bool = True,
) -> InstalledPlugin:
    """把一个 Python 插件安装到管理器拥有的独立目录，并更新全局目录。"""
    source = _normalize_source(source)
    installed = list(home.catalog())
    stage = Path(tempfile.mkdtemp(prefix=".plugin-install-", dir=home.packages_dir))
    try:
        _run_pip(
            source,
            stage,
            upgrade=upgrade,
            with_dependencies=with_dependencies,
        )
        record = _inspect_stage(stage, source, with_dependencies=with_dependencies)
        if expected_id is not None and record.plugin_id != expected_id:
            raise NucleaError(
                ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED,
                "更新后的插件 id 与原安装记录不一致。",
                detail={"expected": expected_id, "actual": record.plugin_id},
            )
        existing = next((item for item in installed if item.plugin_id == record.plugin_id), None)
        if existing is not None and not upgrade:
            raise NucleaError(
                ErrorCode.PLUGIN_REGISTRATION_CONFLICT,
                "插件已经全局安装；请使用 nm plugins update。",
                detail={"plugin_id": record.plugin_id},
            )
        target = home.package_dir(record.plugin_id)
        backup = target.with_name(f".{target.name}.backup")
        if backup.exists():
            shutil.rmtree(backup)
        if target.exists():
            target.replace(backup)
        try:
            stage.replace(target)
            rows = [item for item in installed if item.plugin_id != record.plugin_id]
            home.write_catalog((*rows, record))
        except BaseException:
            if target.exists():
                shutil.rmtree(target)
            if backup.exists():
                backup.replace(target)
            raise
        if backup.exists():
            shutil.rmtree(backup)
        return record
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def update_plugin(home: GlobalPluginHome, plugin_id: str) -> InstalledPlugin:
    record = next((item for item in home.catalog() if item.plugin_id == plugin_id), None)
    if record is None:
        raise NucleaError(
            ErrorCode.PLUGIN_LOAD_FAILED,
            "插件尚未全局安装。",
            detail={"plugin_id": plugin_id},
        )
    if record.backend != "python":
        raise NucleaError(
            ErrorCode.PLUGIN_LOAD_FAILED,
            "当前 Runtime 不支持更新这种插件安装后端。",
            detail={"plugin_id": plugin_id, "backend": record.backend},
        )
    return install_plugin(
        home,
        record.source,
        upgrade=True,
        expected_id=plugin_id,
        with_dependencies=record.with_dependencies,
    )


def uninstall_plugin(home: GlobalPluginHome, plugin_id: str) -> InstalledPlugin:
    installed = list(home.catalog())
    record = next((item for item in installed if item.plugin_id == plugin_id), None)
    if record is None:
        raise NucleaError(
            ErrorCode.PLUGIN_LOAD_FAILED,
            "插件尚未全局安装。",
            detail={"plugin_id": plugin_id},
        )
    target = home.package_dir(plugin_id)
    backup = target.with_name(f".{target.name}.uninstalling")
    if backup.exists():
        shutil.rmtree(backup)
    if target.exists():
        target.replace(backup)
    try:
        home.write_catalog([item for item in installed if item.plugin_id != plugin_id])
    except BaseException:
        if backup.exists():
            backup.replace(target)
        raise
    if backup.exists():
        shutil.rmtree(backup)
    return record


def _run_pip(
    source: str, target: Path, *, upgrade: bool, with_dependencies: bool
) -> None:
    command = _python_installer_command(target)
    if upgrade:
        command.append("--upgrade")
    if not with_dependencies:
        command.append("--no-deps")
    command.append(source)
    installer_env = dict(os.environ)
    installer_env.setdefault("UV_CACHE_DIR", str(target.parent.parent / "plugin-cache"))
    result = subprocess.run(  # noqa: S603
        command,
        check=False,
        capture_output=True,
        text=True,
        env=installer_env,
    )
    if result.returncode != 0:
        raise NucleaError(
            ErrorCode.PLUGIN_LOAD_FAILED,
            "插件安装失败。",
            detail={"installer": "python", "exit_code": result.returncode},
        )


def _normalize_source(source: str) -> str:
    """本地来源写成绝对路径，使以后 ``update`` 不依赖当时的工作目录。"""
    path = Path(source).expanduser()
    return str(path.resolve()) if path.exists() else source


def _python_installer_command(target: Path) -> list[str]:
    """选择当前环境可用的 Python 包安装前端；用户始终只调用 ``nm``。"""
    if importlib.util.find_spec("pip") is not None:
        return [sys.executable, "-m", "pip", "install", "--target", str(target)]
    uv = shutil.which("uv")
    if uv is not None:
        return [uv, "pip", "install", "--python", sys.executable, "--target", str(target)]
    raise NucleaError(
        ErrorCode.PLUGIN_LOAD_FAILED,
        "找不到可用的 Python 插件安装后端；请安装 pip 或 uv。",
        detail={"installer": "python"},
    )


def _inspect_stage(
    stage: Path, source: str, *, with_dependencies: bool
) -> InstalledPlugin:
    matches: list[tuple[str, str, str, str]] = []
    for distribution in distributions(path=[str(stage)]):
        name = str(distribution.metadata["Name"])
        version = distribution.version or ""
        for point in distribution.entry_points:
            if point.group == ENTRY_POINT_GROUP:
                matches.append((point.name, point.value, name, version))
    if len(matches) != 1:
        raise NucleaError(
            ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED,
            "一个安装源必须恰好提供一个 nucleamind.plugins entry point。",
            detail={"entry_points": len(matches)},
        )
    plugin_id, entry_point, distribution, version = matches[0]
    validate_identifier("plugin", plugin_id)
    root = str(stage)
    sys.path.insert(0, root)
    try:
        candidate = PluginCandidate(
            plugin_id=plugin_id,
            location=entry_point,
        )
        raw = read_candidate(candidate)
        if isinstance(raw, PluginManifest):
            manifest = raw
        elif isinstance(raw, Mapping):
            manifest = parse_manifest(
                cast("Mapping[str, object]", raw), origin=candidate.origin
            )
        else:
            raise NucleaError(
                ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED,
                "插件 entry point 没有提供合法 manifest。",
                detail={"plugin_id": plugin_id, "type": type(raw).__name__},
            )
        if manifest.id != plugin_id:
            raise NucleaError(
                ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED,
                "manifest id 与插件 entry point 名不一致。",
                detail={"plugin_id": plugin_id, "manifest_id": manifest.id},
            )
        if not manifest.sdk_compatible or not manifest.matches_platform():
            raise NucleaError(
                ErrorCode.PLUGIN_SDK_INCOMPATIBLE,
                "插件与当前 SDK 或平台不兼容。",
                detail={"plugin_id": plugin_id, "sdk_range": manifest.sdk_range},
            )
    finally:
        sys.path.remove(root)
    return InstalledPlugin(
        plugin_id=plugin_id,
        version=manifest.version or version,
        distribution=distribution,
        entry_point=entry_point,
        source=source,
        backend="python",
        with_dependencies=with_dependencies,
    )


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _catalog_error("无法读取全局插件数据。", path=path) from exc


def _write_json(path: Path, payload: Mapping[str, JsonValue]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, path)
    except OSError as exc:
        try:
            temp.unlink()
        except OSError:
            pass
        raise NucleaError(
            ErrorCode.PERSISTENCE_WRITE_FAILED,
            "无法写入全局插件数据。",
            detail={"path": str(path), "errno": exc.errno},
        ) from exc


def _catalog_error(message: str, *, path: Path | None = None) -> NucleaError:
    detail: dict[str, JsonValue] = {}
    if path is not None:
        detail["path"] = str(path)
    return NucleaError(ErrorCode.CONFIG_INVALID, message, detail=detail)
