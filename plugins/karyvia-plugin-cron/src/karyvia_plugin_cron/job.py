"""任务的数据形状与 JSON 编解码。**不碰文件、不碰时钟。**

职责：`Schedule` / `Origin` / `RunRecord` / `CronJob` 四个不可变形状，以及它们与
`jobs.json` 里那份 JSON 之间的双向翻译。
不负责：算下一次运行时刻（`schedule.py`）、读写文件（`store.py`）、
表达式语法（`expr.py`）。

**任务是不可变的，改动一律 `dataclasses.replace`。** 调度循环、三条工具与 `/cron` 都会
读到同一批任务对象，就地改字段会让「这条任务现在是什么样」取决于谁先跑到——而这正是
调度类代码最难复现的一类问题。

**时间一律存 UTC ISO 串**（`created_at` / `next_run_at` / 运行历史）。**只有
`Schedule.tz` 是时区名**，因为「每天早上 9 点」这句话必须跟着某个墙钟走：存成 UTC 瞬间
的话，夏令时一切换，用户的 9 点就变成 8 点或 10 点。

**格式一旦发布就是契约**（同一条判据）：`jobs.json` 的字段名与
`docs`/README 里的示例由测试直接解析，改这里的字段就要改那边。未知字段在读取时**丢弃
而不是报错**——一个被降级的实例不该因为新版本写过的字段而拒绝启动；但 `version` 高于
本实现即拒绝（不猜、不降级，与 `builtins/session_jsonl` 同）。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from karyvia.contracts import ErrorCode, JsonValue, KaryviaError

__all__ = [
    "MAX_HISTORY",
    "MAX_MESSAGE_CHARS",
    "MAX_NAME_CHARS",
    "CronJob",
    "Origin",
    "RunRecord",
    "RunStatus",
    "Schedule",
    "ScheduleKind",
    "decode_job",
    "encode_job",
    "new_job_id",
    "parse_moment",
]

#: 每个任务保留多少条运行历史。取小：历史是给人排查用的，不是审计日志，而
#: `jobs.json` 每次保存都整份重写。
MAX_HISTORY: Final = 20

#: 任务正文的上界。它会成为一条 `InboundMessage.content`，而那条消息要进模型上下文。
MAX_MESSAGE_CHARS: Final = 2_000

#: 任务名的上界。名字只用于展示与检索。
MAX_NAME_CHARS: Final = 80

_BAD_RECORD: Final = "jobs.json 里的任务记录不合法。"
_BAD_MOMENT: Final = "时间字段不是合法的 ISO 8601 时刻。"


class ScheduleKind(StrEnum):
    """三种调度形态。**没有第四种**：其余需求（工作日、月末）由 cron 表达式表达。"""

    AT = "at"
    EVERY = "every"
    CRON = "cron"


class RunStatus(StrEnum):
    """一次运行的结局。**它描述的是「派发」而不是「turn 成功了没有」**。

    Channel 泵吞掉 `TurnReceipt`（`runtime/instance.py::_fanout_for`），而按 `session_key`
    关联 turn 事件分不清同会话的并发 turn。因此这里如实只记到派发这一步，
    README 与 `/cron` 的输出里都这么说。
    """

    #: 消息已交给泵。turn 之后成功与否不在这条记录的能力范围内。
    DISPATCHED = "dispatched"
    #: 到期时任务被跳过（错过窗口之外的补跑）。
    SKIPPED = "skipped"
    #: 一次性任务在进程停止期间过期，且不在补跑窗口内。
    MISSED = "missed"
    #: 派发本身失败（构造消息时出错）。
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Origin:
    """任务绑定的会话。**结果就投递到这里。**

    只存 `channel_id + conversation_id`：`SessionKey` 的第三个分量 `scope` 是实例级常量
    （`kernel/turn/orchestrator.py` 用 `deps.scope` 统一填），存下来只会与配置分叉。
    """

    channel_id: str
    conversation_id: str


@dataclass(frozen=True, slots=True)
class Schedule:
    """一条调度定义。三种形态共用一个形状，无关字段为 `None`。"""

    kind: ScheduleKind
    #: `AT`：绝对时刻（带时区）。
    at: datetime | None = None
    #: `EVERY`：间隔毫秒。
    every_ms: int | None = None
    #: `CRON`：5 字段表达式。
    expr: str | None = None
    #: `CRON`：IANA 时区名。留空即用配置里的默认时区。
    tz: str | None = None

    def describe(self) -> str:
        """给人看的一行描述。`/cron list` 与工具返回值共用它。"""
        if self.kind is ScheduleKind.AT:
            return f"一次性 {self.at.isoformat() if self.at else '?'}"
        if self.kind is ScheduleKind.EVERY:
            return f"每 {(self.every_ms or 0) // 1000} 秒"
        return f"cron {self.expr}" + (f"（{self.tz}）" if self.tz else "")


@dataclass(frozen=True, slots=True)
class RunRecord:
    """一次到期派发的记录。"""

    fired_at: datetime
    status: RunStatus
    #: 补充说明。只放本插件自己生成的文本，不放第三方异常消息。
    detail: str = ""


@dataclass(frozen=True, slots=True)
class CronJob:
    """一个定时任务。"""

    job_id: str
    name: str
    #: 到点注入的正文，会成为 `InboundMessage.content`。
    message: str
    origin: Origin
    schedule: Schedule
    created_at: datetime
    enabled: bool = True
    next_run_at: datetime | None = None
    history: tuple[RunRecord, ...] = ()

    def with_run(self, record: RunRecord, *, next_run_at: datetime | None) -> CronJob:
        """记一次运行并推进下一次时刻。历史只留最近 `MAX_HISTORY` 条。"""
        return replace(
            self,
            history=(*self.history, record)[-MAX_HISTORY:],
            next_run_at=next_run_at,
            # 一次性任务跑完就没有下一次，停用它而不是删掉——用户还要看得到它跑过。
            enabled=self.enabled if next_run_at is not None else False,
        )

    @property
    def last_run(self) -> RunRecord | None:
        return self.history[-1] if self.history else None


def new_job_id() -> str:
    """生成任务标识。短、可读、可整串复制进 `/cron rm`。"""
    return f"cj-{uuid.uuid4().hex[:10]}"


def parse_moment(raw: JsonValue, *, field_name: str) -> datetime:
    """解析一个 ISO 时刻，**必须带时区**。

    不带时区的时间在跨时区的定时任务里没有意义，静默按本地时区补全会让同一份
    `jobs.json` 在两台机器上排出不同的时间表。
    """
    if not isinstance(raw, str):
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED, _BAD_MOMENT, detail={"field": field_name}
        )
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError as error:
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED, _BAD_MOMENT, detail={"field": field_name}
        ) from error
    if moment.tzinfo is None:
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED, _BAD_MOMENT, detail={"field": field_name}
        )
    return moment


# ------------------------------------------------------------------------------ 编码


def encode_job(job: CronJob) -> dict[str, JsonValue]:
    """任务 → JSON。时刻统一换算成 UTC 再写，读的一方因此不必猜偏移的含义。"""
    payload: dict[str, JsonValue] = {
        "id": job.job_id,
        "name": job.name,
        "message": job.message,
        "enabled": job.enabled,
        "created_at": _iso(job.created_at),
        "origin": {
            "channel_id": job.origin.channel_id,
            "conversation_id": job.origin.conversation_id,
        },
        "schedule": _encode_schedule(job.schedule),
        "history": [
            {
                "fired_at": _iso(record.fired_at),
                "status": record.status.value,
                "detail": record.detail,
            }
            for record in job.history
        ],
    }
    if job.next_run_at is not None:
        payload["next_run_at"] = _iso(job.next_run_at)
    return payload


def _encode_schedule(schedule: Schedule) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {"kind": schedule.kind.value}
    if schedule.at is not None:
        payload["at"] = _iso(schedule.at)
    if schedule.every_ms is not None:
        payload["every_ms"] = schedule.every_ms
    if schedule.expr is not None:
        payload["expr"] = schedule.expr
    if schedule.tz is not None:
        payload["tz"] = schedule.tz
    return payload


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


# ------------------------------------------------------------------------------ 解码


def decode_job(raw: JsonValue) -> CronJob:
    """JSON → 任务。**未知字段丢弃，缺必填字段报错。**"""
    data = _require_mapping(raw, "job")
    origin = _require_mapping(data.get("origin"), "job.origin")
    next_run = data.get("next_run_at")
    return CronJob(
        job_id=_require_text(data.get("id"), "job.id"),
        name=_require_text(data.get("name"), "job.name"),
        message=_require_text(data.get("message"), "job.message"),
        origin=Origin(
            channel_id=_require_text(origin.get("channel_id"), "job.origin.channel_id"),
            conversation_id=_require_text(
                origin.get("conversation_id"), "job.origin.conversation_id"
            ),
        ),
        schedule=_decode_schedule(data.get("schedule")),
        created_at=parse_moment(data.get("created_at"), field_name="job.created_at"),
        enabled=_require_bool(data.get("enabled"), "job.enabled"),
        next_run_at=(
            parse_moment(next_run, field_name="job.next_run_at")
            if next_run is not None
            else None
        ),
        history=_decode_history(data.get("history")),
    )


def _decode_schedule(raw: JsonValue) -> Schedule:
    data = _require_mapping(raw, "job.schedule")
    kind_text = _require_text(data.get("kind"), "job.schedule.kind")
    if kind_text not in tuple(kind.value for kind in ScheduleKind):
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            _BAD_RECORD,
            detail={"field": "job.schedule.kind", "value": kind_text},
        )
    # 三个可选字段各绑一个局部变量再判：`isinstance(data.get(k), str)` 之后再写
    # `data[k]` 是两次查表，类型检查器也narrow不到——而它narrow不到的地方，
    # 正是「判的和取的可能不是同一个值」的地方。
    every = data.get("every_ms")
    at = data.get("at")
    expr = data.get("expr")
    tz = data.get("tz")
    return Schedule(
        kind=ScheduleKind(kind_text),
        at=parse_moment(at, field_name="job.schedule.at") if at is not None else None,
        every_ms=every if isinstance(every, int) and not isinstance(every, bool) else None,
        expr=expr if isinstance(expr, str) else None,
        tz=tz if isinstance(tz, str) else None,
    )


def _decode_history(raw: JsonValue) -> tuple[RunRecord, ...]:
    """运行历史。**坏记录整条丢弃而不是让整个任务读不出来**——历史是诊断数据，
    为它拒绝加载一个还在排期的任务，代价与收益完全不成比例。"""
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        return ()
    records: list[RunRecord] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        status = item.get("status")
        fired = item.get("fired_at")
        if not isinstance(status, str) or status not in tuple(s.value for s in RunStatus):
            continue
        if not isinstance(fired, str):
            continue
        try:
            moment = parse_moment(fired, field_name="history.fired_at")
        except KaryviaError:
            continue
        detail = item.get("detail")
        records.append(
            RunRecord(
                fired_at=moment,
                status=RunStatus(status),
                detail=detail if isinstance(detail, str) else "",
            )
        )
    return tuple(records[-MAX_HISTORY:])


def _require_mapping(raw: JsonValue | None, field_name: str) -> Mapping[str, JsonValue]:
    if not isinstance(raw, Mapping):
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED, _BAD_RECORD, detail={"field": field_name}
        )
    return raw


def _require_text(raw: JsonValue | None, field_name: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED, _BAD_RECORD, detail={"field": field_name}
        )
    return raw


def _require_bool(raw: JsonValue | None, field_name: str) -> bool:
    if raw is None:
        return True
    if not isinstance(raw, bool):
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED, _BAD_RECORD, detail={"field": field_name}
        )
    return raw
