"""诊断的三个只读查询：能力、插件、单个 turn。

职责：把「谁提供了这项能力」「这个插件现在什么状态」「这个 turn 发生了什么」三个问题
收敛到一份实现上，供 `karyvia capabilities` / `karyvia plugins`与会话内 `/plugins` 共用。
不负责：产生这些数据（能力来自 `kernel/registry`，插件状态来自 `kernel/plugins`
，事件来自 `MemoryRingSink`）、修改任何状态、格式化终端输出。

`karyvia plugins enable` 与会话内 `/plugins` 是两个刻意分开的 surface：
前者改配置、后者只读查询，但**读的是这一份实现**，不各写一套。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from ...contracts import KaryviaError, PluginId, RuntimeEvent, TurnId
from .redaction import error_to_json
from .sinks import MemoryRingSink

if TYPE_CHECKING:  # pragma: no cover - 仅为注解。
    from ...contracts import JsonValue
    from ..registry import ResolutionReport

__all__ = ["Diagnostics", "PluginState", "PluginStatus"]


class PluginState(StrEnum):
    """插件的可观测状态。

    取值直接对应 `contracts.EventName` 的 plugin 族，**不发明第二套生命周期 taxonomy**：
    插件的阶段划分是 / 的事， 只负责显示它。多出来的 `DISABLED` 不是阶段
    而是配置结论，与 `ResolutionReport.disabled` 同义。
    """

    DISCOVERED = "discovered"
    LOADED = "loaded"
    ACTIVATED = "activated"
    DEACTIVATED = "deactivated"
    FAILED = "failed"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class PluginStatus:
    """单个插件的诊断视图：状态、版本、已注册能力、失败原因与阶段。

    `reason` 是 `state` 的补充说明：`DISABLED` 至少有三个来源——没列进
    `plugins.enabled`、列进了 `plugins.disable`、平台不匹配，而用户要做的事各不相同。
    `PluginState` 刻意不为它们各加一个取值（那就是在发明第二套生命周期 taxonomy），
    于是差别落在这一行自由文本上。空串表示无需补充。
    """

    plugin_id: PluginId
    version: str
    state: PluginState
    capabilities: tuple[str, ...] = ()
    failure: KaryviaError | None = None
    failed_phase: str | None = None
    reason: str = ""

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "plugin_id": str(self.plugin_id),
            "version": self.version,
            "state": self.state.value,
            "capabilities": list(self.capabilities),
            "failure": None if self.failure is None else error_to_json(self.failure),
            "failed_phase": self.failed_phase,
            "reason": self.reason,
        }


def _no_plugins() -> Sequence[PluginStatus]:
    return ()


@dataclass(frozen=True, slots=True)
class Diagnostics:
    """三个只读查询的聚合门面。

    数据源是注入的 callable 而不是三个单方法 Protocol：那三样都只有一个方法，为它们各
    定义一个 Protocol 只是在给同一件事起第二个名字（`AGENTS.md`「少结构、多智能」）。
    `capabilities` 是 callable 而不是一份现成的报告，因为覆盖解析在启动期才产出，而
    诊断门面在此之前就已经装配好了。
    """

    events: MemoryRingSink
    capabilities_source: Callable[[], ResolutionReport]
    plugins_source: Callable[[], Sequence[PluginStatus]] = field(default=_no_plugins)

    def capabilities(self) -> ResolutionReport:
        """每项能力最终由内建还是哪个插件提供。"""
        return self.capabilities_source()

    def plugins(self) -> tuple[PluginStatus, ...]:
        """每个插件的状态、版本、已注册能力、失败原因与失败阶段。

        插件运行时落地前默认为空元组——空列表与「查询未接线」在这里
        没有区别，因为此刻确实一个插件都没有。
        """
        return tuple(self.plugins_source())

    def turn(self, turn_id: TurnId) -> tuple[RuntimeEvent, ...]:
        """该 turn 的事件序列，按 `sequence` 升序。

        只查内存环，不回读 JSONL：环是「供 CLI 与诊断查询」的那一个，
        而回读日志文件会让一个只读查询变成 IO 路径。事件不在环里时返回空元组，
        `dropped_events` 说得出那是因为被挤掉了还是本来就没有。
        """
        return self.events.by_turn(turn_id)

    @property
    def dropped_events(self) -> int:
        """内存环因容量上限丢弃的事件数。turn 查不到时先看这个。"""
        return self.events.dropped

    def to_json(self) -> dict[str, JsonValue]:
        """一次性的诊断快照，`karyvia doctor` 直接 `json.dumps` 它。"""
        return {
            "capabilities": self.capabilities().to_json(),
            "plugins": [status.to_json() for status in self.plugins()],
            "dropped_events": self.dropped_events,
        }
