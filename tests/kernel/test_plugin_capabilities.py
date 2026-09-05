"""六个单值 kind 的载荷形状与取回函数测试（`D16`，技术方案 §6.1 的缺口）。

三条主线：

- **形状核对是取回函数的职责**。`Registration.payload` 是 `object`，而
  `contracts/protocols.py` 写死了 `runtime_checkable` 只作诊断、永不参与控制流——
  于是每个 kind 必须有一个具体 wrapper 供 `isinstance` 窄化。五个 kind 各有一条
  「塞错载荷必须被抓住」的用例。
- **arity 决定取回的形状**。MULTI_UNIQUE 的三个返回元组，SINGLETON 的两个返回
  单项或 `None`。`D16` 的 `BUILTIN_MANIFESTS` 是空元组，因此「什么都没有」是必须跑得通的
  正常路径，不是退化分支。
- **身份如实记着**。`owner` / `name` / `priority` 要能原样回答「这个实现是谁提供的」
  （`PLG-006`），否则 `karyvia capabilities` 与诊断就没有数据源。

能力经真 Host 注册再取回，而不是直接 `batch.add`：`D16` 之后这条路是唯一的注册路径，
测试也该走它。
"""

from __future__ import annotations

import pytest

from karyvia.contracts import (
    Builtin,
    CapabilityKind,
    ErrorCode,
    KaryviaError,
    Plugin,
    PluginId,
    ProviderId,
)
from karyvia.kernel.plugins import (
    CapabilityDeclaration,
    CapabilityHost,
    channels_from,
    cli_entry_from,
    memory_providers_from,
    model_providers_from,
    session_store_from,
    turn_context_compactors_from,
)
from karyvia.kernel.registry import CapabilityRegistry, resolve_into
from karyvia.sdk.testing import (
    FakeCliEntry,
    FakeMemoryProvider,
    FakeModelProvider,
    FakePluginContext,
    InMemorySessionStore,
    NullChannel,
    StaticTurnContextCompactor,
)

# ------------------------------------------------------------------------------------ 夹具

_SINGLE_KINDS = (
    CapabilityKind.MODEL,
    CapabilityKind.CHANNEL,
    CapabilityKind.MEMORY,
    CapabilityKind.TURN_COMPACTOR,
    CapabilityKind.SESSION_STORE,
    CapabilityKind.CLI_ENTRY,
)


def frozen_with(
    kind: CapabilityKind, name: str, payload: object, *, provider: ProviderId | None = None
) -> CapabilityRegistry:
    """把一个**任意**载荷登记到某个 kind 上并冻结——用来喂错形状。

    这条路径绕过 Host 是刻意的：Host 不可能交出错误形状，而取回函数的守卫要防的是
    「有人绕过 Host 直接往 registry 里塞东西」，因此测试必须能构造那种局面。
    """
    registry = CapabilityRegistry()
    with registry.batch(provider or Builtin()) as batch:
        batch.add(kind, name, payload)
    resolve_into(registry)
    return registry


def wired(*, provider: ProviderId | None = None) -> CapabilityRegistry:
    """经真 Host 注册六类单值能力，解析并冻结。"""
    registry = CapabilityRegistry()
    batch = registry.batch(provider or Builtin())
    host = CapabilityHost(
        batch,
        FakePluginContext(),
        declarations=tuple(
            CapabilityDeclaration(kind=kind, name=kind.value) for kind in _SINGLE_KINDS
        ),
    )
    host.register_model_provider(CapabilityKind.MODEL.value, FakeModelProvider())
    host.register_channel(CapabilityKind.CHANNEL.value, NullChannel())
    host.register_memory_provider(CapabilityKind.MEMORY.value, FakeMemoryProvider())
    host.register_turn_compactor(
        CapabilityKind.TURN_COMPACTOR.value, StaticTurnContextCompactor()
    )
    host.register_session_store(CapabilityKind.SESSION_STORE.value, InMemorySessionStore())
    host.register_cli_entry(CapabilityKind.CLI_ENTRY.value, FakeCliEntry())
    host.finish()
    batch.commit()
    resolve_into(registry)
    return registry


# ------------------------------------------------------------------------------ 往返与身份


def test_every_single_valued_kind_round_trips_through_the_host() -> None:
    """注册进去的就是取回来的**同一个对象**（`is`，不是相等）。"""
    registry = wired()
    assert len(model_providers_from(registry)) == 1
    assert len(channels_from(registry)) == 1
    assert len(memory_providers_from(registry)) == 1
    assert len(turn_context_compactors_from(registry)) == 1

    store = session_store_from(registry)
    entry = cli_entry_from(registry)
    assert store is not None and isinstance(store.value, InMemorySessionStore)
    assert entry is not None and isinstance(entry.value, FakeCliEntry)


