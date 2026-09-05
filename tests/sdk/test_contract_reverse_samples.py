"""契约基类会「拦」的证明：6 个基类各配一个故意违约的实现（`D16` 验收、`NFR-702`）。

契约基类如果只是空壳，继承它的实现会**全绿地**通过一组什么都没断言的用例——那比没有契约
测试更危险，因为它给出的是虚假的可替换性保证。`tests/sdk/test_testing_kit.py` 已经为
`ModelProviderContract` 立了这个范式（`_IgnoresCancellation`），`D16` 把它补齐到 5 个，
`D39` 随 `MemoryProviderContract` 补上第 6 个。

**这些违约实现都不是臆造的**，每一个都对应一条真实的踩坑路径：

- `SessionStore`：「第一次说话」被当成错误抛出——最常见的实现偷懒方式。
- `ContextProvider`：空会话时抛错，而 `CTX-001` 不允许把「没有贡献」当成失败。
- `ToolHandler`：坏参数直接抛异常。逸出的异常会让 Kernel 只能把副作用标成 `UNKNOWN`，
  这是 `TOL` 系列契约里最要紧的一条。
- `Channel`：`stop()` 第二次调用就炸。停止阶段的异常只会拖住整个实例退出（`EDG-104`）。
- `MemoryProvider`：`recall()` 按插入顺序返回。契约写死「顺序即相关性排序」，而按插入
  顺序返回是最省事、也最难被发现的一种违约——调用方拿到的东西看起来完全正常。

命名约定：违约实现与承载它的套件类都**不以 `Test` 开头**，因此不会被 pytest 收集；
它们只被下面那几条 `test_*` 手动调用。这与既有文件的做法一致。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping

import pytest

from karyvia.contracts import (
    CancelSignal,
    Channel,
    ContextFragment,
    ContextProvider,
    Correlation,
    FragmentScope,
    InboundMessage,
    MemoryProvider,
    OutboundMessage,
    RiskLevel,
    SessionKey,
    SessionMessage,
    SessionSnapshot,
    SessionStore,
    SideEffect,
    ToolHandler,
    ToolInvocation,
    ToolResult,
    ToolSpec,
)
from karyvia.sdk.testing import (
    ECHO_SPEC,
    ChannelContract,
    ContextProviderContract,
    EchoTool,
    FakeMemoryProvider,
    MemoryProviderContract,
    NullChannel,
    SessionStoreContract,
    StaticContextProvider,
    ToolContract,
)

# ------------------------------------------------------------------ SessionStore：空会话即抛


class _RaisesOnUnknownSession:
    """违约实现：未知会话抛错，而契约要求返回空快照。"""

    def __init__(self) -> None:
        self._data: dict[str, list[SessionMessage]] = {}

    async def load(self, key: SessionKey) -> SessionSnapshot:
        if key.storage_id() not in self._data:
            raise KeyError(key.storage_id())
        return SessionSnapshot(session_key=key, messages=tuple(self._data[key.storage_id()]))

    async def append(self, key: SessionKey, messages: object) -> None:
        self._data.setdefault(key.storage_id(), []).extend(messages)  # type: ignore[arg-type]

    async def compact(self, key: SessionKey, through: int, summary: SessionMessage) -> None:
        raise NotImplementedError

    async def delete(self, key: SessionKey) -> bool:
        return self._data.pop(key.storage_id(), None) is not None

    async def list_keys(self) -> tuple[SessionKey, ...]:
        return ()


class _EmptySessionSuite(SessionStoreContract):
    def make_store(self) -> SessionStore:
        return _RaisesOnUnknownSession()


async def test_contract_rejects_a_store_that_raises_on_a_new_session() -> None:
    """「第一次说话」不是错误。抛 `KeyError` 说明实现把空会话当成了缺失。"""
    with pytest.raises(KeyError):
        await _EmptySessionSuite().test_loading_an_unknown_session_returns_an_empty_snapshot()


# ------------------------------------------------------- ContextProvider：空会话即抛


class _FailsOnEmptySession:
    """违约实现：空会话时抛错，而 `CTX-001` 要求「没有贡献」是正常结果。"""

    async def provide(
        self, snapshot: SessionSnapshot, correlation: Correlation, cancel: CancelSignal
    ) -> tuple[ContextFragment, ...]:
        del correlation, cancel
        if not snapshot.messages:
            raise RuntimeError("没有历史可用")
        return ()


class _EmptyContextSuite(ContextProviderContract):
    def make_provider(self) -> ContextProvider:
        return _FailsOnEmptySession()


async def test_contract_rejects_a_provider_that_fails_on_an_empty_session() -> None:
    with pytest.raises(RuntimeError):
        await _EmptyContextSuite().test_an_empty_session_is_not_an_error()


# ------------------------------------------------------------- ToolHandler：坏参数抛异常


_STRICT_SPEC = ToolSpec(
    name="test.strict",
    description="坏参数直接抛异常的违约工具。",
    parameters={"type": "object", "properties": {"text": {"type": "string"}}},
    read_only=True,
    risk=RiskLevel.SAFE,
)


class _RaisesOnBadArguments:
    """违约实现：坏参数抛异常而不是返回失败结果。

    后果是 Kernel 无从判断工具是否已经产生副作用，只能标 `UNKNOWN`——这正是
    「失败是一等结果」这条契约要避免的。
    """

    async def execute(self, invocation: ToolInvocation, cancel: CancelSignal) -> ToolResult:
        del cancel
        text = invocation.call.arguments.get("text")
        if not isinstance(text, str):
            raise TypeError("text 必须是字符串")
        return ToolResult(
            call_id=invocation.call.call_id,
            ok=True,
            content=text,
            truncated=False,
            side_effect=SideEffect.NONE,
        )


class _BadArgumentsSuite(ToolContract):
    def make_tool(self) -> tuple[ToolSpec, ToolHandler]:
        return _STRICT_SPEC, _RaisesOnBadArguments()

    def valid_arguments(self) -> dict[str, object]:
        return {"text": "hello"}  # type: ignore[return-value]

    def invalid_arguments(self) -> dict[str, object] | None:
        return {"text": 42}  # type: ignore[return-value]


async def test_contract_rejects_a_tool_that_raises_instead_of_failing() -> None:
    with pytest.raises(TypeError):
        await _BadArgumentsSuite().test_bad_arguments_come_back_as_a_result_not_an_exception()


# ------------------------------------------------------------------- Channel：stop 不幂等


class _StopIsNotIdempotent:
    """违约实现：`stop()` 第二次调用就炸，而契约要求它不抛（`EDG-104`）。"""

    def __init__(self) -> None:
        self._stopped = False

    @property
    def channel_id(self) -> str:
        return "brittle"

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        if self._stopped:
            raise RuntimeError("已经停过了")
        self._stopped = True

    def receive(self) -> AsyncIterator[InboundMessage]:
        return self._receive()

    async def _receive(self) -> AsyncIterator[InboundMessage]:
        return
        yield  # pragma: no cover - 让本方法成为异步生成器

    async def deliver(self, message: OutboundMessage) -> None:
        return None


class _StopTwiceSuite(ChannelContract):
    def make_channel(self) -> Channel:
        return _StopIsNotIdempotent()


async def test_contract_rejects_a_channel_whose_stop_is_not_idempotent() -> None:
    with pytest.raises(RuntimeError):
        await _StopTwiceSuite().test_stop_is_safe_to_call_twice()


# ------------------------------------------------ MemoryProvider：召回顺序不是相关性顺序


class _RecallsInInsertionOrder:
    """违约实现：`recall()` 按写入顺序返回，而契约要求按相关性从高到低插入。

    它「能用」——调用方拿到的是合法的映射、合法的片段，一切看起来正常。只有当最相关的
    那条排在第三位、模型据此答错时才会被发现，而那时没人会怀疑到记忆后端头上。
    """

    def __init__(self) -> None:
        self._records: dict[str, ContextFragment] = {}

    async def remember(self, fragment: ContextFragment, cancel: CancelSignal) -> str:
        del cancel
        record_id = f"insertion-{len(self._records)}"
        self._records[record_id] = fragment
        return record_id

    async def recall(
        self,
        query: str,
        *,
        scope: FragmentScope,
        limit: int,
        cancel: CancelSignal,
    ) -> Mapping[str, ContextFragment]:
        del query, cancel
        hits = [
            (record_id, fragment)
            for record_id, fragment in self._records.items()
            if fragment.scope is scope
        ]
        return dict(hits[:limit])

    async def forget(self, record_id: str) -> bool:
        return self._records.pop(record_id, None) is not None


class _InsertionOrderSuite(MemoryProviderContract):
    def make_provider(self) -> MemoryProvider:
        return _RecallsInInsertionOrder()


async def test_contract_rejects_a_memory_provider_that_ignores_relevance() -> None:
    with pytest.raises(AssertionError):
        await _InsertionOrderSuite().test_recall_order_is_relevance_order()


# -------------------------------------------------------------- 参考实现必须**通过**全部基类


def test_every_contract_base_class_has_a_reverse_sample() -> None:
    """本文件的存在意义就是这张清单——6 个基类一个都不能漏。

    `ModelProviderContract` 的反向样例在 `test_testing_kit.py`（`_IgnoresCancellation`），
    那是 `D05` 立的范式，不搬过来是为了不动既有文件的结构。
    """
    covered = {
        SessionStoreContract: _EmptySessionSuite,
        ContextProviderContract: _EmptyContextSuite,
        ToolContract: _BadArgumentsSuite,
        ChannelContract: _StopTwiceSuite,
        MemoryProviderContract: _InsertionOrderSuite,
    }
    assert len(covered) == 5
    assert all(issubclass(suite, base) for base, suite in covered.items())


# ------------------------------------------------- 随 SDK 发布的参考实现必须**通过**基类


class TestEchoToolContract(ToolContract):
    """`D16` 之前 `ToolContract` 没有任何随 SDK 发布的参考实现，插件作者无样例可抄。"""

    def make_tool(self) -> tuple[ToolSpec, ToolHandler]:
        return ECHO_SPEC, EchoTool()

    def valid_arguments(self) -> dict[str, object]:
        return {"text": "hello"}  # type: ignore[return-value]

    def invalid_arguments(self) -> dict[str, object] | None:
        return {"text": 42}  # type: ignore[return-value]


class TestNullChannelContract(ChannelContract):
    def make_channel(self) -> Channel:
        return NullChannel()


class TestStaticContextProviderContract(ContextProviderContract):
    def make_provider(self) -> ContextProvider:
        return StaticContextProvider()


class TestFakeMemoryProviderContract(MemoryProviderContract):
    """随 SDK 发布的参考实现必须通过基类——否则「照它抄」这句话是空的。"""

    def make_provider(self) -> MemoryProvider:
        return FakeMemoryProvider()
