"""Engine 依赖面：`EngineDeps` 与两个 kernel-local 调度 Protocol（技术方案 §6.2、§6.6）。

职责：声明 engine 与外界交互的**唯一**通道——`EngineDeps` 四个槽，`ToolInvoker` 与
`HookDispatcher` 的方法签名、异常约定与取消语义，以及 engine 负责分发的 4 个 `HookName`。
不负责：提供任何实现、决定 Hook 顺序与超时、查 registry、等待不可取消的工具——
实现在 `D14` 的装配侧与 `D16` 的 Host；本模块只有签名与常量，不含 IO。

**为什么这两个 Protocol 在 kernel 而不是 contracts**：`contracts/protocols.py` 的 9 个是
**单能力**契约（一个工具、一个 Hook handler），是插件要实现的形状；这里的两个是**批量调度器**
（一批工具、一次 Hook 分发的归并结果），是 Kernel 内部的装配缝。把调度器写进 contracts，
等于要求插件去理解「一批工具怎么并发」——那正是 engine 存在的理由。

**三条已经定死的职责边界**（每条都影响正确性，不只是风格）：

1. **`ToolSpec` 查找留在 engine**：本轮生效的工具集就是 `ModelRequest.tools`，engine 进循环时
   建一次索引。再给 invoker 一个 `spec_for()`，就有了两个可能不一致的工具集，「模型看得见但
   调度不认识」会以「本该并发的批次被当成屏障」这种静默形式发生。
2. **超时 engine 定、invoker 执行**：`ToolInvocation.timeout_ms` 必填且为正，而 engine 才知道
   turn 还剩多少时间（`BudgetLedger.remaining_ms()`）——一个 120 秒的工具不该把只剩 5 秒的 turn
   拖成 125 秒。执行侧持有协程，宽限期赛跑与孤儿任务登记只能在那里做。
3. **结果截断在 engine，且在 `after_tool_call` 之后**：预算住在 `EngineDeps.limits`；更硬的一条是
   `after_tool_call` 是拦截器、可以 `REPLACE` 掉整个结果，先截后钩等于给插件留一条绕过预算的路。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

from nucleamind.contracts import (
    CancelSignal,
    Correlation,
    HookContext,
    HookName,
    HookOutcome,
    ModelProvider,
    ToolCall,
    ToolInvocation,
    ToolResult,
)

from .limits import TurnLimits

__all__ = [
    "ENGINE_HOOKS",
    "EngineDeps",
    "HookDispatcher",
    "ToolInvoker",
]

#: engine 负责分发的 4 个 Hook。恰好是 `HOOK_REQUIRED_SLOTS` 只要求
#: `{correlation, request, response, invocation, result}` 的那几个——engine 拿得出的槽位
#: 只有这些。`turn_start` 要 `message`、`context_assemble` 要 `fragments`、`turn_end` 要
#: `outcome`（而 `TurnOutcome` 需要两个带时区的时间戳，engine 没有时钟），全部归 orchestrator。
#: 这条分工有对照测试盯着。
#:
#: ⚠️ `BEFORE_MODEL_REQUEST` 由 engine **每轮**分发，而技术方案 §10.2 第 9 步把它画在进 engine
#: 之前：第一轮两者重合，第 2..N 轮的请求只有 engine 造得出来。`D14` 不得在调用 engine 前
#: 再分发一次，否则第一轮触发两遍、插件的累积式改写被叠加两次。
ENGINE_HOOKS: Final[frozenset[HookName]] = frozenset(
    {
        HookName.BEFORE_MODEL_REQUEST,
        HookName.AFTER_MODEL_RESPONSE,
        HookName.BEFORE_TOOL_CALL,
        HookName.AFTER_TOOL_CALL,
    }
)


@runtime_checkable
class ToolInvoker(Protocol):
    """把一次 `ToolCall` 变成 `ToolResult` 的执行面（技术方案 §6.2 的 `tools` 槽）。

    实现方负责 schema 校验、调用预算校验与真正的执行（`KER-004`）。engine 只负责调度
    顺序、超时数值与结果截断——它不认识 JSON Schema。
    """

    def prepare(
        self, call: ToolCall, *, correlation: Correlation, timeout_ms: int
    ) -> ToolInvocation:
        """把一次调用补齐成可执行的调用上下文。

        engine 调用它是为了拿到 `before_tool_call` Hook 的必填槽（`HookContext.invocation`）——
        Hook 要能改写工具参数，就必须在执行**之前**看到一个完整的 `ToolInvocation`。

        **异常约定**：约定不抛。工具不存在的情况不会到达这里（engine 先按
        `ModelRequest.tools` 拦下并合成一条 tool 消息回给模型）。真抛了也会被 engine 兜成
        `TurnFailed`，但那时诊断信息只剩异常类型名。
        **取消语义**：纯策略查询，不涉及取消。
        """
        ...

    async def invoke(self, invocation: ToolInvocation, cancel: CancelSignal) -> ToolResult:
        """执行一次调用，**必须**在 `invocation.timeout_ms + tool_cancel_grace_ms` 内返回。

        **异常约定**：约定不抛——schema 不合、超时、handler 逸出的异常一律折成
        `ToolResult(ok=False, error=...)`。逸出的异常 engine 会兜成
        `side_effect=UNKNOWN`，但那样就丢掉了执行侧本来能给出的副作用判断。
        **取消语义**：`cancel` 是本次调用专属的子令牌（`CancelToken.child()`）。宽限期等待与
        孤儿任务登记（`EDG-407`、`EDG-104`）在本实现内完成：engine 里没有「实例」这个概念，
        孤儿任务表不可能在那一层，因此 engine 不再加第二层超时。
        """
        ...


@runtime_checkable
class HookDispatcher(Protocol):
    """一次 Hook 分发的归并结果（技术方案 §6.6）。"""

    async def dispatch(self, context: HookContext) -> HookOutcome:
        """分发一个 Hook，返回**已归并**的处置。

        排序（`priority`, `plugin_id`）、每 handler 超时、多个拦截器的累积式改写、
        插件失败的隔离全在实现内。观察者一律返回 `HookAction.CONTINUE`——它们的返回值
        按 §6.6 被忽略，engine 不该自己去判断这次分发的是不是观察者。

        **异常约定**：插件异常被实现折成诊断并跳过，不影响当前 turn。
        **取消语义**：不接受 `CancelSignal`。Hook 有自己的独立超时（观察者整批 2000ms、
        拦截器每个 5000ms），与 turn 取消是两件事。
        """
        ...


@dataclass(frozen=True, slots=True)
class EngineDeps:
    """engine 与外界交互的全部通道，恰好四个槽（技术方案 §6.2）。

    「只通过 `deps` 与外界交互」这条不变量是可检查的：`engine.py` 的模块级 import 清单里
    不出现 `os` / `pathlib` / `socket`，有测试盯着。多加一个槽就要多问一遍「这个东西为什么
    不是 orchestrator 的事」——四个槽是 §6.2 的原文，不扩。
    """

    model: ModelProvider
    tools: ToolInvoker
    hooks: HookDispatcher
    limits: TurnLimits
