"""生产级 `InstanceView` / `TurnControl`：把 kernel 的查询接到插件够得着的门面上。

职责：用 `CommandIndex`、`Diagnostics`、配置文档与 `SessionStore` 装出一个 `InstanceView`，
用 `TurnOrchestrator` 装出一个 `TurnControl`。
不负责：产生这些数据（在 `kernel/`）、渲染输出（`builtins/commands_core/render.py`）、
构造 `PluginContext` 本身。

**这里是 `R5` 的落点**，理由与 `wiring.py` 完全相同：门面的类型在 `contracts/`，实现要读
`kernel/registry` 与 `kernel/observability` 的东西，而 `R4` 禁止 `builtins/` 够到它们、
`R2` 禁止 `kernel/` 反向依赖。全项目只有 `runtime/` 同时看得见两边。

**一致性同样靠类型标注静态证明**（`wiring.py` 的既有实现）：`build_instance_view()` 与
`build_turn_control()` 的返回类型就写成 `InstanceView` / `TurnControl`，basedpyright 严格
模式下它一旦不成立就当场报错。测试验不了这件事（`pyproject.toml` 把 `**/tests` 排除在
类型检查之外），`isinstance` 也验不了（`runtime_checkable` 只查属性存在性）。

**命令索引是惰性取的**（`commands_source` 是 callable 而不是一份现成的 `CommandIndex`）：
与 `Diagnostics.capabilities_source` 同一条理由——索引在启动期才建得出来，而
`PluginContext` 要在 `setup()` 之前就交给插件。给它一份当时还不存在的索引是不可能的。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from karyvia.contracts import (
    CancelReason,
    CommandSpec,
    InstanceView,
    JsonValue,
    SessionKey,
    SessionSnapshot,
    SessionStore,
    TurnControl,
    TurnId,
)
from karyvia.kernel.observability import Diagnostics
from karyvia.kernel.routing import CommandIndex
from karyvia.kernel.turn import TurnOrchestrator

__all__ = ["KernelInstanceView", "KernelTurnControl", "build_instance_view", "build_turn_control"]


@dataclass(frozen=True, slots=True)
class KernelInstanceView:
    """`InstanceView` 的生产实现。结构化满足契约，不继承任何宿主基类。"""

    commands_source: Callable[[], CommandIndex]
    diagnostics: Diagnostics
    config_source: Callable[[], Mapping[str, JsonValue]]
    sessions: SessionStore

    def commands(self) -> tuple[CommandSpec, ...]:
        """`CommandIndex.specs()` 已按命令名排序去重，这里原样交出。"""
        return self.commands_source().specs()

    def capabilities(self) -> Mapping[str, JsonValue]:
        return self.diagnostics.capabilities().to_json()

    def plugins(self) -> tuple[Mapping[str, JsonValue], ...]:
        return tuple(status.to_json() for status in self.diagnostics.plugins())

    def config_document(self) -> Mapping[str, JsonValue]:
        """完整配置文档。

        明文凭据结构性地不在这里： 定死配置树自始至终持有 `${VAR}` 字面量，解析出的
        明文只在 `SecretMap` 里，而 `LoadedConfig.to_json()` 序列化的就是那棵树。
        """
        return self.config_source()

    async def session_snapshot(self, key: SessionKey) -> SessionSnapshot:
        """只转发 `load()`。

        **刻意只暴露这一个方法**而不是整个 store：`SessionStore` 带着 `delete()` 与
        `compact()`，而读一个快照的调用方一个都不需要。门面的宽度应当等于用途的宽度。
        """
        return await self.sessions.load(key)


@dataclass(frozen=True, slots=True)
class KernelTurnControl:
    """`TurnControl` 的生产实现。

    **`TurnOrchestrator.cancel` 是 §10.3 的唯一入口**，这里只是把它转发到
    插件够得着的位置——不另建一张令牌表。
    """

    orchestrator: TurnOrchestrator

    def live_turns(self) -> tuple[TurnId, ...]:
        # `TurnOrchestrator.live_turns` 是 property，本门面把它做成方法：契约的其余成员
        # 都是方法，为一个字段破例只会让实现方多记一条例外。
        return self.orchestrator.live_turns

    def cancel_turn(self, turn_id: TurnId, reason: CancelReason = CancelReason.USER) -> bool:
        return self.orchestrator.cancel(turn_id, reason)


def build_instance_view(
    *,
    commands_source: Callable[[], CommandIndex],
    diagnostics: Diagnostics,
    config_source: Callable[[], Mapping[str, JsonValue]],
    sessions: SessionStore,
) -> InstanceView:
    """装出只读视图。返回类型即「它满足契约」的静态证明（见模块 docstring）。"""
    view: InstanceView = KernelInstanceView(
        commands_source=commands_source,
        diagnostics=diagnostics,
        config_source=config_source,
        sessions=sessions,
    )
    return view


def build_turn_control(orchestrator: TurnOrchestrator) -> TurnControl:
    """装出 turn 控制面。返回类型即静态证明。"""
    control: TurnControl = KernelTurnControl(orchestrator=orchestrator)
    return control
