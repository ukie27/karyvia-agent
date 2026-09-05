"""内建 sink：有界内存环、JSONL 文件，以及配置错误落盘（`NFR-404`、`OBS-005`、`EDG-501`）。

职责：把 `EventBus` 扇出的事件收进一个有容量上限的内存环、或按天追加到 JSONL 文件；
另提供 `EDG-501` 要求的配置解析错误落盘。
不负责：决定谁订阅（`bus.py`）、脱敏（`redaction.py`，事件到这里时已经安全）、
知道实例目录在哪（路径由调用方注入，见 `JsonlFileSink`）。

两个 sink 都只是普通订阅者，Bus 不认识它们——这正是「Bus 只做扇出」的可检验形态。
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, TextIO

from ...contracts import KaryviaError, RuntimeEvent, TurnId
from .redaction import error_to_json, event_to_json

if TYPE_CHECKING:  # pragma: no cover - 仅为注解。
    from ...contracts import JsonValue

__all__ = [
    "DEFAULT_RING_CAPACITY",
    "JsonlFileSink",
    "MemoryRingSink",
    "write_config_error",
]

#: 内存环默认容量。够放下若干个完整 turn 的事件，又不至于让一个长跑实例把内存吃掉。
DEFAULT_RING_CAPACITY: Final = 1024


class MemoryRingSink:
    """有界内存环，供 CLI 与诊断查询（`NFR-404`）。

    满了丢**最旧**的。`dropped` 必须可查：诊断查不到某个 turn 时，「环里从来没有这条」
    与「它被挤出去了」是两个完全不同的结论，塌成「查不到」会让人去错的方向排查。
    """

    __slots__ = ("_dropped", "_events")

    def __init__(self, capacity: int = DEFAULT_RING_CAPACITY) -> None:
        if capacity <= 0:
            raise ValueError("内存环容量必须为正整数。")
        self._events: deque[RuntimeEvent] = deque(maxlen=capacity)
        self._dropped = 0

    def __call__(self, event: RuntimeEvent) -> None:
        if len(self._events) == self._capacity:
            self._dropped += 1
        self._events.append(event)

    @property
    def _capacity(self) -> int:
        # deque(maxlen=…) 保证非 None；单独取出来是为了让上面那行读得懂。
        return self._events.maxlen or 0

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def dropped(self) -> int:
        """因容量上限被挤掉的事件数。"""
        return self._dropped

    def __len__(self) -> int:
        return len(self._events)

    def events(self) -> tuple[RuntimeEvent, ...]:
        """环内全部事件，按 `sequence` 升序。

        排序而不是直接返回插入顺序：`OBS-002` 的重放契约写的是「按序号」，而事件的
        构造点与投递点可以不同（重入排队、将来的跨线程发布）。
        """
        return tuple(sorted(self._events, key=lambda event: event.sequence))

    def by_turn(self, turn_id: TurnId) -> tuple[RuntimeEvent, ...]:
        """属于某个 turn 的事件，按 `sequence` 升序（`OBS-002`）。"""
        return tuple(
            event
            for event in self.events()
            if event.correlation is not None and event.correlation.turn_id == turn_id
        )

    def clear(self) -> None:
        self._events.clear()
        self._dropped = 0


class JsonlFileSink:
    """按天分片的 JSONL 事件日志。

    构造时接一个 `Callable[[date], Path]`，由 `runtime`（`D23`）传
    `layout.events_log_path`。**不 import `kernel.config`，也不在这里第二次拼
    `events-<date>.jsonl` 这个文件名**——两处各写一份必然分叉。

    日期取自 `event.occurred_at`（已保证带时区），跨天自动换文件。

    写失败**不抛**：抛出去只会被 bus 的隔离层吞掉，counter 至少查得到，而
    `last_error` 说得出是哪一类失败。
    """

    __slots__ = ("_day", "_handle", "_last_error", "_path_for_day", "_write_failures", "_written")

    def __init__(self, path_for_day: Callable[[date], Path]) -> None:
        self._path_for_day = path_for_day
        self._day: date | None = None
        self._handle: TextIO | None = None
        self._written = 0
        self._write_failures = 0
        self._last_error: str | None = None

    def __call__(self, event: RuntimeEvent) -> None:
        line = json.dumps(event_to_json(event), ensure_ascii=False, sort_keys=True)
        try:
            handle = self._open_for(event.occurred_at.date())
            handle.write(f"{line}\n")
            handle.flush()
        except OSError as exc:
            self._write_failures += 1
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._close()
        else:
            self._written += 1

    @property
    def written(self) -> int:
        return self._written

    @property
    def write_failures(self) -> int:
        return self._write_failures

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def current_path(self) -> Path | None:
        """当前正在写的文件；还没写过任何事件时为 None。"""
        return None if self._day is None else self._path_for_day(self._day)

    def _open_for(self, day: date) -> TextIO:
        if self._handle is not None and self._day == day:
            return self._handle
        self._close()
        path = self._path_for_day(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 行缓冲 + 每次 flush：崩溃时最后一条事件也已落盘，日志的价值全在最后几行。
        self._handle = path.open("a", encoding="utf-8")
        self._day = day
        return self._handle

    def _close(self) -> None:
        handle, self._handle, self._day = self._handle, None, None
        if handle is None:
            return
        try:
            handle.close()
        except OSError:  # pragma: no cover - 关闭失败没有可做的补救。
            pass

    def close(self) -> None:
        """关闭当前文件句柄。实例停止时调用；再次写入会重新打开。"""
        self._close()


def write_config_error(
    path: Path, error: KaryviaError, *, occurred_at: datetime | None = None
) -> bool:
    """把一条配置解析错误追加写到 `path`，返回是否成功（`EDG-501` 后半句）。

    刻意**不是**一个 sink：`EDG-501` 的场景是配置解析失败，那时事件总线还没建起来
    （启动第 2 步在建 bus 之前）。做成订阅者就等于把这条需求推回它无法成立的时序里。

    `KaryviaError` 构造时已脱敏，因此这里直接序列化；best-effort，失败返回 False 而不是
    抛出——在一条已经失败的启动路径上再抛一次，只会把真正的原因盖掉。
    """
    stamp = occurred_at if occurred_at is not None else datetime.now(UTC)
    record: dict[str, JsonValue] = {
        "kind": "config_error",
        "occurred_at": stamp.isoformat(),
        "error": error_to_json(error),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{json.dumps(record, ensure_ascii=False, sort_keys=True)}\n")
    except OSError:
        return False
    return True
