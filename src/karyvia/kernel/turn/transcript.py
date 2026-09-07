"""会话记录与 turn 账本：一次 turn 产生了什么、其中哪些进历史。

职责：`Transcript` 把一次 turn 的用户输入、assistant 正文与工具结果折成 `SessionMessage`
序列，并在持久化边界执行  基线记下的三条编排决定；`TurnState` 是编排期的可变账本
（已产出的文本、待发的出站消息、命令注入的片段）。
不负责：真的写盘（`SessionStore.append`）、决定终态、发事件——那些在 `orchestrator.py`。

**三条决定逐条对应 `tests/baseline/test_loop_behavior.py`**：

1. **空 assistant 消息不入历史**。没有正文的 assistant 记录（纯工具调用轮）在重放时
   只会变成一条无意义的空消息，`ModelMessage` 甚至构造不出来。
2. **孤儿 tool 结果丢弃**：`call_id` 不在本轮声明的调用集合里的工具结果不写入。
   一条没有来处的 tool 记录会让后续请求在 Provider 侧被拒。
3. **工具结果在持久化边界再截断一次**。in-flight 的截断按
   `tool_result_max_bytes`（engine 的 `folding.py`），但历史是长期资产：一次配置调小
   之后，旧记录仍然按旧上限躺在文件里，重放时会把预算撑爆。

assistant 的 `tool_calls` **不进 `SessionMessage`**（契约层没有这个
字段）。工具往返仍然完整写进历史供 `/session` 与诊断查看，但
`context_builder.replay_messages()` 重放时跳过 `role=TOOL` 的记录——见那里的说明。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from karyvia.contracts import (
    AttachmentRef,
    ContextFragment,
    Correlation,
    InboundMessage,
    OutboundMessage,
    Role,
    SessionMessage,
    ToolCall,
    ToolResult,
    TurnId,
)
from karyvia.contracts.message import MAX_ATTACHMENTS

from .compaction import SessionCompactionTracker
from .limits import BudgetLedger, TurnLimits

__all__ = ["Transcript", "TurnState"]


@dataclass(frozen=True, slots=True)
class ModelInput:
    """分流后的一条模型输入；文本与同一消息的附件保持成对。"""

    content: str
    attachments: tuple[AttachmentRef, ...] = ()


@dataclass(slots=True)
class Transcript:
    """一次 turn 的可持久化产物。边跑边攒，终态时一次性交给 `SessionStore.append`。"""

    turn_id: TurnId
    created_at: datetime
    limits: TurnLimits
    _user: list[SessionMessage] = field(default_factory=list)
    _body: list[SessionMessage] = field(default_factory=list)
    _declared: set[str] = field(default_factory=set)
    _assistants: int = 0

    def add_inputs(self, messages: Iterable[InboundMessage]) -> None:
        """记下本批用户输入。用消息自己的 `message_id`，去重与历史因此可以对上。"""
        for message in messages:
            self._user.append(
                SessionMessage(
                    message_id=message.message_id,
                    role=Role.USER,
                    content=message.content,
                    created_at=self.created_at,
                    turn_id=self.turn_id,
                    attachments=message.attachments,
                )
            )

    def declare(self, calls: Sequence[ToolCall]) -> None:
        """登记本轮 assistant 声明的调用，供孤儿判定使用。"""
        self._declared.update(call.call_id for call in calls)

    def add_assistant(
        self,
        content: str,
        *,
        interrupted: bool = False,
        attachments: Sequence[AttachmentRef] = (),
    ) -> None:
        """记一条 assistant 消息；正文和附件同时为空时才丢弃（决定 1）。"""
        if not content and not attachments:
            return
        self._assistants += 1
        self._body.append(
            SessionMessage(
                message_id=f"{self.turn_id}-a{self._assistants}",
                role=Role.ASSISTANT,
                content=content,
                created_at=self.created_at,
                turn_id=self.turn_id,
                interrupted=interrupted,
                attachments=tuple(attachments),
            )
        )

    def add_tool_result(self, result: ToolResult) -> None:
        """记一条工具结果。孤儿丢弃（决定 2），内容再截断一次（决定 3）。"""
        if result.call_id not in self._declared:
            return
        content, _ = self.limits.truncate_tool_result(result.content)
        self._body.append(
            SessionMessage(
                message_id=f"{self.turn_id}-t-{result.call_id}",
                role=Role.TOOL,
                content=content,
                created_at=self.created_at,
                turn_id=self.turn_id,
                tool_call_id=result.call_id,
            )
        )

    def mark_interrupted(self) -> None:
        """把最后一条 assistant 记录标成中断（不得当作完整回答）。"""
        for index in range(len(self._body) - 1, -1, -1):
            record = self._body[index]
            if record.role is Role.ASSISTANT:
                self._body[index] = SessionMessage(
                    message_id=record.message_id,
                    role=record.role,
                    content=record.content,
                    created_at=record.created_at,
                    turn_id=record.turn_id,
                    tool_call_id=record.tool_call_id,
                    interrupted=True,
                    attachments=record.attachments,
                    metadata=record.metadata,
                )
                return

    def records(self) -> tuple[SessionMessage, ...]:
        """按「用户输入在前」的顺序交出全部记录。"""
        return (*self._user, *self._body)


@dataclass(slots=True)
class TurnState:
    """一次 turn 在编排期攒下的东西。

    与 `Transcript` 分开：后者只回答「哪些进历史」，这里还装着只在本次执行中有意义的
    东西（出站消息、命令注入的片段、本轮的账本）。两者塞进一个类，就分不清「这个字段
    会不会落盘」了。
    """

    correlation: Correlation
    started_at: datetime
    transcript: Transcript
    #: 已产出的正文分片，用于在没有最终答复时兜底呈现（取消、失败）。
    text: list[str] = field(default_factory=list)
    #: **尚未写进 transcript** 的正文分片。每轮响应完整时清空——那一轮的内容已经由
    #: `ModelResponseCompleted.response` 权威地记过一次，再按分片记一遍就是重复写入。
    #: 剩下的就是「最后一次完整响应之后产生的内容」，也就是被打断的那半句。
    pending: list[str] = field(default_factory=list)
    #: 分流之后真正要送进模型的输入（可能来自多条被合并的消息）。
    model_inputs: list[ModelInput] = field(default_factory=list)
    #: 命令注入的上下文片段。
    fragments: list[ContextFragment] = field(default_factory=list)
    #: 已发出的中间帧，终帧不在其中——终帧由 `_finish` 单独产出。
    emitted: list[OutboundMessage] = field(default_factory=list)
    #: 本轮工具产出的、要随终帧发给用户的附件。按到达顺序，已去重、已封顶。
    attachments: list[AttachmentRef] = field(default_factory=list)
    #: 因为撞上 `MAX_ATTACHMENTS` 而没能带上的附件条数。**不是静默丢弃**：调用方要据此
    #: 报一条诊断，否则用户看到的是「有几张图没发出来」而日志里一个字都没有。
    dropped_attachments: int = 0
    #: 模型给出的最终答复（没有 tool_calls 的那一轮）。
    final: str = ""
    ledger: BudgetLedger | None = None
    #: `True` 表示答案因 `MAX_TOKENS` 停止被截断。由 orchestrator 在
    #: 处理 `TurnCompleted` 事件时赋值，`_finish` 据此把出站消息改为 `CANCELLED` 状态。
    truncated: bool = False
    #: 本轮请求压缩与初始 Session 水位的映射；没有模型输入的命令 Turn 不创建。
    session_compaction: SessionCompactionTracker | None = None

    def collect_attachments(self, result: ToolResult) -> None:
        """收下一条工具结果里的附件。

        **按 `(source, locator)` 去重**：模型在一轮里两次生成同一张图（内容寻址的落点
        因此相同）不该让用户收到两份。**封顶在 `MAX_ATTACHMENTS`**，与 `OutboundMessage`
        同一个上界——超出的记进 `dropped_attachments` 而不是让终帧的构造抛异常，
        一次成功的 turn 不该因为附件太多而变成失败。

        **不看 `result.ok`**：一条失败的工具调用仍然可能已经把文件写出来了，而它自己
        比 Kernel 更清楚该不该交出来——工具没填 `attachments` 就没有附件，这已经是表态。
        """
        seen = {(item.source, item.locator) for item in self.attachments}
        for attachment in result.attachments:
            key = (attachment.source, attachment.locator)
            if key in seen:
                continue
            if len(self.attachments) >= MAX_ATTACHMENTS:
                self.dropped_attachments += 1
                continue
            seen.add(key)
            self.attachments.append(attachment)

    @property
    def iterations(self) -> int:
        return self.ledger.iterations if self.ledger is not None else 0

    @property
    def tool_calls(self) -> int:
        return self.ledger.tool_calls if self.ledger is not None else 0
