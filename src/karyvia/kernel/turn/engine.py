"""Turn Engine：一次 turn 的模型-工具迭代循环（技术方案 §6.2 第一层）。

职责：执行模型与工具的迭代循环，在 4 个命名检查点上响应取消，在每轮迭代前与每批工具发起前
判预算，分发 4 个 turn 内 Hook，并把全过程表达为一条**以恰好一个终态事件结尾**的事件流。
不负责：session 加载与写入、context 组装、命令分流、事件发布、`OutboundMessage` 生成、
turn 总超时的看门狗与模型请求的重发——前者在 `retry.py`（Engine 不知道重试存在），其余在
`orchestrator.py`；长度截断续写属于本模块的模型-消息循环；
本模块只通过 `EngineDeps` 与外界交互，不做任何文件、网络或数据库操作。

**三条不变量**（技术方案 §6.2，全部有测试守护）：

1. 只通过 `deps` 与外界交互。可检查的形态是本模块的 import 清单里没有 `os` / `pathlib` /
   `socket`——有一条测试解析本文件的 AST 盯着它。
2. `deps` 回调抛出的任何异常都转成终态事件，engine 自身不向上抛。「没有正常事件序列的中断」
   会让 orchestrator 无从善后（Pi 明确列出的坑）。落点是 `run_turn` 里**唯一**的那处
   `except`——不是散落各处的 try/except。
3. 单个事件一旦 yield，其内容不再变更（全部 frozen dataclass）。

**捕 `Exception` 而不捕 `BaseException` 是刻意的**：`asyncio.CancelledError`（3.11 起属于
`BaseException`）、`GeneratorExit`、`KeyboardInterrupt` 必须穿透——那是**任务本身**被杀，
不是 turn 被取消。本项目的 turn 取消走 `CancelToken` 正是为了让这两件事可区分（§6.4）；
把它们也吞掉，等于把「进程正在退出」伪装成「这轮对话结束了」。

**续写与重试的分界线**：长度续写不能靠 Hook 改写响应，只能在同一轮循环中
复用 `ledger`，把上一轮 assistant 消息放回 `messages`。`MAX_TOKENS` 且无工具调用时，
engine 最多续写 `_MAX_LENGTH_RECOVERIES` 次；耗尽后以 `truncated=True` 收尾，编排层聚合正文。

「模型根本没给出响应」是另一件事：重发同一个请求不改写任何响应，因此它落在包住
`ModelProvider` 的 `retry.py` 里，engine 一个字都不用改。**不能反过来在 orchestrator 重跑
`run_turn` 来实现它**——那会把已经执行过的工具再做一遍。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, replace

from karyvia.contracts import (
    CancelReason,
    ChunkKind,
    Correlation,
    ErrorCode,
    HookAction,
    HookContext,
    HookName,
    HookOutcome,
    KaryviaError,
    ModelMessage,
    ModelRequest,
    StopReason,
    ToolCall,
    ToolResult,
    ToolSpec,
    TurnStatus,
)

from .cancel import CancelToken, Checkpoint
from .deps import EngineDeps
from .events import (
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
    TurnStoppedByLimit,
    terminal_from_error,
)
from .folding import (
    StreamFolder,
    assistant_message,
    blocked_result,
    escaped_result,
    fold_tool_result,
    skipped_result,
    unknown_tool_result,
)
from .limits import BudgetLedger, LimitBreach
from .replacement import validated_model_request, validated_tool_invocation
from .scheduling import execute_batch, partition_tool_batches

__all__ = ["run_turn"]

#: 拦截器允许的处置。`REJECT` 只属于 `turn_start`（orchestrator 的 Hook），出现在这里说明
#: handler 用错了 Hook——静默忽略会让插件以为自己拒掉了这个 turn，实际什么都没发生。
_REQUEST_ACTIONS = frozenset({HookAction.CONTINUE, HookAction.REPLACE})
_TOOL_ACTIONS = frozenset({HookAction.CONTINUE, HookAction.REPLACE, HookAction.BLOCK})
_MAX_LENGTH_RECOVERIES = 3


@dataclass(frozen=True, slots=True)
class _Attempt:
    """一次工具调用的结果与处置。只在本模块内部流转，不进事件流。"""

    result: ToolResult
    disposition: ToolDisposition


async def run_turn(
    request: ModelRequest,
    deps: EngineDeps,
    cancel: CancelToken,
    *,
    ledger: BudgetLedger | None = None,
) -> AsyncIterator[TurnEvent]:
    """执行一次 turn，产出事件流。

    `request` 是**第一轮**的完整请求，后续每轮由它派生（`replace(messages=..., timeout_ms=...)`）。
    以整个 `ModelRequest` 为种子而不是 `(messages, tool_specs)`：`before_model_request` Hook 的
    载荷槽就是 `ModelRequest`，拆成两个参数就得把 Hook 改过的请求再拆回去，而
    `params` / `timeout_ms` 的改写在那一步会被静默丢掉。顺带地，「模型看得见的工具集」与
    「engine 能调度的工具集」成了同一个对象（`request.tools`），不需要额外保证同源。

    `ledger` 不传则新建。传入的场景是 orchestrator 的续写与总超时看门狗要与 engine 共用
    一本账（见模块 docstring）。

    **异常约定**：不抛。一切异常都以 `TurnFailed` / `TurnCancelled` 收场，`BaseException` 除外。
    **取消语义**：在 4 个检查点上观察 `cancel`；工具拿到的是 `cancel.child()`。
    """
    book = ledger if ledger is not None else BudgetLedger(deps.limits)
    try:
        async for event in _iterate(request, deps, cancel, book):
            yield event
    # 捕 Exception 而不是 BaseException，见模块 docstring 的不变量 2 与那段说明。
    except Exception as error:
        yield terminal_from_error(
            error,
            reason=cancel.reason,
            iterations=book.iterations,
            tool_calls=book.tool_calls,
        )


async def _iterate(
    request: ModelRequest,
    deps: EngineDeps,
    cancel: CancelToken,
    ledger: BudgetLedger,
) -> AsyncIterator[TurnEvent]:
    """主循环。所有异常都留给 `run_turn` 的那一处 `except` 落地。"""
    messages = list(request.messages)
    length_recoveries = 0

    while True:
        breach = ledger.check()
        if breach is not None:
            yield _terminal_for_breach(breach, cancel, ledger)
            return

        # 检查点先于记账：在这里被取消的一轮从未真的开始，把它计进 `iterations` 会让
        # `TurnOutcome` 里的迭代数比实际多一次。
        cancel.checkpoint(Checkpoint.BEFORE_MODEL_REQUEST)
        ledger.begin_iteration()
        iteration = ledger.iterations

        current = await _prepare_request(request, messages, deps, ledger)
        specs = {spec.name: spec for spec in current.tools}

        if current.stream:
            folder = StreamFolder(current.model_id)
            async for chunk in deps.model.stream(current, cancel):
                cancel.checkpoint(Checkpoint.BETWEEN_STREAM_CHUNKS)
                folder.push(chunk)
                if chunk.kind is ChunkKind.TEXT:
                    yield ModelTextDelta(iteration, chunk.text)
                elif chunk.kind is ChunkKind.REASONING:
                    yield ModelReasoningDelta(iteration, chunk.text)
            response = folder.finish()
        else:
            response = await deps.model.complete(current, cancel)

        # 观察者：返回值按 §6.6 被忽略，engine 不去判断这次分发的是不是观察者。
        await deps.hooks.dispatch(
            HookContext(
                HookName.AFTER_MODEL_RESPONSE,
                correlation=current.correlation,
                response=response,
            )
        )

        if not response.tool_calls:
            continuation: ModelMessage | None = None
            if (
                response.stop_reason is StopReason.MAX_TOKENS
                and response.content
                and length_recoveries < _MAX_LENGTH_RECOVERIES
            ):
                continuation = assistant_message(response)
            yield ModelResponseCompleted(iteration, response, continuation)
            if continuation is not None:
                messages.append(continuation)
                length_recoveries += 1
                continue
            truncated = response.stop_reason is StopReason.MAX_TOKENS
            yield TurnCompleted(response, ledger.iterations, ledger.tool_calls, truncated=truncated)
            return

        assistant = assistant_message(response)
        messages.append(assistant)
        yield ModelResponseCompleted(iteration, response, assistant)

        # 预算判定必须在**发起前**：先记账再判定会让最后一批工具真的执行完才发现超了，
        # 副作用已经发生，上限也就失去了意义（`limits.check` 的 docstring 写死了这条）。
        breach = ledger.check(pending_tool_calls=len(response.tool_calls))
        if breach is not None:
            yield _terminal_for_breach(breach, cancel, ledger)
            return
        ledger.record_tool_calls(len(response.tool_calls))

        cancel.checkpoint(Checkpoint.BEFORE_TOOL_CALL)
        folded: dict[str, ModelMessage] = {}
        async for event in _run_tool_phase(
            response.tool_calls, specs, deps, cancel, current.correlation, ledger, iteration, folded
        ):
            yield event
        # 消息顺序恒等于 tool_calls 顺序，与事件的完成顺序无关：前者是模型的输入契约，
        # 后者是给 UI 的实时反馈。并发调度不得改变模型看到的对应关系。
        messages.extend(folded[call.call_id] for call in response.tool_calls)
        cancel.checkpoint(Checkpoint.AFTER_TOOL_RESULT)


async def _run_tool_phase(
    calls: Sequence[ToolCall],
    specs: Mapping[str, ToolSpec],
    deps: EngineDeps,
    cancel: CancelToken,
    correlation: Correlation,
    ledger: BudgetLedger,
    iteration: int,
    folded: dict[str, ModelMessage],
) -> AsyncIterator[TurnEvent]:
    """按批次执行本轮工具，产出事件，并把每次调用对应的消息写进 `folded`。

    `ToolCallStarted` 在每批开始时**整批发出**——并发批里它们确实是一起启动的，而串行批只有
    一个成员，两种情形下「开始事件先于它自己的完成事件」都成立。

    消息用出参而不是返回值：本函数是异步生成器，事件必须边产边发（UI 要实时反馈），
    而消息要等整轮结束后按 `tool_calls` 顺序重排——两种消费节奏，一个出口装不下。
    """
    for batch in partition_tool_batches(calls, specs):
        for call in batch:
            yield ToolCallStarted(iteration, call)

        async def run_one(call: ToolCall) -> _Attempt:
            return await _invoke_one(call, specs, deps, cancel, correlation, ledger)

        async for call, attempt in execute_batch(batch, run_one):
            # 截断在 `after_tool_call` **之后**：那个 Hook 是拦截器、可以 REPLACE 掉整个结果，
            # 先截后钩等于给插件留一条绕过 `tool_result_max_bytes` 的路。
            result, message = fold_tool_result(call, attempt.result, deps.limits)
            folded[call.call_id] = message
            yield ToolCallCompleted(iteration, call, result, message, attempt.disposition)


def _terminal_for_breach(
    breach: LimitBreach, cancel: CancelToken, ledger: BudgetLedger
) -> TerminalEvent:
    """把一次越界翻译成终态事件。

    终态查 `LimitBreach.terminal_status`（`LIMIT_OUTCOMES` 是唯一来源），不在这里硬编码：
    `turn_timeout_ms` 的终态是 `CANCELLED` 而不是 `STOPPED_BY_LIMIT`，两者的善后动作不同。
    走取消路径时先 `cancel.request()`，让在途的子令牌也收到信号——否则「turn 已经超时」这件事
    传不到正在执行的工具那里。
    """
    if breach.terminal_status is TurnStatus.CANCELLED:
        reason = breach.cancel_reason or CancelReason.BUDGET
        cancel.request(reason)
        return TurnCancelled(reason, ledger.iterations, ledger.tool_calls)
    return TurnStoppedByLimit(breach, ledger.iterations, ledger.tool_calls)


async def _prepare_request(
    request: ModelRequest,
    messages: Sequence[ModelMessage],
    deps: EngineDeps,
    ledger: BudgetLedger,
) -> ModelRequest:
    """派生本轮请求并过一遍 `before_model_request` Hook。

    这个 Hook 由 Engine **每轮**分发。第一轮请求与进入 Engine 的边界重合；第 2..N 轮的
    请求只有 Engine 能构造，因此编排层不得在调用前额外分发一次。
    """
    current = replace(
        request,
        messages=tuple(messages),
        timeout_ms=_model_timeout(request, deps, ledger),
    )
    outcome = _checked(
        await deps.hooks.dispatch(
            HookContext(
                HookName.BEFORE_MODEL_REQUEST,
                correlation=current.correlation,
                request=current,
            )
        ),
        HookName.BEFORE_MODEL_REQUEST,
        _REQUEST_ACTIONS,
    )
    if outcome.action is HookAction.REPLACE and outcome.request is not None:
        return validated_model_request(current, outcome.request, remaining_ms=ledger.remaining_ms())
    return current


def _model_timeout(request: ModelRequest, deps: EngineDeps, ledger: BudgetLedger) -> int:
    """本轮模型请求的超时：调用方给的值（或 turn 总预算）与 turn 剩余时间取小。

    不让单次请求的超时超过 turn 剩余时间——否则一次 300 秒的模型调用能把只剩 5 秒的 turn
    拖成 305 秒。`ModelRequest.timeout_ms` 允许 0（= 供应商默认），这里一律给正数。
    """
    ceiling = request.timeout_ms or deps.limits.turn_timeout_ms
    return max(1, min(ceiling, ledger.remaining_ms()))


def _checked(outcome: HookOutcome, hook: HookName, allowed: frozenset[HookAction]) -> HookOutcome:
    """校验 Hook 的处置对这个 Hook 合法。

    不合法就抛（→ `TurnFailed`）而不是忽略：一个在 `before_model_request` 上返回 `REJECT` 的
    插件，作者认为自己拒掉了这个 turn。静默忽略会让它以为生效了，而用户看到的是「什么都
    没发生」——`HookOutcome` 对 `REJECT`/`BLOCK` 强制要求 `reason`，就是不想有这种结局。
    """
    if outcome.action not in allowed:
        raise KaryviaError(
            ErrorCode.KERNEL_INVARIANT_VIOLATED,
            "Hook 返回了该 Hook 不支持的处置。",
            detail={
                "hook": hook.value,
                "action": outcome.action.value,
                "allowed": sorted(action.value for action in allowed),
            },
        )
    return outcome


async def _invoke_one(
    call: ToolCall,
    specs: Mapping[str, ToolSpec],
    deps: EngineDeps,
    cancel: CancelToken,
    correlation: Correlation,
    ledger: BudgetLedger,
) -> _Attempt:
    """走完一次工具调用的全流程。四条路径里只有一条真的执行。

    未知工具在这里拦下而不是交给 invoker：本轮生效的工具集就是 `request.tools`，
    「模型报了一个不在里面的名字」是 Engine 唯一有权判定的事。它被折成对话式工具结果，
    让模型下一轮有机会修正名字，而不是把整个 turn 判为系统故障。
    """
    if call.name not in specs:
        return _Attempt(unknown_tool_result(call, tuple(specs)), ToolDisposition.UNKNOWN_TOOL)
    if cancel.requested:
        # 查而不抛：批次中途取消时，前面已执行的工具必须保留真实结果。在这里抛异常，
        # 那些结果就只能靠 except 里的局部变量抢救——检查点 6 才是统一的抛出点（§6.4）。
        return _Attempt(
            skipped_result(call, cancel.reason or CancelReason.USER), ToolDisposition.SKIPPED
        )

    invocation = deps.tools.prepare(
        call,
        correlation=correlation,
        timeout_ms=max(1, min(deps.limits.tool_timeout_ms, ledger.remaining_ms())),
    )
    outcome = _checked(
        await deps.hooks.dispatch(
            HookContext(
                HookName.BEFORE_TOOL_CALL, correlation=correlation, invocation=invocation
            )
        ),
        HookName.BEFORE_TOOL_CALL,
        _TOOL_ACTIONS,
    )
    if outcome.action is HookAction.BLOCK:
        return _Attempt(blocked_result(call, outcome.reason), ToolDisposition.BLOCKED)
    if outcome.action is HookAction.REPLACE and outcome.invocation is not None:
        invocation = validated_tool_invocation(invocation, outcome.invocation)

    try:
        result = await deps.tools.invoke(invocation, cancel.child())
    # `invoke` 约定不抛；逸出的异常折成一条结果回给模型，不得打掉整个 turn。
    except Exception as error:
        result = escaped_result(call, error)

    after = _checked(
        await deps.hooks.dispatch(
            HookContext(
                HookName.AFTER_TOOL_CALL,
                correlation=correlation,
                invocation=invocation,
                result=result,
            )
        ),
        HookName.AFTER_TOOL_CALL,
        _REQUEST_ACTIONS,
    )
    if after.action is HookAction.REPLACE and after.result is not None:
        result = after.result
    return _Attempt(result, ToolDisposition.EXECUTED)
