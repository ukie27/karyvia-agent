"""全局插件安装目录与实例登记。

职责：定义 ``~/.karyvia/`` 下由插件管理器拥有的路径，持久化已安装插件、其逻辑依赖
与已解析 Python 发行包集合，并在全局插件变更前确认没有实例正在运行。
不负责：解析插件 manifest、决定实例启用哪些插件、执行插件代码。

全局数据直接放在 Karyvia home 下，不增加一层 ``global/``。插件代码与实例状态分开：
前者在 ``plugin-packages/``，命名实例统一在 ``instances/<name>/``，插件业务状态仍在各
实例的 ``plugins/`` 目录。显式外部实例通过 ``instances.json`` 纳入全局停机检查。
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

from packaging.utils import canonicalize_name

from karyvia.builtins.registry import BUILTIN_MANIFESTS
from karyvia.contracts import ErrorCode, JsonValue, KaryviaError, validate_identifier
from karyvia.kernel.config import INSTANCES_DIRNAME, KARYVIA_HOME_ENV, InstanceLock
from karyvia.kernel.plugins import (
    ENTRY_POINT_GROUP,
    PlanNode,
    PluginCandidate,
    plan_load_order,
    read_candidate,
)
from karyvia.sdk import PluginManifest, parse_manifest

__all__ = [
    "KARYVIA_HOME_ENV",
    "GlobalPluginHome",
    "InstalledPlugin",
    "install_plugin",
    "uninstall_plugin",
    "validate_uninstall",
    "update_plugin",
]

_HOME_DIRNAME: Final = ".karyvia"
_CATALOG_VERSION: Final = 2
_BUILTIN_PLUGIN_IDS: Final = frozenset(manifest.id for manifest in BUILTIN_MANIFESTS)


@dataclass(frozen=True, slots=True)
class InstalledPlugin:
    """一份由 ``karyvia`` 管理的插件安装记录。

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
    dependencies: tuple[str, ...] = ()
    resolved_distributions: tuple[str, ...] = ()

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
        dependencies = _string_tuple(data.get("dependencies"), field="dependencies")
        resolved = _string_tuple(
            data.get("resolved_distributions"), field="resolved_distributions"
        )
        if any(_split_pin(pin) is None for pin in resolved):
            raise _catalog_error("插件安装记录包含非法的发行包版本锁。")
        return cls(
            **cast("dict[str, str]", fields),
            with_dependencies=with_dependencies,
            dependencies=dependencies,
            resolved_distributions=resolved,
        )

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "plugin_id": self.plugin_id,
            "version": self.version,
            "distribution": self.distribution,
            "entry_point": self.entry_point,
            "source": self.source,
            "backend": self.backend,
            "with_dependencies": self.with_dependencies,
            "dependencies": list(self.dependencies),
            "resolved_distributions": list(self.resolved_distributions),
        }


