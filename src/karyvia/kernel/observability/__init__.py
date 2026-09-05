"""可观测性：事件总线、脱敏与序列化、内建 sink、诊断查询（技术方案 §6.8）。

职责：re-export `redaction` / `bus` / `sinks` / `diagnostics` 四个模块的公开表面，
使调用方只需要 `from karyvia.kernel.observability import ...` 一条导入路径。
不负责：决定发布哪些事件（那是 engine / orchestrator / runtime 的事）、知道实例目录在哪
（JSONL sink 的路径由调用方注入）、认识任何具体消费者。

四个模块的分工是单向的：`bus` 用 `redaction`，`sinks` 用 `redaction`，
`diagnostics` 用 `sinks`，反过来都不成立。Bus 不认识任何 sink——内建的两个 sink 也只是
普通订阅者，这正是「Bus 只做扇出」（`OBS-005`）的可检验形态。
"""

from __future__ import annotations

from .bus import (
    DEFAULT_MAX_STRIKES,
    DEFAULT_RETIRED_HISTORY,
    DEFAULT_SLOW_AFTER_MS,
    EventBus,
    Subscriber,
    SubscriberHealth,
    Subscription,
)
from .diagnostics import Diagnostics, PluginState, PluginStatus
from .redaction import (
    MAX_PAYLOAD_ENTRIES,
    MAX_SEQUENCE_ITEMS,
    error_to_json,
    event_to_json,
    prepare_payload,
)
from .sinks import DEFAULT_RING_CAPACITY, JsonlFileSink, MemoryRingSink, write_config_error

__all__ = [
    "DEFAULT_MAX_STRIKES",
    "DEFAULT_RETIRED_HISTORY",
    "DEFAULT_RING_CAPACITY",
    "DEFAULT_SLOW_AFTER_MS",
    "MAX_PAYLOAD_ENTRIES",
    "MAX_SEQUENCE_ITEMS",
    "Diagnostics",
    "EventBus",
    "JsonlFileSink",
    "MemoryRingSink",
    "PluginState",
    "PluginStatus",
    "Subscriber",
    "SubscriberHealth",
    "Subscription",
    "error_to_json",
    "event_to_json",
    "prepare_payload",
    "write_config_error",
]
