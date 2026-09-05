"""输入分流与 Session 并发（技术方案 §6.3、§6.5）。

职责：re-export `dispatcher` / `session_lock` / `dedup` 三个模块的公开表面，
使调用方只需 `from karyvia.kernel.routing import ...` 一条导入路径。
不负责：执行 turn（`kernel/turn/`）、发布事件（`D14` 的 orchestrator 是 turn 事件的唯一
发布点）、实现任何具体命令（`builtins/commands_core/`，`D22`）。

三个模块互不相识，因为它们回答的是三个独立的问题，编排层按顺序问一遍即可：

    这条消息是不是重复投递？   dedup.DedupCache.remember()
    这个 session 现在能不能写？ session_lock.SessionScheduler.submit()
    这条输入该走命令还是模型？ dispatcher.Dispatcher.dispatch()

顺序是有讲究的：**去重在最前面**（在后三者之中）。重复投递的消息不该占用队列名额，
更不该在 `MERGE` 策略下被并进下一批——那两件事都会让「重复投递不产生第二次副作用」
（`EDG-201`）失效。

Session 票据在 `submit()` 返回前同步登记；Runtime Channel 泵因此只需并发等待结果，不再
维护第二套 per-conversation 队列。
"""

from __future__ import annotations

from .dedup import (
    DEFAULT_DEDUP_CAPACITY,
    DEFAULT_DEDUP_TTL_MS,
    DedupCache,
    DedupHit,
)
from .dispatcher import (
    DEFAULT_COMMAND_PREFIX,
    CommandIndex,
    Dispatcher,
    DispatchOutcome,
    ParsedCommand,
    RegisteredCommand,
    build_command_index,
    parse_command,
)
from .session_lock import (
    DEFAULT_QUEUE_MAX_SIZE,
    ConcurrencyPolicy,
    SessionScheduler,
    SessionSlot,
    SubmitOutcome,
    SubmitStatus,
)

__all__ = [
    "DEFAULT_COMMAND_PREFIX",
    "DEFAULT_DEDUP_CAPACITY",
    "DEFAULT_DEDUP_TTL_MS",
    "DEFAULT_QUEUE_MAX_SIZE",
    "CommandIndex",
    "ConcurrencyPolicy",
    "DedupCache",
    "DedupHit",
    "DispatchOutcome",
    "Dispatcher",
    "ParsedCommand",
    "RegisteredCommand",
    "SessionScheduler",
    "SessionSlot",
    "SubmitOutcome",
    "SubmitStatus",
    "build_command_index",
    "parse_command",
]
