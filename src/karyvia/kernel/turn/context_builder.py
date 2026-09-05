"""Context 组装：Provider 调度、过滤、trust 放置与最终消息渲染。

职责：并发调用全部生效的 `ContextProvider`（各自独立超时、失败隔离），分发
`context_assemble` 拦截器，并按 `trust` 决定片段的放置位置，产出 `ModelMessage` 序列。
不负责：调用模型、决定谁是 Provider（Registry 说了算）、持久化压缩历史
（`compaction.py` 在本函数返回后协调）、请求预算判断与压缩（模型请求包装层负责）、
**长期记忆的召回策略与降级**
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
4. **先渲染再计量**。`ContextFragment.estimated_tokens` 是插件提供的提示值，不再作为
   Kernel 的预算真相；最终消息和完整请求都使用 `request_size.py` 的同一把尺。

本模块不做确定性硬裁剪。超预算请求统一进入请求级压缩；其中能精确映射到 Session
连续前缀的摘要在 Turn 收口时持久化，其余摘要只服务当前 Turn。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from karyvia.contracts import (
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
    KaryviaError,
    ModelMessage,
    ProviderId,
    Role,
    Sensitivity,
    SessionSnapshot,
    TrustLevel,
    provider_sort_key,
)

from ..registry import CapabilityRegistry
from .deps import HookDispatcher
from .memory import MemoryRecall
from .message_projection import render_message_content
from .request_size import TokenAccounting, estimate_tokens

__all__ = [
    "DEFAULT_CONTEXT_PROVIDER_TIMEOUT_MS",
    "AssembledContext",
    "ContextProviderBinding",
    "DroppedFragment",
    "ReplayedMessage",
    "RegisteredContextProvider",
    "assemble",
    "context_providers_from",
    "estimate_tokens",
    "replay_history",
    "replay_messages",
]

#: 单个 Context Provider 的独立超时（§10.2 第 7 步 b）。
DEFAULT_CONTEXT_PROVIDER_TIMEOUT_MS: Final = 3_000


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
    session_history: tuple[ReplayedMessage, ...]
    fragments: tuple[ContextFragment, ...]
    dropped: tuple[DroppedFragment, ...]
    estimated_tokens: int


@dataclass(frozen=True, slots=True)
class ReplayedMessage:
    """一条进入模型的 Session 消息及其在持久化记录中的结束水位。"""

    message: ModelMessage
    through: int


def context_providers_from(registry: CapabilityRegistry) -> tuple[ContextProviderBinding, ...]:
    """从已冻结的 registry 取出全部生效的 Context Provider，按 `(priority, provider, name)` 排序。

    **异常约定**：registry 未冻结或载荷形状不对时抛 `KERNEL_INVARIANT_VIOLATED`。
    """
    bindings: list[ContextProviderBinding] = []
    for registration in registry.of_kind(CapabilityKind.CONTEXT):
        payload = registration.payload
        if not isinstance(payload, RegisteredContextProvider):
            raise KaryviaError(
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


def replay_history(snapshot: SessionSnapshot) -> tuple[ReplayedMessage, ...]:
    """把会话历史投影成模型消息（`EDG-305`：投影可以变，持久化格式不变）。

    **只取 user / assistant / system 且正文非空的记录**。`role=TOOL` 的记录被跳过：
    `SessionMessage` 不保存 assistant 的 `tool_calls`，一条没有对应调用声明的 tool 消息
    会让下一次请求在 Provider 侧直接被拒。工具往返仍然留在会话文件里（`/session` 与诊断
    要看得到），只是不参与重放。
    """
    messages: list[ReplayedMessage] = []
    for index, record in enumerate(
        snapshot.messages[snapshot.compacted_through :], start=snapshot.compacted_through
    ):
        if record.role is Role.TOOL or (not record.content and not record.attachments):
            continue
        messages.append(
            ReplayedMessage(
                message=ModelMessage(
                    role=record.role,
                    content=render_message_content(record.content, record.attachments),
                ),
                through=index + 1,
            )
        )
    return tuple(messages)


def replay_messages(snapshot: SessionSnapshot) -> tuple[ModelMessage, ...]:
    """返回 Session 的模型消息投影；水位映射由 `replay_history()` 保留。"""
    return tuple(item.message for item in replay_history(snapshot))


async def assemble(
    *,
    snapshot: SessionSnapshot,
    user_input: str,
    correlation: Correlation,
    cancel: CancelSignal,
    bindings: Sequence[ContextProviderBinding] = (),
    extra_fragments: Iterable[ContextFragment] = (),
    hooks: HookDispatcher | None = None,
    memory: MemoryRecall | None = None,
    accounting: TokenAccounting | None = None,
    now: datetime,
    provider_timeout_ms: int = DEFAULT_CONTEXT_PROVIDER_TIMEOUT_MS,
    on_failure: Callable[[KaryviaError], None] | None = None,
) -> AssembledContext:
    """走完 §10.2 第 7 步的 a–e，产出一份可直接交给 engine 的消息序列。

    `extra_fragments` 是命令注入的片段（`CommandResult.fragments`，`CMD-004`）：它们与
    Provider 产出的片段同批参与拦截、过滤与放置，没有旁路。

    `memory` 是长期记忆的召回（`None` = 不启用）。它产出的片段**与上面两批完全同
    等**：同批拦截、同批过滤、同批放置。做成 `assemble` 的一个参数而不是让 orchestrator
    自己召回再拼进 `extra_fragments`，是因为「召回」就是上下文组装的 a 步——放在外面会让
    「片段从哪来」有两个答案，而 `orchestrator.py` 也贴着 500 行上限。**查询词是本次输入**
    （`MemoryRecall` 自己挡掉空串）；策略与降级全在 `memory.py`，这里只调它。

    **异常约定**：Provider 失败交给 `on_failure` 后跳过；空上下文直接拒绝。
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

    history = replay_history(snapshot)
    messages = _render(kept, tuple(item.message for item in history), user_input)
    meter = accounting or TokenAccounting()
    return AssembledContext(
        messages=messages,
        session_history=history,
        fragments=tuple(kept),
        dropped=tuple(dropped),
        estimated_tokens=meter.estimate_messages(messages),
    )