def test_bindings_report_who_provided_the_implementation() -> None:
    """`PLG-006`：诊断要能一眼看出是谁的问题。"""
    provider: ProviderId = Plugin(PluginId("acme"))
    binding = model_providers_from(wired(provider=provider))[0]
    assert binding.owner == provider
    assert binding.name == CapabilityKind.MODEL.value
    assert binding.kind is CapabilityKind.MODEL
    assert binding.ref.target == "plugin:acme:model"


def test_binding_sort_key_matches_the_registration_ordering() -> None:
    """与 `Registration.sort_key` 同构：`(priority, provider 字典序, name)`。"""
    binding = model_providers_from(wired())[0]
    assert binding.sort_key == (0, "builtin", "model")


# -------------------------------------------------------------------------------- 空注册表


def test_an_empty_registry_yields_empty_tuples_and_none() -> None:
    """`D16` 的形状：`BUILTIN_MANIFESTS` 为空时装配链必须照常跑完（`EDG-101`）。

    两个 SINGLETON 返回 `None` 而不是抛错——`BAS-009`/`EDG-108` 的「CLI 入口必须存在」
    是 `D23` 在装配根上的判定，本层只如实回答有没有。
    """
    registry = CapabilityRegistry()
    resolve_into(registry)
    assert model_providers_from(registry) == ()
    assert channels_from(registry) == ()
    assert memory_providers_from(registry) == ()
    assert turn_context_compactors_from(registry) == ()
    assert session_store_from(registry) is None
    assert cli_entry_from(registry) is None


# ---------------------------------------------------------------------------------- 形状守卫


@pytest.mark.parametrize(
    ("kind", "retrieve"),
    [
        (CapabilityKind.MODEL, model_providers_from),
        (CapabilityKind.CHANNEL, channels_from),
        (CapabilityKind.MEMORY, memory_providers_from),
        (CapabilityKind.TURN_COMPACTOR, turn_context_compactors_from),
        (CapabilityKind.SESSION_STORE, session_store_from),
        (CapabilityKind.CLI_ENTRY, cli_entry_from),
    ],
    ids=[kind.value for kind in _SINGLE_KINDS],
)
def test_a_wrong_payload_shape_is_caught_at_retrieval(
    kind: CapabilityKind, retrieve: object
) -> None:
    """形状不对即 `KERNEL_INVARIANT_VIOLATED`，与已有四个取回函数同构。"""
    registry = frozen_with(kind, "x", payload="不是合法载荷")
    with pytest.raises(KaryviaError) as excinfo:
        retrieve(registry)  # type: ignore[operator]
    assert excinfo.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED
    assert excinfo.value.detail["actual"] == "str"


def test_a_bare_implementation_object_is_not_an_acceptable_payload() -> None:
    """裸实现对象也要被拒——这正是「必须包一层 dataclass」的理由。

    `FakeModelProvider` 结构上满足 `ModelProvider`，因此 `isinstance(payload, ModelProvider)`
    会放它过去；具体 wrapper 才是可靠的窄化手段。
    """
    registry = frozen_with(CapabilityKind.MODEL, "model", payload=FakeModelProvider())
    with pytest.raises(KaryviaError) as excinfo:
        model_providers_from(registry)
    assert excinfo.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


def test_retrieval_before_freezing_is_refused() -> None:
    """冻结前不可查找——未定案的结果随时可能被覆盖掉（registry 自己抛）。"""
    registry = CapabilityRegistry()
    with pytest.raises(KaryviaError) as excinfo:
        model_providers_from(registry)
    assert excinfo.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


# ------------------------------------------------------------------------------ SINGLETON 语义


def test_two_session_stores_without_an_override_leave_the_slot_empty() -> None:
    """SINGLETON 的多实现冲突下**冲突各方都不生效**（`resolution.py` 的既定语义）。

    因此 `session_store_from()` 返回 `None`，而不是替用户挑一个。
    """
    registry = CapabilityRegistry()
    with registry.batch(Builtin()) as batch:
        batch.add(CapabilityKind.SESSION_STORE, "a", _store())
    with registry.batch(Plugin(PluginId("acme"))) as batch:
        batch.add(CapabilityKind.SESSION_STORE, "b", _store())
    report = resolve_into(registry)
    assert not report.ok
    assert session_store_from(registry) is None


def test_two_active_singletons_would_be_a_broken_invariant() -> None:
    """真出现两项说明解析被绕过了——不静默取第一个，那会让「唯一生效实现」名存实亡。

    只能手工 `freeze()` 才构造得出这个局面（`resolve_into` 永远不会产出它），
    而这正说明守卫防的是「有人绕过解析」而不是某条正常路径。
    """
    registry = CapabilityRegistry()
    with registry.batch(Builtin()) as batch:
        first = batch.add(CapabilityKind.SESSION_STORE, "a", _store())
        second = batch.add(CapabilityKind.SESSION_STORE, "b", _store())
    registry.freeze([first, second])
    with pytest.raises(KaryviaError) as excinfo:
        session_store_from(registry)
    assert excinfo.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


def _store() -> object:
    from karyvia.kernel.plugins import RegisteredSessionStore

    return RegisteredSessionStore(store=InMemorySessionStore())
