"""配置加载的编排。

职责：把「定位实例目录 -> 建目录 -> 收集三层 -> 合并 -> 校验 -> 解析 workspace 绝对路径」
串成一条 `load_config()`，产出一个 `LoadedConfig`。
不负责：获取实例锁（`lock.py`，由 `runtime/bootstrap.py` 在合适的生命周期点调用）、
构造 Registry 或加载插件、发布事件。

**锁不在这里获取。** 加载配置是个纯读操作，可以在任何地方安全调用（`karyvia config show`、
诊断、测试）；获取实例锁会改变全局状态且必须有配对的释放。把两者绑在一起，就等于
`karyvia config show` 也要抢锁，而且 loader 得替调用方决定什么时候释放。

顺序不可交换：workspace 的绝对路径必须在校验**之后**解析（要先确认 `workspace.root`
是个字符串），而实例目录必须在读文件**之前**定位（`config.json` 在它里面）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Mapping

from ...contracts import ErrorCode, KaryviaError
from .layout import InstanceLayout
from .merge import MergeResult, merge_layers
from .schema import KaryviaConfig, validate_config
from .sources import collect_layers

if TYPE_CHECKING:
    from ...contracts import JsonValue
    from ..turn.limits import TurnLimits

__all__ = ["LoadedConfig", "load_config"]


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    """一次加载的完整结果：配置本体、它落在哪、每个值从哪来。"""

    config: KaryviaConfig
    layout: InstanceLayout
    #: workspace 的**生效**绝对路径（已应用 `workspace.root` 覆盖）。
    workspace_root: Path
    #: 来源索引，供 `karyvia config show --origins` 回答「这个值是谁设的」。
    merge: MergeResult

    @property
    def limits(self) -> TurnLimits:
        """turn 小节转成 engine 要的 `TurnLimits`。"""
        return self.config.turn.to_limits()

    def origin_of(self, *path: str) -> str | None:
        """某字段的来源名；取自默认值时为 `None`。"""
        return self.merge.origin_of(*path)

    def to_json(self) -> dict[str, JsonValue]:
        """诊断视图（`karyvia config show` / `karyvia doctor`）。

        每个小节自己负责把元组转成列表，因此这个视图一定能被 `json.dumps` 编码——
        它的唯一用途就是被打印或写盘。
        """
        return {
            "config": self.config.to_json(),
            "layout": self.layout.to_json(),
            "workspace_root": str(self.workspace_root),
            "origins": dict(sorted(self.merge.origins.items())),
        }


def _resolve_workspace(config: KaryviaConfig, layout: InstanceLayout) -> Path:
    """定出 workspace 的绝对路径。

    相对路径按**实例目录**解析，不按进程 cwd：同一份配置在不同目录下启动必须指向同一个
    workspace，否则从哪个目录跑 `karyvia` 会改变 agent 能看见的文件。
    """
    raw = config.workspace.root
    if raw is None:
        return layout.workspace_dir
    if not raw.strip():
        raise KaryviaError(
            ErrorCode.CONFIG_INVALID,
            "workspace.root 不能是空字符串。",
            detail={
                "pointer": "/workspace/root",
                "suggestion": "删除该键以使用实例目录下的 workspace/，或填一个有效路径。",
            },
        )
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = layout.root / candidate
    return candidate.resolve()


def load_config(
    *,
    instance_dir: Path | str | None = None,
    instance: str | None = None,
    env: Mapping[str, str] | None = None,
    overrides: Iterable[str] | None = None,
    home: Path | None = None,
    ensure_dirs: bool = True,
) -> LoadedConfig:
    """加载一个实例的配置。

    `ensure_dirs=False` 用于只读诊断（`karyvia doctor` 检查一个不存在的实例时不该顺手建目录）。
    默认建目录：正常启动路径需要它们存在，而 `ensure()` 只建目录、不写文件。
    """
    layout = InstanceLayout.resolve(
        instance_dir=instance_dir,
        instance=instance,
        env=env,
        home=home,
    )
    if ensure_dirs:
        layout.ensure()

    merged = merge_layers(collect_layers(layout.config_path, env=env, overrides=overrides))
    config = validate_config(merged.data)
    workspace_root = _resolve_workspace(config, layout)

    return LoadedConfig(
        config=config,
        layout=layout,
        workspace_root=workspace_root,
        merge=merged,
    )
