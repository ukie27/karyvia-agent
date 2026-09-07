"""运行时事件契约。

职责：定义冻结的事件名清单、事件族，以及带 `Correlation` 与单调 `sequence`、
构造时即完成脱敏的不可变 `RuntimeEvent`。
不负责：分配 `sequence`、发布与扇出、写 sink、决定订阅者——那些都在
`kernel/observability/`；本模块不含任何 IO。

事件必带实例标识与单调序号，因此单个 turn 的执行过程可以按序完整重放。
脱敏在构造时完成而不是在 sink 端：新增一个 sink 不应重新引入泄漏面。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING

from .errors import ErrorCode, KaryviaError, redact
from .ids import Correlation, InstanceId

if TYPE_CHECKING:  # pragma: no cover - 仅为注解。
    from . import JsonValue

__all__ = ["EventFamily", "EventName", "RuntimeEvent"]

#: 空 payload 的共享只读默认值。MappingProxyType 不可变，可以安全地当默认值用。
_EMPTY_PAYLOAD: Mapping[str, "JsonValue"] = MappingProxyType({})


class EventFamily(StrEnum):
    """事件族。事件名的第一段必须是其中之一。"""

    INSTANCE = "instance"
    PLUGIN = "plugin"
    CAPABILITY = "capability"
    SESSION = "session"
    TURN = "turn"
    MODEL = "model"
    TOOL = "tool"
    CHANNEL = "channel"


class EventName(StrEnum):
    """公开事件名清单。新增成员属于兼容新增，改名或改语义属于破坏性变化。

    三个容易混淆的边界：

    - `TURN_STOPPED_BY_LIMIT` 不等于 `TURN_COMPLETED`，前者表示预算中止而非模型自然结束。
    - `INSTANCE_INPUT_DROPPED` 表示消息在进入 Orchestrator 前被 Channel 背压拒绝；此时尚无
      turn，不能伪造 `TURN_REJECTED`。
    - `CHANNEL_DELIVERY_FAILED` 表示答案和 Transcript 已经产生但投递失败；它既不是
      `TURN_FAILED`，也不必然是 `PLUGIN_FAILED`。

    这些区分保证观察者能分别采取重跑、重发或调整背压等不同补救动作。
    """

    INSTANCE_STARTING = "instance.starting"
    INSTANCE_READY = "instance.ready"
    INSTANCE_STOPPING = "instance.stopping"
    INSTANCE_STOPPED = "instance.stopped"
    INSTANCE_INPUT_DROPPED = "instance.input_dropped"

    PLUGIN_DISCOVERED = "plugin.discovered"
    PLUGIN_LOADED = "plugin.loaded"
    PLUGIN_LOAD_FAILED = "plugin.load_failed"
    PLUGIN_ACTIVATED = "plugin.activated"
    PLUGIN_DEACTIVATED = "plugin.deactivated"
    PLUGIN_FAILED = "plugin.failed"

    CAPABILITY_REGISTERED = "capability.registered"
    CAPABILITY_SHADOWED = "capability.shadowed"
    CAPABILITY_DISABLED = "capability.disabled"
    CAPABILITY_RESOLVED = "capability.resolved"

    SESSION_STARTED = "session.started"
    SESSION_LOADED = "session.loaded"
    SESSION_COMPACTED = "session.compacted"
    SESSION_CLOSED = "session.closed"

    TURN_STARTED = "turn.started"
    TURN_REJECTED = "turn.rejected"
    TURN_COMPLETED = "turn.completed"
    TURN_FAILED = "turn.failed"
    TURN_CANCELLED = "turn.cancelled"
    TURN_STOPPED_BY_LIMIT = "turn.stopped_by_limit"

    MODEL_REQUEST_STARTED = "model.request_started"
    MODEL_RESPONSE_RECEIVED = "model.response_received"
    MODEL_REQUEST_FAILED = "model.request_failed"

    TOOL_CALL_STARTED = "tool.call_started"
    TOOL_CALL_COMPLETED = "tool.call_completed"
    TOOL_CALL_BLOCKED = "tool.call_blocked"
    TOOL_CALL_FAILED = "tool.call_failed"

    CHANNEL_DELIVERY_FAILED = "channel.delivery_failed"

    @property
    def family(self) -> EventFamily:
        """事件名首段即事件族。"""
        return EventFamily(self.value.split(".", 1)[0])


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """一条不可变的运行时事件。

    - `sequence` 由 `EventBus` 单调递增分配，实例内全局唯一，用于按序重放。
    - `correlation` 在实例级事件（启动、插件加载）中为 None：那时还没有会话与 turn。
      它非 None 时，其 `instance_id` 必须与事件自身一致，否则关联链会指向别的实例。
    - `payload` 构造时脱敏并冻结为只读映射，直接交给任何 sink 都安全。
    """

    name: EventName
    sequence: int
    occurred_at: datetime
    instance_id: InstanceId
    correlation: Correlation | None = None
    payload: Mapping[str, JsonValue] = _EMPTY_PAYLOAD
    error: KaryviaError | None = None

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "事件序号必须非负且单调递增。",
                detail={"sequence": self.sequence, "event": self.name.value},
            )
        if self.occurred_at.tzinfo is None:
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "事件时间必须带时区，否则跨实例排序无意义。",
                detail={"event": self.name.value},
            )
        if self.correlation is not None and self.correlation.instance_id != self.instance_id:
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "事件的实例标识与关联标识不一致。",
                detail={
                    "event": self.name.value,
                    "instance_id": self.instance_id,
                    "correlation_instance_id": self.correlation.instance_id,
                },
            )

        redacted, _ = redact(self.payload)
        object.__setattr__(self, "payload", MappingProxyType(dict(redacted)))

    @property
    def family(self) -> EventFamily:
        """所属事件族，等价于 `name.family`。"""
        return self.name.family
