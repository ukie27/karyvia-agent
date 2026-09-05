"""Turn 执行机制：取消、预算、事件、依赖面、折叠、调度、引擎与编排（技术方案 §6.2、§6.4）。

职责：re-export `kernel/turn/` 各模块的公开表面，使调用方只需要
`from karyvia.kernel.turn import ...` 一条导入路径。
不负责：装配一个实例（`runtime/`）、提供具体能力（`builtins/` 与插件）；
本包不读文件、不访问网络——IO 只经注入进来的 `SessionStore` / `ModelProvider` 等发生。

包内依赖是单向的，没有环：

```text
cancel ─┐
        ├─> events ─> engine ──┐
limits ─┘      ↑        ↑      ├─> orchestrator
   └─> folding ─┘   scheduling │
        deps ───────────┘      │
hooks / invoker / context_builder / transcript / translation / orchestration ─┘
```

`cancel` 与 `limits` 之间**没有**依赖：取消是「有人要求停下」，预算是「已经用掉多少」。
把它们缝在一起会让「撞上迭代上限」与「用户中断」共用一条判定路径，而这两件事的 turn 终态
不同（`STOPPED_BY_LIMIT` 对 `CANCELLED`），必须分别可判定。唯一的交点在
`LimitBreach.cancel_reason`：`turn_timeout_ms` 是预算项，触发时以 `CancelReason.TIMEOUT`
走取消路径，由 `engine._terminal_for_breach()` 把这个 reason 交给 `CancelToken.request()`。
"""

from __future__ import annotations

from .cancel import (
    CANCEL_REASON_CODES,
    CHECKPOINT_OWNERS,
    DEFAULT_TOOL_CANCEL_GRACE_MS,
    CancelToken,
    Checkpoint,
    CheckpointOwner,
)
from .context_builder import (
    DEFAULT_CONTEXT_PROVIDER_TIMEOUT_MS,
    AssembledContext,
    ContextProviderBinding,
    DroppedFragment,
    RegisteredContextProvider,
    assemble,
    context_providers_from,
    estimate_tokens,
    replay_messages,
)
from .deps import ENGINE_HOOKS, EngineDeps, HookDispatcher, ToolInvoker
from .engine import run_turn
from .events import (
    TERMINAL_EVENTS,
    ModelReasoningDelta,
    ModelResponseCompleted,
    ModelTextDelta,
    TerminalEvent,
    ToolCallCompleted,
    ToolCallStarted,
    ToolDisposition,
    TurnCancelled,
    TurnCompleted,
    TurnEvent,
    TurnFailed,
    TurnStoppedByLimit,
    terminal_from_error,
)
from .folding import (
    EMPTY_TOOL_RESULT_TEXT,
    StreamFolder,
    assistant_message,
    blocked_result,
    escaped_result,
    fold_tool_result,
    skipped_result,
    unknown_tool_result,
)
from .hooks import (
    DEFAULT_INTERCEPTOR_TIMEOUT_MS,
    DEFAULT_OBSERVER_TIMEOUT_MS,
    HOOK_ACTIONS,
    HOOK_REPLACE_SLOTS,
    HookBinding,
    HookRouter,
    RegisteredHook,
    bindings_from,
)
from .invoker import (
    DEFAULT_MAX_ORPHANS,
    OrphanTask,
    RegisteredTool,
    ToolExecutor,
    tools_from,
)
from .limits import (
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_MAX_TOOL_CALLS_PER_TURN,
    DEFAULT_TOOL_RESULT_MAX_BYTES,
    DEFAULT_TOOL_TIMEOUT_MS,
    DEFAULT_TURN_TIMEOUT_MS,
    FALLBACK_CONTEXT_MAX_TOKENS,
    LIMIT_OUTCOMES,
    BudgetLedger,
    LimitBreach,
    LimitKind,
    TurnLimits,
)
from .memory import (
    DEFAULT_MEMORY_FRAGMENT_PRIORITY,
    DEFAULT_MEMORY_ON_FAILURE,
    DEFAULT_MEMORY_RECALL_LIMIT,
    DEFAULT_MEMORY_RECALL_TIMEOUT_MS,
    MEMORY_ON_FAILURE_CHOICES,
    MEMORY_RECALL_SCOPE,
    MemoryRecall,
    select_memory,
)
from .message_projection import render_message_content
from .orchestration import (
    OrchestratorDeps,
    TurnReceipt,
    emit_outbound,
    engine_deps,
)
from .orchestrator import TurnOrchestrator
from .request_size import ContextBudget, TokenAccounting
from .retry import (
    DEFAULT_RETRY_BASE_DELAY_MS,
    DEFAULT_RETRY_EMPTY_RESPONSE,
    DEFAULT_RETRY_MAX_ATTEMPTS,
    DEFAULT_RETRY_MAX_DELAY_MS,
    MAX_HELD_CHUNKS,
    RETRY_POLL_MS,
    RetryingModel,
    RetryPolicy,
    is_empty_answer,
    retry_delay_ms,
    wait_before_retry,
)
from .scheduling import execute_batch, partition_tool_batches
from .transcript import Transcript, TurnState
from .translation import (
    TERMINAL_EVENT_NAMES,
    TERMINAL_STREAM_STATES,
    TOOL_EVENT_NAMES,
    as_karyvia,
    outcome_for_error,
    outcome_from,
    outcome_without_engine,
    tool_event_name,
)
from .turn_compaction import (
    DEFAULT_TURN_COMPACTOR_TIMEOUT_MS,
    TurnCompactingModel,
    TurnCompactionPolicy,
    project_units,
)