@dataclass(frozen=True, slots=True)
class GlobalPluginHome:
    """Karyvia home 下的全局插件路径。"""

    root: Path

    @classmethod
    def resolve(
        cls,
        *,
        env: Mapping[str, str] | None = None,
        home: Path | None = None,
    ) -> GlobalPluginHome:
        environ = os.environ if env is None else env
        explicit = environ.get(KARYVIA_HOME_ENV) if home is None else None
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
    def named_instances_dir(self) -> Path:
        """由实例名定位的实例容器；显式 ``--instance-dir`` 不受它约束。"""
        return self.root / INSTANCES_DIRNAME

    @property
    def manager_lock_path(self) -> Path:
        return self.root / "plugin-manager.lock"

    def package_dir(self, plugin_id: str) -> Path:
        validate_identifier("plugin", plugin_id)
        if plugin_id in {".", ".."} or "/" in plugin_id or "\\" in plugin_id:
            raise KaryviaError(
                ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED,
                "插件 id 不能用作安全的安装目录名。",
                detail={"plugin_id": plugin_id},
            )
        return self.packages_dir / plugin_id

    def ensure(self) -> None:
        try:
            self.packages_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise KaryviaError(
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
        """返回受 ``karyvia`` 管理的候选，并把安装根加入模块搜索路径。

        这里只加入字符串路径，不执行 ``.pth``，也不导入模块；未启用插件仍然没有代码
        执行。真正读取 manifest 仍受 ``plugins.enabled`` 控制。其他后端以后由各自的执行桥
        接入，不能把它们误当成 Python 路径。默认安装记录了每个根中的完整发行包集合，并在
        发布前拒绝跨根版本冲突，因此这里的顺序不再决定第三方依赖版本。显式 ``--no-deps``
        的插件只含自身发行包，其余依赖由启动 ``karyvia`` 的 Python 环境负责。
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
                raise KaryviaError(
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
        if self.named_instances_dir.is_dir():
            for child in self.named_instances_dir.iterdir():
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
            except KaryviaError:
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
    """安装一个 Python 插件；候选集合验证通过后才原子发布目录与全局目录。"""
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
        if record.plugin_id in _BUILTIN_PLUGIN_IDS:
            raise KaryviaError(
                ErrorCode.PLUGIN_REGISTRATION_CONFLICT,
                "外部插件 id 不能与内建插件相同；请用 overrides 替换具体能力。",
                detail={"plugin_id": record.plugin_id},
            )
        if expected_id is not None and record.plugin_id != expected_id:
            raise KaryviaError(
                ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED,
                "更新后的插件 id 与原安装记录不一致。",
                detail={"expected": expected_id, "actual": record.plugin_id},
            )
        existing = next((item for item in installed if item.plugin_id == record.plugin_id), None)
        if existing is not None and not upgrade:
            raise KaryviaError(
                ErrorCode.PLUGIN_REGISTRATION_CONFLICT,
                "插件已经全局安装；请使用 karyvia plugins update。",
                detail={"plugin_id": record.plugin_id},
            )
        rows = [item for item in installed if item.plugin_id != record.plugin_id]
        _validate_install_set((*rows, record))
        target = home.package_dir(record.plugin_id)
        backup = target.with_name(f".{target.name}.backup")
        if backup.exists():
            shutil.rmtree(backup)
        if target.exists():
            target.replace(backup)
        try:
            stage.replace(target)
            home.write_catalog((*rows, record))
        except BaseException:
            if target.exists():
                target.replace(stage)
            if backup.exists():
                backup.replace(target)
            raise
        if backup.exists():
            # catalog 与目标目录已经一起提交；备份只是可回收垃圾，清理失败不能把成功的
            # 安装谎报成失败。下次同 id 变更会在发布前再次清理这个具名备份。
            shutil.rmtree(backup, ignore_errors=True)
        return record
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def update_plugin(home: GlobalPluginHome, plugin_id: str) -> InstalledPlugin:
    record = next((item for item in home.catalog() if item.plugin_id == plugin_id), None)
    if record is None:
        raise KaryviaError(
            ErrorCode.PLUGIN_LOAD_FAILED,
            "插件尚未全局安装。",
            detail={"plugin_id": plugin_id},
        )
    if record.backend != "python":
        raise KaryviaError(
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
    record = _require_uninstallable(installed, plugin_id)
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
        # 删除发生在逻辑提交之后；残留的隐藏备份不参与发现，下次变更会重新清理。
        shutil.rmtree(backup, ignore_errors=True)
    return record


def validate_uninstall(home: GlobalPluginHome, plugin_id: str) -> InstalledPlugin:
    """只读校验一次卸载，供跨实例配置事务在写任何文件前完成 preflight。"""
    return _require_uninstallable(home.catalog(), plugin_id)


def _require_uninstallable(
    installed: Sequence[InstalledPlugin], plugin_id: str
) -> InstalledPlugin:
    record = next((item for item in installed if item.plugin_id == plugin_id), None)
    if record is None:
        raise KaryviaError(
            ErrorCode.PLUGIN_LOAD_FAILED,
            "插件尚未全局安装。",
            detail={"plugin_id": plugin_id},
        )
    dependents = sorted(
        item.plugin_id for item in installed if plugin_id in item.dependencies
    )
    if dependents:
        raise KaryviaError(
            ErrorCode.PLUGIN_LOAD_FAILED,
            "插件仍被其他已安装插件依赖，不能卸载。",
            detail={"plugin_id": plugin_id, "dependents": dependents},
        )
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
        raise KaryviaError(
            ErrorCode.PLUGIN_LOAD_FAILED,
            "插件安装失败。",
            detail={"installer": "python", "exit_code": result.returncode},
        )


def _normalize_source(source: str) -> str:
    """本地来源写成绝对路径，使以后 ``update`` 不依赖当时的工作目录。"""
    path = Path(source).expanduser()
    return str(path.resolve()) if path.exists() else source


def _python_installer_command(target: Path) -> list[str]:
    """选择当前环境可用的 Python 包安装前端；用户始终只调用 ``karyvia``。"""
    if importlib.util.find_spec("pip") is not None:
        return [sys.executable, "-m", "pip", "install", "--target", str(target)]
    uv = shutil.which("uv")
    if uv is not None:
        return [uv, "pip", "install", "--python", sys.executable, "--target", str(target)]
    raise KaryviaError(
        ErrorCode.PLUGIN_LOAD_FAILED,
        "找不到可用的 Python 插件安装后端；请安装 pip 或 uv。",
        detail={"installer": "python"},
    )


def _inspect_stage(
    stage: Path, source: str, *, with_dependencies: bool
) -> InstalledPlugin:
    matches: list[tuple[str, str, str, str]] = []
    found_distributions = tuple(distributions(path=[str(stage)]))
    resolved: list[str] = []
    for distribution in found_distributions:
        name = str(distribution.metadata["Name"])
        version = distribution.version or ""
        if not name or not version:
            raise KaryviaError(
                ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED,
                "安装结果包含缺少名称或版本的 Python 发行包。",
            )
        resolved.append(f"{canonicalize_name(name)}=={version}")
        for point in distribution.entry_points:
            if point.group == ENTRY_POINT_GROUP:
                matches.append((point.name, point.value, name, version))
    if len(matches) != 1:
        raise KaryviaError(
            ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED,
            "一个安装源必须恰好提供一个 karyvia.plugins entry point。",
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
            raise KaryviaError(
                ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED,
                "插件 entry point 没有提供合法 manifest。",
                detail={"plugin_id": plugin_id, "type": type(raw).__name__},
            )
        if manifest.id != plugin_id:
            raise KaryviaError(
                ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED,
                "manifest id 与插件 entry point 名不一致。",
                detail={"plugin_id": plugin_id, "manifest_id": manifest.id},
            )
        if not manifest.sdk_compatible or not manifest.matches_platform():
            raise KaryviaError(
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
        dependencies=manifest.dependencies,
        resolved_distributions=tuple(sorted(set(resolved))),
    )


def _validate_install_set(installed: Sequence[InstalledPlugin]) -> None:
    """验证全局逻辑依赖图和同进程 Python 发行包集合。

    每个插件仍可独立替换代码目录，但这些目录最终进入同一个解释器。因此它们不是隔离环境：
    同名发行包必须解析到同一版本。把这个事实变成安装期硬约束，避免启动时由 ``sys.path``
    顺序偶然决定版本。
    """
    by_id = {item.plugin_id: item for item in installed}
    if len(by_id) != len(installed):
        raise KaryviaError(
            ErrorCode.PLUGIN_REGISTRATION_CONFLICT,
            "全局安装集合含有重复插件 id。",
        )

    plan = plan_load_order(
        [
            PlanNode(plugin_id=item.plugin_id, dependencies=item.dependencies)
            for item in installed
        ],
        provided=_BUILTIN_PLUGIN_IDS,
    )
    if plan.failures:
        failure = plan.failures[0]
        failure_detail = dict(failure.error.detail)
        # Kernel 的阶段 A 诊断面向“实例启用”；安装期只要求全局在场，不能让那条建议误导
        # 用户顺手修改实例配置。
        failure_detail.pop("suggestion", None)
        if "missing" in failure_detail:
            failure_detail["suggestion"] = "先全局安装缺少的依赖插件，再重试当前操作。"
        raise KaryviaError(
            ErrorCode.PLUGIN_LOAD_FAILED,
            "全局插件依赖图无效，安装没有发布。",
            detail={
                **failure_detail,
                "plugin_id": failure.plugin_id,
                "cause": failure.error.code.value,
            },
        )

    owners: dict[str, tuple[str, str]] = {}
    for item in sorted(installed, key=lambda candidate: candidate.plugin_id):
        if item.backend != "python":
            continue
        for pin in item.resolved_distributions:
            parsed = _split_pin(pin)
            if parsed is None:
                raise _catalog_error("插件安装记录包含非法的发行包版本锁。")
            name, version = parsed
            previous = owners.get(name)
            if previous is not None and previous[0] != version:
                raise KaryviaError(
                    ErrorCode.PLUGIN_LOAD_FAILED,
                    "Python 插件依赖版本冲突，安装没有发布。",
                    detail={
                        "distribution": name,
                        "requested_versions": sorted({previous[0], version}),
                        "plugins": sorted({previous[1], item.plugin_id}),
                    },
                )
            owners[name] = (version, item.plugin_id)


def _split_pin(pin: str) -> tuple[str, str] | None:
    name, separator, version = pin.partition("==")
    if not separator or not name or not version or "==" in version:
        return None
    return (canonicalize_name(name), version)


def _string_tuple(raw: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise _catalog_error(  # noqa: TRY003 - 损坏目录需要指出具体字段。
            f"插件安装记录缺少 {field} 字符串数组。"
        )
    values: list[str] = []
    for item in cast("list[object]", raw):
        if not isinstance(item, str):
            raise _catalog_error(  # noqa: TRY003 - 损坏目录需要指出具体字段。
                f"插件安装记录的 {field} 必须是字符串数组。"
            )
        values.append(item)
    return tuple(values)


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
        raise KaryviaError(
            ErrorCode.PERSISTENCE_WRITE_FAILED,
            "无法写入全局插件数据。",
            detail={"path": str(path), "errno": exc.errno},
        ) from exc


def _catalog_error(message: str, *, path: Path | None = None) -> KaryviaError:
    detail: dict[str, JsonValue] = {}
    if path is not None:
        detail["path"] = str(path)
    return KaryviaError(ErrorCode.CONFIG_INVALID, message, detail=detail)
