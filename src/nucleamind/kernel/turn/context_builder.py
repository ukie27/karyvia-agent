"""Context 组装：Provider 调度、trust 放置与预算裁剪（技术方案 §10.2 第 7 步 a–e）。

职责：并发调用全部生效的 `ContextProvider`（各自独立超时、失败隔离），分发
`context_assemble` 拦截器，按 `trust` 决定片段的放置位置，并在 `context_max_tokens`
之内裁剪出一份 `ModelMessage` 序列。
不负责：调用模型、决定谁是 Provider（Registry 说了算）、持久化压缩历史
（`compaction.py` 在本函数返回后协调）、**长期记忆的召回策略与降级**
（`memory.py` 负责；这里只调用一次并把结果与其余片段同批处理）。本模块不做任何 IO，
只 await 注入进来的 Provider 与 Hook。

**四条会影响正确性的规则**：

1. **`UNTRUSTED` 的包裹不在这里做**。片段一律经 `fragment.as_model_text()` 渲染，数据块
   与固定前缀由契约层加（`CMD-005`、`EDG-306`）——组装器自己拼字符串就等于开了一条绕过
   包裹的路，而这正是 `contracts/context.py` 把包裹放在契约上的理由。
2. **`trust=SYSTEM` 是进入系统指令位置的唯一凭据**，`kind` 不参与判定。一个
   `kind=SYSTEM` 但 `trust=UNTRUSTED` 的片段（例如「从检索结果里捞到的系统提示」）只能
   落进数据块。
3. **`sensitivity=SECRET` 的片段不进模型请求**（`contracts/context.py` 的 `Sensitivity`
   docstring 写死），过期片段同理丢弃。两者都记进 `dropped`，不静默消失。
4. **拦截器在裁剪之前**。先裁后钩等于让插件在预算之外再塞东西。

**仍超限怎么办**：丢到只剩系统段与当前输入仍超预算时抛 `INPUT_TOO_LARGE`
（→ turn `FAILED`）。普通裁剪始终先产出确定结果；可选 Compactor 只在历史确实被裁
时尝试持久化摘要，失败不会改变本次已组装好的上下文。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from nucleamind.contracts import (
    CancelSignal,
    CapabilityKind,
    CapabilityRef,
    ContextFragment,
    ContextProvider,
    Correlation,
    ErrorCode,
    FragmentKind,
    FragmentScope,
    HookAction,
    HookContext,
    HookName,
    ModelInfo,
    ModelMessage,
    NucleaError,
    ProviderId,
    Role,
    Sensitivity,
    SessionSnapshot,
    TrustLevel,
    provider_sort_key,
)

from ..registry import CapabilityRegistry
from .deps import HookDispatcher
from .limits import TurnLimits
from .memory import MemoryRecall
from .message_projection import render_message_content
from .request_size import estimate_tokens

__all__ = [
    "DEFAULT_CONTEXT_PROVIDER_TIMEOUT_MS",
    "HISTORY_TRIM_PRIORITY",
    "AssembledContext",
    "ContextProviderBinding",
    "DroppedFragment",
    "RegisteredContextProvider",
    "assemble",
    "context_providers_from",
    "estimate_tokens",
    "replay_messages",
]

#: 单个 Context Provider 的独立超时（§10.2 第 7 步 b）。
DEFAULT_CONTEXT_PROVIDER_TIMEOUT_MS: Final = 3_000

#: 会话历史在裁剪序列里的优先级。取 0（= 内建基准）而不是更小的值：历史是用户资产，
#: 应当在插件片段（基准 100）之后才被丢；但它不该比内建片段更晚被丢——同优先级时先丢
#: 片段、再丢历史，因为片段下一轮还能重新产出，历史丢了就是丢了。
HISTORY_TRIM_PRIORITY: Final = 0

@dataclass(frozen=True, slots=True)
class RegisteredContextProvider:
    """`CapabilityKind.CONTEXT` 的注册载荷形状。"""

    provider: ContextProvider


@dataclass(frozen=True, slots=True)
class ContextProviderBinding:
    """一个已生效的 Context Provider，外加排序与诊断需要的元数据。"""

    provider: ContextProvider
    owner: ProviderId
    name: str
    priority: int = 0

    @property
    def sort_key(self) -> tuple[int, str, str]:
        return (self.priority, provider_sort_key(self.owner), self.name)

    @property
    def ref(self) -> CapabilityRef:
        return CapabilityRef(kind=CapabilityKind.CONTEXT, name=self.name, provider=self.owner)


@dataclass(frozen=True, slots=True)
class DroppedFragment:
    """一个没能进入请求的片段，以及原因。诊断里「它去哪了」必须查得到。"""

    fragment: ContextFragment
    reason: str


@dataclass(frozen=True, slots=True)
class AssembledContext:
    """一次组装的产物。"""

    messages: tuple[ModelMessage, ...]
    fragments: tuple[ContextFragment, ...]
    dropped: tuple[DroppedFragment, ...]
    estimated_tokens: int
    budget: int
    history_dropped: int = 0

def context_providers_from(registry: CapabilityRegistry) -> tuple[ContextProviderBinding, ...]:
    """从已冻结的 registry 取出全部生效的 Context Provider，按 `(priority, provider, name)` 排序。

    **异常约定**：registry 未冻结或载荷形状不对时抛 `KERNEL_INVARIANT_VIOLATED`。
    """
    bindings: list[ContextProviderBinding] = []
    for registration in registry.of_kind(CapabilityKind.CONTEXT):
        payload = registration.payload
        if not isinstance(payload, RegisteredContextProvider):
            raise NucleaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "CONTEXT 能力的注册载荷必须是 RegisteredContextProvider。",
                detail={"capability": registration.ref.target},
                capability=registration.ref,
            )
        bindings.append(
            ContextProviderBinding(
                provider=payload.provider,
                owner=registration.ref.provider,
                name=registration.ref.name,
                priority=registration.priority,
            )
        )
    return tuple(sorted(bindings, key=lambda item: item.sort_key))


def replay_messages(snapshot: SessionSnapshot) -> tuple[ModelMessage, ...]:
    """把会话历史投影成模型消息（`EDG-305`：投影可以变，持久化格式不变）。

    **只取 user / assistant / system 且正文非空的记录**。`role=TOOL` 的记录被跳过：
    `SessionMessage` 不保存 assistant 的 `tool_calls`，一条没有对应调用声明的 tool 消息
    会让下一次请求在 Provider 侧直接被拒。工具往返仍然留在会话文件里（`/session` 与诊断
    要看得到），只是不参与重放。
    """
    messages: list[ModelMessage] = []
    for record in snapshot.live_messages:
        if record.role is Role.TOOL or (not record.content and not record.attachments):
            continue
        messages.append(
            ModelMessage(
                role=record.role,
                content=render_message_content(record.content, record.attachments),
            )
        )
    return tuple(messages)


async def assemble(
    *,
    snapshot: SessionSnapshot,
    user_input: str,
    correlation: Correlation,
    cancel: CancelSignal,
    limits: TurnLimits,
    bindings: Sequence[ContextProviderBinding] = (),
    extra_fragments: Iterable[ContextFragment] = (),
    hooks: HookDispatcher | None = None,
    model_info: ModelInfo | None = None,
    memory: MemoryRecall | None = None,
    now: datetime,
    provider_timeout_ms: int = DEFAULT_CONTEXT_PROVIDER_TIMEOUT_MS,
    on_failure: Callable[[NucleaError], None] | None = None,
) -> AssembledContext:
    """走完 §10.2 第 7 步的 a–e，产出一份可直接交给 engine 的消息序列。

    `extra_fragments` 是命令注入的片段（`CommandResult.fragments`，`CMD-004`）：它们与
    Provider 产出的片段同批参与拦截、放置与裁剪，没有旁路。

    `memory` 是长期记忆的召回（`None` = 不启用）。它产出的片段**与上面两批完全同
    等**：同批拦截、同批放置、同批裁剪。做成 `assemble` 的一个参数而不是让 orchestrator
    自己召回再拼进 `extra_fragments`，是因为「召回」就是上下文组装的 a 步——放在外面会让
    「片段从哪来」有两个答案，而 `orchestrator.py` 也贴着 500 行上限。**查询词是本次输入**
    （`MemoryRecall` 自己挡掉空串）；策略与降级全在 `memory.py`，这里只调它。

    **异常约定**：Provider 失败交给 `on_failure` 后跳过；裁剪到底仍超预算抛
    `INPUT_TOO_LARGE`。
    记忆后端的失败按 `MemoryRecall.critical` 分叉（`MEM-003`），判定在那一侧。
    **取消语义**：`cancel` 透传给每个 Provider 与记忆后端；本函数自身不设检查点
    （检查点 1 在 orchestrator，就在调用本函数之前）。
    """
    collected = await _collect(bindings, snapshot, correlation, cancel, provider_timeout_ms, on_failure)
    recalled: tuple[ContextFragment, ...] = ()
    if memory is not None:
        recalled = await memory.recall(user_input, correlation, cancel, on_failure=on_failure)
    fragments = (*collected, *recalled, *extra_fragments)
    fragments = await _run_interceptor(fragments, correlation, hooks)

    kept: list[ContextFragment] = []
    dropped: list[DroppedFragment] = []
    for fragment in fragments:
        if fragment.sensitivity is Sensitivity.SECRET:
            dropped.append(DroppedFragment(fragment, "sensitivity"))
        elif fragment.is_expired(now):
            dropped.append(DroppedFragment(fragment, "expired"))
        else:
            kept.append(fragment)

    budget = limits.resolve_context_max_tokens(model_info)
    plan = _trim(kept, replay_messages(snapshot), user_input, budget, dropped)
    return AssembledContext(
        messages=_render(plan, user_input),
        fragments=(*plan.system, *plan.fragments),
        dropped=tuple(dropped),
        estimated_tokens=plan.tokens,
        budget=budget,
        history_dropped=plan.history_dropped,
    )


# --------------------------------------------------------------------------- a / b


async def _collect(
    bindings: Sequence[ContextProviderBinding],
    snapshot: SessionSnapshot,
    correlation: Correlation,
    cancel: CancelSignal,
    timeout_ms: int,
    on_failure: Callable[[NucleaError], None] | None,
) -> tuple[ContextFragment, ...]:
    """并发调用全部 Provider，各自独立超时；失败上报后跳过（`CTX-005`、`EDG-302`）。

    片段的顺序由 `sort_key` 决定，与谁先返回无关——`CTX-002` 要的是确定的组合顺序，
    并发只是为了不让一个慢 Provider 串起全部延迟。**排序在这里做而不是指望调用方传进来
    就是有序的**：那样一来「顺序确定」就变成了一条要人记得遵守的约定。
    """
    if not bindings:
        return ()
    ordered = sorted(bindings, key=lambda item: item.sort_key)
    results = await asyncio.gather(
        *(
            _call_one(binding, snapshot, correlation, cancel, timeout_ms)
            for binding in ordered
        ),
        return_exceptions=True,
    )
    fragments: list[ContextFragment] = []
    for binding, result in zip(ordered, results, strict=True):
        if isinstance(result, BaseException):
            error = _provider_error(binding, result, timeout_ms)
            if on_failure is not None:
                on_failure(error)
            continue
        fragments.extend(result)
    return tuple(fragments)


async def _call_one(
    binding: ContextProviderBinding,
    snapshot: SessionSnapshot,
    correlation: Correlation,
    cancel: CancelSignal,
    timeout_ms: int,
) -> tuple[ContextFragment, ...]:
    return await asyncio.wait_for(
        binding.provider.provide(snapshot, correlation, cancel), timeout=timeout_ms / 1000
    )


def _provider_error(
    binding: ContextProviderBinding, error: BaseException, timeout_ms: int
) -> NucleaError:
    """把一次 Provider 失败折成可上报的错误。异常消息不进 detail（可能带凭据）。"""
    if isinstance(error, NucleaError):
        return error
    if isinstance(error, TimeoutError):
        return NucleaError(
            ErrorCode.TIMEOUT_HOOK,
            "Context Provider 超时。",
            detail={"provider": str(binding.owner), "timeout_ms": timeout_ms},
            capability=binding.ref,
        )
    return NucleaError(
        ErrorCode.PLUGIN_HOOK_FAILED,
        "Context Provider 抛出了异常。",
        detail={"provider": str(binding.owner), "exception": type(error).__name__},
        capability=binding.ref,
    )


# ------------------------------------------------------------------------------- d


async def _run_interceptor(
    fragments: tuple[ContextFragment, ...],
    correlation: Correlation,
    hooks: HookDispatcher | None,
) -> tuple[ContextFragment, ...]:
    """`context_assemble` 拦截器（累积式）。空片段集也要分发——插件可以凭空补一段。"""
    if hooks is None:
        return fragments
    outcome = await hooks.dispatch(
        HookContext(
            HookName.CONTEXT_ASSEMBLE,
            correlation=correlation,
            fragments=fragments or (_PLACEHOLDER,),
        )
    )
    if outcome.action is HookAction.REPLACE and outcome.fragments:
        return tuple(item for item in outcome.fragments if item is not _PLACEHOLDER)
    return fragments


#: `HookContext` 要求 `context_assemble` 必须带 `fragments`（`HOOK_REQUIRED_SLOTS`），
#: 而「这一轮没有任何片段」是完全正常的状态。用一个明确的占位片段过契约校验，再在结果里
#: 摘掉它——比给契约开一个「这个槽有时可以空」的口子小得多。
_PLACEHOLDER: Final = ContextFragment(
    source="builtin:context-assemble",
    kind=FragmentKind.RUNTIME,
    content="(no fragments)",
    priority=0,
    estimated_tokens=0,
    scope=FragmentScope.SESSION,
    trust=TrustLevel.SYSTEM,
)


# ------------------------------------------------------------------------------- e


@dataclass(frozen=True, slots=True)
class _Plan:
    """裁剪后的组装计划。"""

    system: tuple[ContextFragment, ...]
    fragments: tuple[ContextFragment, ...]
    history: tuple[ModelMessage, ...]
    tokens: int
    history_dropped: int


def _trim(
    fragments: Sequence[ContextFragment],
    history: Sequence[ModelMessage],
    user_input: str,
    budget: int,
    dropped: list[DroppedFragment],
) -> _Plan:
    """按预算裁剪（`CTX-003`、`EDG-301`）。

    丢弃顺序：`priority` 逆序；同优先级内**先丢片段（按 (provider, name) 序）、再丢历史
    （从最旧）**。片段下一轮还能重新产出，历史丢了就是丢了。系统段与当前输入永不裁剪。
    """
    system = tuple(item for item in fragments if item.trust is TrustLevel.SYSTEM)
    body = [item for item in fragments if item.trust is not TrustLevel.SYSTEM]
    kept_history = list(history)
    original_history_count = len(kept_history)

    fixed = sum(item.estimated_tokens for item in system) + estimate_tokens(user_input)
    total = fixed + sum(item.estimated_tokens for item in body)
    total += sum(estimate_tokens(message.content) for message in kept_history)

    # 每次丢一个「当前最该丢的单元」：body 里 priority 最大的那个，或最旧的一条历史。
    while total > budget and (body or kept_history):
        if _fragment_goes_first(body):
            victim = max(range(len(body)), key=lambda index: (body[index].priority, index))
            fragment = body.pop(victim)
            dropped.append(DroppedFragment(fragment, "budget"))
            total -= fragment.estimated_tokens
        else:
            total -= estimate_tokens(kept_history.pop(0).content)

    if total > budget:
        raise NucleaError(
            ErrorCode.INPUT_TOO_LARGE,
            "系统指令与本次输入本身已超出上下文预算，无法在不丢失指令的前提下裁剪。",
            detail={"estimated_tokens": total, "budget": budget},
        )
    return _Plan(
        system=system,
        fragments=tuple(body),
        history=tuple(kept_history),
        tokens=total,
        history_dropped=original_history_count - len(kept_history),
    )


def _fragment_goes_first(body: Sequence[ContextFragment]) -> bool:
    """还有片段就先丢片段。

    这不是「片段比历史不重要」，而是 `HISTORY_TRIM_PRIORITY = 0` 与
    `ContextFragment.priority >= 0`（契约层保证非负）两条合起来的必然结果：任何片段的
    优先级都不低于历史，同优先级时按上面的约定片段先走。写成一个具名函数是为了让这条
    推理在代码里留下痕迹——直接写 `if body:` 六个月后就没人知道它凭什么成立。
    """
    return bool(body)


def _render(plan: _Plan, user_input: str) -> tuple[ModelMessage, ...]:
    """把计划渲染成消息序列：系统段 → 历史 → 上下文块 → 当前输入。

    非系统片段合成**一条** user 消息而不是各自一条：模型看到的是一段带来源标注的参考
    资料，而不是一串来路不明的「用户发言」。渲染一律走 `as_model_text()`。
    """
    messages: list[ModelMessage] = []
    if plan.system:
        messages.append(
            ModelMessage(
                role=Role.SYSTEM,
                content="\n\n".join(item.as_model_text() for item in plan.system),
            )
        )
    messages.extend(plan.history)
    if plan.fragments:
        messages.append(
            ModelMessage(
                role=Role.USER,
                content="\n\n".join(item.as_model_text() for item in plan.fragments),
            )
        )
    if user_input:
        messages.append(ModelMessage(role=Role.USER, content=user_input))
    if not messages:
        raise NucleaError(
            ErrorCode.INPUT_MALFORMED,
            "组装后的上下文为空，没有任何东西可以发给模型。",
        )
    return tuple(messages)
