"""Hook 调度：观察者并发隔离、拦截器顺序累积。

职责：把一组 `HookBinding` 归并成 `deps.HookDispatcher` 要的那**一个** `HookOutcome`——
观察者并发执行、整批超时、失败一律隔离；拦截器按 `(priority, provider, name)` 顺序执行、
每个独立超时、`REPLACE` 累积、`REJECT`/`BLOCK` 短路；失败上报后跳过。
不负责：发布事件、认识 `EventBus`、决定某个 Hook 在什么时候被分发、判断 Hook 的处置对
业务是否合理——分发时机在 `engine.py`（4 个）与 `orchestrator.py`（3 个），失败的去向由
注入的 `on_failure` 决定；本模块不做任何 IO。

**为什么 `on_failure` 是注入的回调而不是 `EventBus`**：本模块只需要「把这条失败交出去」，
不需要知道它变成 `plugin.failed` 还是一行日志。注入回调让 hooks 不 import
`kernel/observability/`，测试里用一个 list 就能断言隔离行为，而 orchestrator 传的正是
`bus.publish(PLUGIN_FAILED, ...)`。

观察者与拦截器的异常都被隔离并上报。拦截器只有返回合法的 `REJECT` / `BLOCK` 才能改变
turn 结果，插件自身故障不能中断 turn。

**顺序是 `(priority, provider, name)` 而不是注册顺序**。前两项是 §6.6 的原文，
末尾补 `name` 是因为同一个插件可以注册多个同优先级的 Hook——少了它，报告与行为都会随
字典插入顺序漂移，`test_interceptor_order_is_deterministic` 也就断言不了什么。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Final

from karyvia.contracts import (
    CapabilityKind,
    CapabilityRef,
    ErrorCode,
    HookAction,
    HookContext,
    HookHandler,
    HookKind,
    HookName,
    HookOutcome,
    KaryviaError,
    ProviderId,
    provider_sort_key,
)

from ..registry import CapabilityRegistry

__all__ = [
    "DEFAULT_INTERCEPTOR_TIMEOUT_MS",
    "DEFAULT_OBSERVER_TIMEOUT_MS",
    "HOOK_ACTIONS",
    "HOOK_REPLACE_SLOTS",
    "HookBinding",
    "HookRouter",
    "RegisteredHook",
    "bindings_from",
]

#: 观察者的**整批**超时（§6.6）。它们并发执行且返回值被忽略，只要整体拖不动 turn 就行。
DEFAULT_OBSERVER_TIMEOUT_MS: Final = 2_000

#: 拦截器的**每个 handler** 超时（§6.6）。它们串行且能改流水线，一个慢 handler 会连累
#: 它后面的全部，因此上限必须逐个算。
DEFAULT_INTERCEPTOR_TIMEOUT_MS: Final = 5_000

#: 每个 Hook 允许的处置。§6.6「返回语义」列的可执行形态：`REJECT` 只属于 `turn_start`、
#: `BLOCK` 只属于 `before_tool_call`。用错的 handler 会被当作插件故障隔离掉，而不是静默
#: 忽略——静默忽略会让作者以为自己拒掉了这个 turn，用户看到的却是「什么都没发生」。
HOOK_ACTIONS: Final[dict[HookName, frozenset[HookAction]]] = {
    HookName.TURN_START: frozenset({HookAction.CONTINUE, HookAction.REJECT}),
    HookName.CONTEXT_ASSEMBLE: frozenset({HookAction.CONTINUE, HookAction.REPLACE}),
    HookName.BEFORE_MODEL_REQUEST: frozenset({HookAction.CONTINUE, HookAction.REPLACE}),
    HookName.BEFORE_TOOL_CALL: frozenset(
        {HookAction.CONTINUE, HookAction.REPLACE, HookAction.BLOCK}
    ),
    HookName.AFTER_TOOL_CALL: frozenset({HookAction.CONTINUE, HookAction.REPLACE}),
}

#: `REPLACE` 对每个拦截器意味着改哪一个槽（`HookAction.REPLACE` 的 docstring）。
#: 累积式改写就是「把上一个 handler 的载荷灌回 `HookContext` 的这个槽再给下一个」。
HOOK_REPLACE_SLOTS: Final[dict[HookName, str]] = {
    HookName.CONTEXT_ASSEMBLE: "fragments",
    HookName.BEFORE_MODEL_REQUEST: "request",
    HookName.BEFORE_TOOL_CALL: "invocation",
    HookName.AFTER_TOOL_CALL: "result",
}

_CONTINUE: Final = HookOutcome(HookAction.CONTINUE)


@dataclass(frozen=True, slots=True)
class RegisteredHook:
    """`CapabilityKind.HOOK` 的注册载荷形状。"""

    hook: HookName
    handler: HookHandler


@dataclass(frozen=True, slots=True)
class HookBinding:
    """一个已生效的 Hook 实现，外加排序与诊断需要的元数据。"""

    hook: HookName
    handler: HookHandler
    provider: ProviderId
    name: str
    priority: int = 0

    @property
    def sort_key(self) -> tuple[int, str, str]:
        """`(priority, provider 字典序, 能力名)`，与 `Registration.sort_key` 同构。"""
        return (self.priority, provider_sort_key(self.provider), self.name)

    @property
    def ref(self) -> CapabilityRef:
        """诊断用的能力标识，挂到 `KaryviaError.capability` 上。"""
        return CapabilityRef(kind=CapabilityKind.HOOK, name=self.name, provider=self.provider)


def bindings_from(registry: CapabilityRegistry) -> tuple[HookBinding, ...]:
    """从已冻结的 registry 取出全部生效的 Hook 绑定。

    载荷形状不对即抛 `KERNEL_INVARIANT_VIOLATED`：`HOOK` 是 MULTI 类能力，注册期不判冲突，
    形状错误只能在这里当场发现——留到分发时才发现，意味着一次 turn 跑到一半才炸。

    **异常约定**：registry 未冻结或载荷形状不对时抛；正常路径不抛。
    """
    bindings: list[HookBinding] = []
    for registration in registry.of_kind(CapabilityKind.HOOK):
        payload = registration.payload
        if not isinstance(payload, RegisteredHook):
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "HOOK 能力的注册载荷必须是 RegisteredHook。",
                detail={"capability": registration.ref.target},
                capability=registration.ref,
            )
        bindings.append(
            HookBinding(
                hook=payload.hook,
                handler=payload.handler,
                provider=registration.ref.provider,
                name=registration.ref.name,
                priority=registration.priority,
            )
        )
    return tuple(bindings)


class HookRouter:
    """一次 Hook 分发的归并实现，结构化满足 `deps.HookDispatcher`。"""

    def __init__(
        self,
        bindings: Iterable[HookBinding] = (),
        *,
        observer_timeout_ms: int = DEFAULT_OBSERVER_TIMEOUT_MS,
        interceptor_timeout_ms: int = DEFAULT_INTERCEPTOR_TIMEOUT_MS,
        on_failure: Callable[[KaryviaError], None] | None = None,
    ) -> None:
        by_hook: dict[HookName, list[HookBinding]] = {}
        for binding in bindings:
            by_hook.setdefault(binding.hook, []).append(binding)
        self._by_hook = {
            hook: tuple(sorted(items, key=lambda item: item.sort_key))
            for hook, items in by_hook.items()
        }
        self._observer_timeout_ms = observer_timeout_ms
        self._interceptor_timeout_ms = interceptor_timeout_ms
        self._on_failure = on_failure

    def bindings_for(self, hook: HookName) -> tuple[HookBinding, ...]:
        """该 Hook 上已排序的绑定，供诊断与测试读取。"""
        return self._by_hook.get(hook, ())

    async def dispatch(self, context: HookContext) -> HookOutcome:
        """分发一个 Hook，返回**已归并**的处置。

        **异常约定**：handler 失败或返回非法结果时上报并跳过；其余 handler 继续执行。
        `KaryviaError` 原样上报，其余异常包成 `PLUGIN_HOOK_FAILED`。
        **取消语义**：不接受 `CancelSignal`。Hook 有自己的独立超时，与 turn 取消是两件事。
        """
        bindings = self._by_hook.get(context.hook, ())
        if not bindings:
            return _CONTINUE
        if context.kind is HookKind.OBSERVER:
            await self._dispatch_observers(bindings, context)
            return _CONTINUE
        return await self._dispatch_interceptors(bindings, context)

    # ------------------------------------------------------------------ 内部

    async def _dispatch_observers(
        self, bindings: Sequence[HookBinding], context: HookContext
    ) -> None:
        """并发跑完全部观察者，整批超时，失败只上报不影响 turn。"""
        tasks = {
            asyncio.ensure_future(binding.handler.handle(context)): binding
            for binding in bindings
        }
        done, pending = await asyncio.wait(tasks, timeout=self._observer_timeout_ms / 1000)
        for task in pending:
            task.cancel()
        if pending:
            # 先等被取消的任务真正结束，再上报：否则事件循环关闭时会留下
            # "Task was destroyed but it is pending" 的噪声，掩盖真正的诊断。
            await asyncio.gather(*pending, return_exceptions=True)
            for task in pending:
                self._report(self._timeout_error(tasks[task], self._observer_timeout_ms))
        for task in done:
            error = task.exception()
            if error is not None and isinstance(error, Exception):
                self._report(self._failure(tasks[task], error))

    async def _dispatch_interceptors(
        self, bindings: Sequence[HookBinding], context: HookContext
    ) -> HookOutcome:
        """顺序跑拦截器：`REPLACE` 累积回灌，`REJECT`/`BLOCK` 首个即短路。"""
        slot = HOOK_REPLACE_SLOTS.get(context.hook)
        current = context
        replaced = False
        for binding in bindings:
            outcome = await self._call_interceptor(binding, current)
            if outcome is None or outcome.action is HookAction.CONTINUE:
                continue
            if outcome.action in (HookAction.REJECT, HookAction.BLOCK):
                return outcome
            payload = getattr(outcome, slot) if slot is not None else None
            if slot is None or not payload:
                self._reject_outcome(binding, current, outcome)
                continue
            current = replace(current, **{slot: payload})
            replaced = True
        if not replaced or slot is None:
            return _CONTINUE
        return HookOutcome(HookAction.REPLACE, **{slot: getattr(current, slot)})

    async def _call_interceptor(
        self, binding: HookBinding, context: HookContext
    ) -> HookOutcome | None:
        """跑一个拦截器并校验它的处置；失败上报后跳过。"""
        try:
            outcome = await asyncio.wait_for(
                binding.handler.handle(context), timeout=self._interceptor_timeout_ms / 1000
            )
        except TimeoutError:
            self._settle(binding, self._timeout_error(binding, self._interceptor_timeout_ms))
            return None
        # 捕 Exception 而不是 BaseException：`CancelledError` 是任务本身被杀，
        # 与「这个插件坏了」不是一回事（engine 同一条理由）。
        except Exception as error:
            self._settle(binding, self._failure(binding, error))
            return None
        if outcome is not None and outcome.action not in HOOK_ACTIONS.get(
            context.hook, frozenset()
        ):
            self._reject_outcome(binding, context, outcome)
            return None
        return outcome

    def _reject_outcome(
        self, binding: HookBinding, context: HookContext, outcome: HookOutcome
    ) -> None:
        """handler 返回了这个 Hook 用不上的处置或载荷。

        当作插件故障而不是忽略：`HookOutcome` 的载荷四选一就是为了让「填错即静默失效」
        不可能发生，在归并这一层放行等于把那条设计作废。
        """
        self._settle(
            binding,
            KaryviaError(
                ErrorCode.PLUGIN_HOOK_FAILED,
                "Hook handler 返回了该 Hook 不支持的处置或载荷。",
                detail={
                    "hook": context.hook.value,
                    "provider": str(binding.provider),
                    "action": outcome.action.value,
                    "expects": HOOK_REPLACE_SLOTS.get(context.hook, ""),
                },
                capability=binding.ref,
            ),
        )

    def _settle(self, binding: HookBinding, error: KaryviaError) -> None:
        """拦截器失败的归宿：上报后继续。"""
        self._report(error)

    def _report(self, error: KaryviaError) -> None:
        if self._on_failure is not None:
            self._on_failure(error)

    def _failure(self, binding: HookBinding, error: Exception) -> KaryviaError:
        """把 handler 逸出的异常折成一条可上报的错误。

        异常**消息**不进 detail，只留类型名：第三方 handler 的异常文本可能带着凭据
        。
        """
        if isinstance(error, KaryviaError):
            return error
        return KaryviaError(
            ErrorCode.PLUGIN_HOOK_FAILED,
            "Hook handler 抛出了异常，已按插件故障隔离。",
            detail={
                "hook": binding.hook.value,
                "provider": str(binding.provider),
                "exception": type(error).__name__,
            },
            capability=binding.ref,
        )

    def _timeout_error(self, binding: HookBinding, timeout_ms: int) -> KaryviaError:
        return KaryviaError(
            ErrorCode.TIMEOUT_HOOK,
            "Hook handler 超时，已按插件故障隔离。",
            detail={
                "hook": binding.hook.value,
                "provider": str(binding.provider),
                "timeout_ms": timeout_ms,
            },
            capability=binding.ref,
        )
