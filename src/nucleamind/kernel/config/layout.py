"""实例目录的路径代数（技术方案 §11、§10.1 步骤 1）。

职责：把「实例在哪」解析成一个 `InstanceLayout`，并给出实例目录内每个 Kernel 拥有的
子路径；`ensure()` 创建那些目录。
不负责：读写目录内的任何文件（配置在 `sources.py`、锁在 `lock.py`）、解析配置、
决定 workspace 的**生效**位置（配置可覆盖，见 `loader.LoadedConfig.workspace_root`）。

布局在配置之前解析（§10.1 的步骤 1 先于步骤 2）：要先知道 `config.json` 在哪才能读它。
因此配置里的 `workspace.root` 只能改 workspace，永远改不了实例目录本身——否则会出现
「读完配置才知道该去哪读配置」的循环。

`env` 与 `home` 都可注入，测试不 monkeypatch 全局状态——与
`contracts.PluginManifest.matches_platform(platform=...)` 同一个习语。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

from ...contracts import ErrorCode, NucleaError, PluginId, validate_identifier

if TYPE_CHECKING:
    from ...contracts import JsonValue

__all__ = [
    "CONFIG_FILENAME",
    "DEFAULT_INSTANCE_NAME",
    "INSTANCE_DIR_ENV",
    "INSTANCE_NAME_ENV",
    "NUCLEAMIND_HOME_ENV",
    "LOCK_FILENAME",
    "LOGS_DIRNAME",
    "PLUGINS_DIRNAME",
    "SESSIONS_DIRNAME",
    "WORKSPACE_DIRNAME",
    "InstanceLayout",
]

#: 未指定实例名时的默认值（§11）。
DEFAULT_INSTANCE_NAME = "default"

#: 直接指定实例目录。优先级最高，跳过 `~/.nucleamind/<name>` 的推导。
INSTANCE_DIR_ENV = "NUCLEAMIND_INSTANCE_DIR"

#: 只指定实例名，目录仍落在 `~/.nucleamind/` 下。
INSTANCE_NAME_ENV = "NUCLEAMIND_INSTANCE"

#: NucleaMind 的数据根。与 ``HOME`` 不同，它直接指向 ``.nucleamind`` 这一层。
NUCLEAMIND_HOME_ENV = "NUCLEAMIND_HOME"

#: 家目录下的容器目录名（`legacy/` 用的是 `.nanobot`，新层不双读，见 AGENTS.md）。
HOME_DIRNAME = ".nucleamind"

#: 实例名长度上限。远小于 `contracts` 的通用标识上限：实例名会成为一段**路径分量**，
#: 而它下面还要接 `sessions/<storage_id>.json`。Windows 默认 MAX_PATH 是 260，一个几百
#: 字符的合法标识会让会话写入在运行期才失败。
MAX_INSTANCE_NAME_LENGTH = 64

CONFIG_FILENAME = "config.json"
LOCK_FILENAME = "instance.lock"

SESSIONS_DIRNAME = "sessions"
PLUGINS_DIRNAME = "plugins"
LOGS_DIRNAME = "logs"
WORKSPACE_DIRNAME = "workspace"

#: `ensure()` 创建的目录，全部由 Kernel 拥有。workspace 也在内：它的默认位置在实例目录
#: 里，缺了它第一次文件工具调用就会失败。
_OWNED_DIRNAMES: tuple[str, ...] = (
    SESSIONS_DIRNAME,
    PLUGINS_DIRNAME,
    LOGS_DIRNAME,
    WORKSPACE_DIRNAME,
)

# 这些名字由 Runtime 的全局插件管理器直接放在 ``~/.nucleamind/`` 下。实例仍与它们共享
# 一个父目录，因此必须在路径拼接前拒绝同名实例，而不能等 ``mkdir`` 以模糊的 IO 错误失败。
_GLOBAL_PLUGIN_NAMES = frozenset(
    {"plugin-packages", "plugin-cache", "plugins.json", "instances.json", "plugin-manager.lock"}
)


def _validate_instance_name(name: str) -> str:
    """校验实例名可安全地拼成一个目录名。

    先走 `validate_identifier`（非空、不超长、无控制字符），再挡住路径分量特有的三种形状。
    `..` 能逃出 `~/.nucleamind/`，这是本模块唯一的安全问题——不能只靠通用标识校验。
    """
    validate_identifier("instance", name, max_length=MAX_INSTANCE_NAME_LENGTH)
    if name in _GLOBAL_PLUGIN_NAMES:
        raise NucleaError(
            ErrorCode.CONFIG_INVALID,
            "实例名与 NucleaMind 的全局目录名冲突。",
            detail={"instance": name},
        )
    if name in {".", ".."} or name.strip() != name:
        raise NucleaError(
            ErrorCode.CONFIG_INVALID,
            "实例名不能是 . 或 ..，也不能带首尾空白。",
            detail={"instance": name},
        )
    separators = {sep for sep in (os.sep, os.altsep, "/", "\\") if sep}
    if any(sep in name for sep in separators):
        raise NucleaError(
            ErrorCode.CONFIG_INVALID,
            "实例名不能包含路径分隔符。",
            detail={"instance": name},
        )
    return name


@dataclass(frozen=True, slots=True)
class InstanceLayout:
    """一个实例目录的全部路径。构造后不做 IO，直到调用 `ensure()`。"""

    root: Path

    @classmethod
    def resolve(
        cls,
        *,
        instance_dir: Path | str | None = None,
        instance: str | None = None,
        env: Mapping[str, str] | None = None,
        home: Path | None = None,
    ) -> InstanceLayout:
        """按「显式目录 > 显式实例名 > `NUCLEAMIND_INSTANCE_DIR` > `NUCLEAMIND_INSTANCE`
        > `default`」定位。

        显式参数来自 `nm --instance-dir` / `--instance`，**两者都压过环境变量**：和配置的
        四层一样，命令行永远赢过环境（`sources.collect_layers()` 同序）。否则 shell 里一个
        导出过的 `NUCLEAMIND_INSTANCE_DIR` 会静默吃掉 `nm --instance work`。

        实例名即使不参与推导也会被校验——一个非法的名字应当报错，而不是静默失效。
        """
        environ = os.environ if env is None else env
        name = _validate_instance_name(
            instance if instance is not None else environ.get(INSTANCE_NAME_ENV) or DEFAULT_INSTANCE_NAME
        )

        if instance_dir is not None:
            return cls(root=Path(instance_dir).expanduser().resolve())

        base = Path.home() if home is None else Path(home)
        data_root = (
            Path(environ[NUCLEAMIND_HOME_ENV]).expanduser().resolve()
            if home is None and environ.get(NUCLEAMIND_HOME_ENV)
            else (base / HOME_DIRNAME).expanduser().resolve()
        )
        if instance is not None:
            return cls(root=data_root / name)

        env_dir = environ.get(INSTANCE_DIR_ENV) or None
        if env_dir is not None:
            return cls(root=Path(env_dir).expanduser().resolve())

        return cls(root=data_root / name)

    @property
    def config_path(self) -> Path:
        """`config.json`。只由 `sources.read_config_file` 打开，且只读（`EDG-501`）。"""
        return self.root / CONFIG_FILENAME

    @property
    def lock_path(self) -> Path:
        """`instance.lock`。见 `lock.InstanceLock`。"""
        return self.root / LOCK_FILENAME

    @property
    def sessions_dir(self) -> Path:
        return self.root / SESSIONS_DIRNAME

    @property
    def plugins_dir(self) -> Path:
        return self.root / PLUGINS_DIRNAME

    @property
    def logs_dir(self) -> Path:
        return self.root / LOGS_DIRNAME

    @property
    def workspace_dir(self) -> Path:
        """workspace 的**默认**位置。生效位置见 `LoadedConfig.workspace_root`。"""
        return self.root / WORKSPACE_DIRNAME

    def plugin_state_dir(self, plugin_id: PluginId) -> Path:
        """单个插件的私有状态目录。`ensure()` 不预建——插件加载时才知道有谁。"""
        return self.plugins_dir / _validate_instance_name(str(plugin_id))

    def session_paths(self, storage_id: str) -> tuple[Path, Path]:
        """会话的 `(历史 .jsonl, 元数据 .meta.json)`。

        `storage_id` 必须来自 `SessionKey.storage_id()`——那个编码已发布即为持久化契约，
        本模块只拼后缀，绝不自己编码会话标识。
        """
        _validate_instance_name(storage_id)
        return (
            self.sessions_dir / f"{storage_id}.jsonl",
            self.sessions_dir / f"{storage_id}.meta.json",
        )

    def events_log_path(self, day: date) -> Path:
        """按天分片的事件日志。`D12` 的文件 sink 写它，`D10` 只给路径。"""
        return self.logs_dir / f"events-{day.isoformat()}.jsonl"

    def config_error_log_path(self, day: date) -> Path:
        """配置解析错误的落点（§6.7 的 `EDG-501` 子句）。

        `D10` 只提供路径：写盘归 `D12` 的 sink 与 `D23` 的接线。让 loader 在自己的错误
        路径上做 IO 会多出一个失败面，而 `NucleaError.detail` 本身已可 JSON 序列化。
        """
        return self.logs_dir / f"config-errors-{day.isoformat()}.jsonl"

    def ensure(self) -> None:
        """创建实例目录与 Kernel 拥有的子目录。**绝不创建任何文件**。

        `config.json` 缺失不是错误（默认值本身完整合法），生成它是 `D24` 的事；
        锁文件由 `lock.py` 用 `O_EXCL` 创建，那是它的互斥机制本身。
        """
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            for dirname in _OWNED_DIRNAMES:
                (self.root / dirname).mkdir(exist_ok=True)
        except OSError as exc:
            raise NucleaError(
                ErrorCode.PERSISTENCE_WRITE_FAILED,
                "无法创建实例目录。",
                detail={"path": str(self.root), "errno": exc.errno},
            ) from exc

    def to_json(self) -> dict[str, JsonValue]:
        """诊断视图（`nm doctor` / `nm config show`）。"""
        return {
            "root": str(self.root),
            "config": str(self.config_path),
            "lock": str(self.lock_path),
            "sessions": str(self.sessions_dir),
            "plugins": str(self.plugins_dir),
            "logs": str(self.logs_dir),
            "workspace": str(self.workspace_dir),
        }