# --------------------------------------------------------------------------- a / b


async def _collect(
    bindings: Sequence[ContextProviderBinding],
    snapshot: SessionSnapshot,
    correlation: Correlation,
    cancel: CancelSignal,
    timeout_ms: int,
    on_failure: Callable[[KaryviaError], None] | None,
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
) -> KaryviaError:
    """把一次 Provider 失败折成可上报的错误。异常消息不进 detail（可能带凭据）。"""
    if isinstance(error, KaryviaError):
        return error
    if isinstance(error, TimeoutError):
        return KaryviaError(
            ErrorCode.TIMEOUT_HOOK,
            "Context Provider 超时。",
            detail={"provider": str(binding.owner), "timeout_ms": timeout_ms},
            capability=binding.ref,
        )
    return KaryviaError(
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


def _render(
    fragments: Sequence[ContextFragment],
    history: Sequence[ModelMessage],
    user_input: str,
) -> tuple[ModelMessage, ...]:
    """渲染为系统段 → 历史 → 上下文块 → 当前输入。"""
    system = tuple(item for item in fragments if item.trust is TrustLevel.SYSTEM)
    body = tuple(item for item in fragments if item.trust is not TrustLevel.SYSTEM)
    messages: list[ModelMessage] = []
    if system:
        messages.append(
            ModelMessage(
                role=Role.SYSTEM,
                content="\n\n".join(item.as_model_text() for item in system),
            )
        )
    messages.extend(history)
    if body:
        messages.append(
            ModelMessage(
                role=Role.USER,
                content="\n\n".join(item.as_model_text() for item in body),
            )
        )
    if user_input:
        messages.append(ModelMessage(role=Role.USER, content=user_input))
    if not messages:
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            "组装后的上下文为空，没有任何东西可以发给模型。",
        )
    return tuple(messages)
