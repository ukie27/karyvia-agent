"""六个单值能力的注册载荷与取回函数（技术方案 §6.1）。

职责：为 `MODEL` / `SESSION_STORE` / `CHANNEL` / `MEMORY` / `CLI_ENTRY` /
`TURN_COMPACTOR` 定义注册载荷（`Registered*`）和从冻结 Registry 取回生效实现的函数，
并在取回时核对载荷形状。
不负责：注册（`host.py`）、判定谁生效（`kernel/registry/resolution.py`）、使用这些实现
（`runtime/` 与 `kernel/turn/`）。本模块不做 IO、不认识 manifest。

**为什么必须包一层 dataclass**，哪怕字段只有一个：取回函数要把 `Registration.payload`
（类型是 `object`）窄化成具体类型，而 `contracts/protocols.py` 已经写死
「`runtime_checkable` 只用于诊断输出，永不参与控制流」——拿 `isinstance(payload,
ModelProvider)` 当分支条件正是它禁止的用法（Protocol 的 `isinstance` 只看方法名存不存在，
一个凑巧有 `describe` / `complete` / `stream` 的对象就能蒙混过去）。具体 dataclass 是唯一
既可靠又不违规的窄化手段，也让日后往载荷里加元数据（例如 Provider 声明的模型清单）
不必改动 registry 契约。这与已有四个（`RegisteredTool` / `RegisteredHook` /
`RegisteredContextProvider` / `RegisteredCommand`）完全同构。

**为什么在 `kernel/plugins/` 而不是 `kernel/turn/`**：这些 kind 不都属于 Turn，载荷形状
应与注册分派 `host.py` 放在同一个包里。否则每个消费点都要自行定义和检查 payload 形状。

**两个 SINGLETON 的取回函数返回 `| None` 而不是抛错**：Registry 查询只回答是否存在；
`BAS-009`/`EDG-108` 的“CLI 入口必须存在”由知道启动语义的组装根判定。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeAlias, TypeVar

from nucleamind.contracts import (
    CapabilityKind,
    CapabilityRef,
    Channel,
    CliEntry,
    ErrorCode,
    MemoryProvider,
    ModelProvider,
    NucleaError,
    ProviderId,
    SessionStore,
    TurnContextCompactor,
    provider_sort_key,
)
from nucleamind.kernel.registry import CapabilityRegistry, Registration

__all__ = [
    "CapabilityBinding",
    "ChannelBinding",
    "CliEntryBinding",
    "TurnContextCompactorBinding",
    "MemoryProviderBinding",
    "ModelProviderBinding",
    "RegisteredChannel",
    "RegisteredCliEntry",
    "RegisteredTurnContextCompactor",
    "RegisteredMemoryProvider",
    "RegisteredModelProvider",
    "RegisteredSessionStore",
    "SessionStoreBinding",
    "channels_from",
    "cli_entry_from",
    "turn_context_compactors_from",
    "memory_providers_from",
    "model_providers_from",
    "session_store_from",
]

_T = TypeVar("_T")
_ShapeT = TypeVar("_ShapeT")


# ------------------------------------------------------------------------------ 载荷形状


@dataclass(frozen=True, slots=True)
class RegisteredModelProvider:
    """`CapabilityKind.MODEL` 的注册载荷形状。"""

    provider: ModelProvider


@dataclass(frozen=True, slots=True)
class RegisteredSessionStore:
    """`CapabilityKind.SESSION_STORE` 的注册载荷形状（SINGLETON）。"""

    store: SessionStore


@dataclass(frozen=True, slots=True)
class RegisteredChannel:
    """`CapabilityKind.CHANNEL` 的注册载荷形状。"""

    channel: Channel


@dataclass(frozen=True, slots=True)
class RegisteredMemoryProvider:
    """`CapabilityKind.MEMORY` 的注册载荷形状。"""

    provider: MemoryProvider


@dataclass(frozen=True, slots=True)
class RegisteredCliEntry:
    """`CapabilityKind.CLI_ENTRY` 的注册载荷形状（SINGLETON）。"""

    entry: CliEntry


@dataclass(frozen=True, slots=True)
class RegisteredTurnContextCompactor:
    """`CapabilityKind.TURN_COMPACTOR` 的注册载荷形状。"""

    compactor: TurnContextCompactor


# -------------------------------------------------------------------------------- 绑定


@dataclass(frozen=True, slots=True)
class CapabilityBinding(Generic[_T]):
    """一个已生效的单值能力：实现本身 + 排序与诊断需要的元数据。

    五个 kind 共用一个泛型类而不是各写一遍：它们的元数据完全相同（谁提供的、叫什么、
    优先级多少），五份同构的 dataclass 只会让「改一处忘四处」有五倍的机会。这与
    `HookBinding` / `ContextProviderBinding` 各自独立并不矛盾——前者有独有的 `hook`
    字段，后者承载上下文提供者本身。
    """

    kind: CapabilityKind
    value: _T
    owner: ProviderId
    name: str
    priority: int = 0

    @property
    def sort_key(self) -> tuple[int, str, str]:
        """`(priority, provider 字典序, 能力名)`，与 `Registration.sort_key` 同构。"""
        return (self.priority, provider_sort_key(self.owner), self.name)

    @property
    def ref(self) -> CapabilityRef:
        """诊断用的能力标识，挂到 `NucleaError.capability` 上（`PLG-006`）。"""
        return CapabilityRef(kind=self.kind, name=self.name, provider=self.owner)


ModelProviderBinding: TypeAlias = CapabilityBinding[ModelProvider]
SessionStoreBinding: TypeAlias = CapabilityBinding[SessionStore]
ChannelBinding: TypeAlias = CapabilityBinding[Channel]
MemoryProviderBinding: TypeAlias = CapabilityBinding[MemoryProvider]
CliEntryBinding: TypeAlias = CapabilityBinding[CliEntry]
TurnContextCompactorBinding: TypeAlias = CapabilityBinding[TurnContextCompactor]


# ------------------------------------------------------------------------------ 取回


def _unwrap(
    registration: Registration,
    kind: CapabilityKind,
    shape: type[_ShapeT],
    take: Callable[[_ShapeT], _T],
) -> CapabilityBinding[_T]:
    """核对一条登记的载荷形状并包成绑定。形状不对即 `KERNEL_INVARIANT_VIOLATED`。

    错误信息在这里写一次而不是五次：五个 kind 的失败模式完全相同，各写一份的唯一后果是
    有四份会慢慢和第五份说得不一样。
    """
    payload = registration.payload
    if not isinstance(payload, shape):
        raise NucleaError(
            ErrorCode.KERNEL_INVARIANT_VIOLATED,
            f"{kind.value} 能力的注册载荷必须是 {shape.__name__}。",
            detail={"capability": registration.ref.target, "actual": type(payload).__name__},
            capability=registration.ref,
        )
    return CapabilityBinding(
        kind=kind,
        value=take(payload),
        owner=registration.ref.provider,
        name=registration.ref.name,
        priority=registration.priority,
    )


def _bindings_of(
    registry: CapabilityRegistry,
    kind: CapabilityKind,
    shape: type[_ShapeT],
    take: Callable[[_ShapeT], _T],
) -> tuple[CapabilityBinding[_T], ...]:
    return tuple(
        _unwrap(registration, kind, shape, take) for registration in registry.of_kind(kind)
    )


def _single(
    registry: CapabilityRegistry,
    kind: CapabilityKind,
    shape: type[_ShapeT],
    take: Callable[[_ShapeT], _T],
) -> CapabilityBinding[_T] | None:
    """SINGLETON 的取回：至多一项，没有就是 `None`。

    「有两项」在这里不可能发生——SINGLETON 的多实现冲突由 `resolution.py` 判定，冲突各方
    都不会进生效集合。真出现两项说明解析被绕过了，那是 kernel 不变量被破坏。
    """
    bindings = _bindings_of(registry, kind, shape, take)
    if len(bindings) > 1:
        raise NucleaError(
            ErrorCode.KERNEL_INVARIANT_VIOLATED,
            f"{kind.value} 是 SINGLETON，生效集合里不应出现多个实现。",
            detail={"kind": kind.value, "found": [binding.ref.target for binding in bindings]},
        )
    return bindings[0] if bindings else None


def model_providers_from(registry: CapabilityRegistry) -> tuple[ModelProviderBinding, ...]:
    """取回全部生效的模型供应商（MULTI_UNIQUE，由配置选用哪一个）。

    **异常约定**：registry 未冻结由 registry 自己抛；载荷形状不对抛
    `KERNEL_INVARIANT_VIOLATED`。
    """
    return _bindings_of(
        registry, CapabilityKind.MODEL, RegisteredModelProvider, lambda item: item.provider
    )


def channels_from(registry: CapabilityRegistry) -> tuple[ChannelBinding, ...]:
    """取回全部生效的 Channel（MULTI_UNIQUE）。**异常约定**同 `model_providers_from()`。"""
    return _bindings_of(
        registry, CapabilityKind.CHANNEL, RegisteredChannel, lambda item: item.channel
    )


def memory_providers_from(registry: CapabilityRegistry) -> tuple[MemoryProviderBinding, ...]:
    """取回全部生效的 Memory 实现（MULTI_UNIQUE，`MEM-003` 允许并存多个具名后端）。

    **异常约定**同 `model_providers_from()`。
    """
    return _bindings_of(
        registry, CapabilityKind.MEMORY, RegisteredMemoryProvider, lambda item: item.provider
    )


def turn_context_compactors_from(
    registry: CapabilityRegistry,
) -> tuple[TurnContextCompactorBinding, ...]:
    """取回全部生效的 Turn 内上下文压缩策略（MULTI_UNIQUE）。"""
    return _bindings_of(
        registry,
        CapabilityKind.TURN_COMPACTOR,
        RegisteredTurnContextCompactor,
        lambda item: item.compactor,
    )


def session_store_from(registry: CapabilityRegistry) -> SessionStoreBinding | None:
    """取回唯一生效的会话存储（SINGLETON）；没有实现时返回 `None`。

    **异常约定**同 `model_providers_from()`，另加「生效集合里有多个」时抛
    `KERNEL_INVARIANT_VIOLATED`。
    """
    return _single(
        registry, CapabilityKind.SESSION_STORE, RegisteredSessionStore, lambda item: item.store
    )


def cli_entry_from(registry: CapabilityRegistry) -> CliEntryBinding | None:
    """取回唯一生效的 CLI 入口（SINGLETON）；没有实现时返回 `None`。

    返回 `None` 不代表这是可接受的终局：`BAS-009`/`EDG-108` 要求 CLI 入口始终存在，
    但那条判定（以及覆盖失败时回落内建）属于组装根。本层只如实回答有没有。

    **异常约定**同 `session_store_from()`。
    """
    return _single(registry, CapabilityKind.CLI_ENTRY, RegisteredCliEntry, lambda item: item.entry)