__all__ = [
    "AssembledContext",
    "BudgetLedger",
    "CANCEL_REASON_CODES",
    "CHECKPOINT_OWNERS",
    "CancelToken",
    "Checkpoint",
    "CheckpointOwner",
    "ContextProviderBinding",
    "ContextBudget",
    "DEFAULT_CONTEXT_PROVIDER_TIMEOUT_MS",
    "DEFAULT_INTERCEPTOR_TIMEOUT_MS",
    "DEFAULT_MAX_ITERATIONS",
    "DEFAULT_MAX_ORPHANS",
    "DEFAULT_MAX_TOOL_CALLS_PER_TURN",
    "DEFAULT_MEMORY_FRAGMENT_PRIORITY",
    "DEFAULT_MEMORY_ON_FAILURE",
    "DEFAULT_MEMORY_RECALL_LIMIT",
    "DEFAULT_MEMORY_RECALL_TIMEOUT_MS",
    "DEFAULT_OBSERVER_TIMEOUT_MS",
    "DEFAULT_RETRY_BASE_DELAY_MS",
    "DEFAULT_RETRY_EMPTY_RESPONSE",
    "DEFAULT_RETRY_MAX_ATTEMPTS",
    "DEFAULT_RETRY_MAX_DELAY_MS",
    "DEFAULT_TOOL_CANCEL_GRACE_MS",
    "DEFAULT_TOOL_RESULT_MAX_BYTES",
    "DEFAULT_TOOL_TIMEOUT_MS",
    "DEFAULT_TURN_TIMEOUT_MS",
    "DEFAULT_TURN_COMPACTOR_TIMEOUT_MS",
    "DroppedFragment",
    "EMPTY_TOOL_RESULT_TEXT",
    "ENGINE_HOOKS",
    "EngineDeps",
    "FALLBACK_CONTEXT_MAX_TOKENS",
    "HOOK_ACTIONS",
    "HOOK_REPLACE_SLOTS",
    "HookBinding",
    "HookDispatcher",
    "HookRouter",
    "LIMIT_OUTCOMES",
    "LimitBreach",
    "LimitKind",
    "MAX_HELD_CHUNKS",
    "MEMORY_ON_FAILURE_CHOICES",
    "MEMORY_RECALL_SCOPE",
    "MemoryRecall",
    "ModelReasoningDelta",
    "ModelResponseCompleted",
    "ModelTextDelta",
    "OrchestratorDeps",
    "OrphanTask",
    "RETRY_POLL_MS",
    "RegisteredContextProvider",
    "RegisteredHook",
    "RegisteredTool",
    "RetryPolicy",
    "RetryingModel",
    "StreamFolder",
    "TERMINAL_EVENTS",
    "TERMINAL_EVENT_NAMES",
    "TERMINAL_STREAM_STATES",
    "TOOL_EVENT_NAMES",
    "TerminalEvent",
    "ToolCallCompleted",
    "ToolCallStarted",
    "ToolDisposition",
    "ToolExecutor",
    "ToolInvoker",
    "Transcript",
    "TurnCancelled",
    "TurnCompleted",
    "TurnCompactingModel",
    "TurnCompactionPolicy",
    "TurnEvent",
    "TurnFailed",
    "TurnLimits",
    "TurnOrchestrator",
    "TurnReceipt",
    "TurnState",
    "TurnStoppedByLimit",
    "TokenAccounting",
    "as_karyvia",
    "assemble",
    "assistant_message",
    "bindings_from",
    "blocked_result",
    "context_providers_from",
    "emit_outbound",
    "engine_deps",
    "escaped_result",
    "estimate_tokens",
    "execute_batch",
    "fold_tool_result",
    "is_empty_answer",
    "outcome_for_error",
    "outcome_from",
    "outcome_without_engine",
    "partition_tool_batches",
    "project_units",
    "replay_messages",
    "render_message_content",
    "retry_delay_ms",
    "run_turn",
    "select_memory",
    "skipped_result",
    "terminal_from_error",
    "tool_event_name",
    "tools_from",
    "unknown_tool_result",
    "wait_before_retry",
]
