"""契约测试基类：可替换性的证明（技术方案 §12.3、`NFR-702`）。

职责：为 7 类能力提供可继承的契约测试基类——实现方（内建或插件）继承对应基类并提供
构造夹具，即获得全部通用用例。
不负责：测某个具体实现的独有行为、提供夹具本身（那在 `fakes.py`）、启动 Kernel。

用法（`pytest` 只收集名字以 `Test` 开头的类，因此子类必须这样命名）：

```python
from karyvia.sdk.testing import SessionStoreContract, InMemorySessionStore


class TestMyStore(SessionStoreContract):
    def make_store(self) -> SessionStore:
        return InMemorySessionStore()
```

两条设计约束：

- **不 import pytest**。基类只是普通类与 `assert`，因此插件作者用什么 runner 都行，
  SDK 也不因为一个测试工具而多一条运行期依赖。异步用例依赖调用方开启
  `asyncio_mode = "auto"`（或自行加 `@pytest.mark.asyncio`）。
- **只断言契约文本明确要求的行为**。「实现大概会这么做」的断言会把契约测试变成对某个
  具体实现的模仿，反而挡住合法的替代实现——那与 `NFR-702` 的目的正相反。当前是第一版
  骨架，各基类的 docstring 列出了后续模块应当补齐的用例。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from karyvia.contracts import (
    CancelSignal,
    Channel,
    ChunkKind,
    ContextFragment,
    ContextProvider,
    ErrorCategory,
    FragmentKind,
    FragmentScope,
    JsonValue,
    KaryviaError,
    MemoryProvider,
    ModelCapability,
    ModelInfo,
    ModelMessage,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    OutboundMessage,
    Role,
    SessionKey,
    SessionMessage,
    SessionSnapshot,
    SessionStore,
    SideEffect,
    StopReason,
    StreamState,
    ToolCall,
    ToolHandler,
    ToolInvocation,
    ToolSpec,
    TrustLevel,
    TurnCompactionRequest,
    TurnCompactionResult,
    TurnContextCompactor,
    TurnContextUnit,
    TurnContextUnitKind,
    TurnId,
)

from .fakes import ManualCancel, make_correlation

__all__ = [
    "ChannelContract",
    "ContextProviderContract",
    "MemoryProviderContract",
    "ModelProviderContract",
    "SessionStoreContract",
    "ToolContract",
    "TurnContextCompactorContract",
]

#: 唯一一处「用例走到了不该到的地方」的失败信息。抽成常量是为了让 `TRY003`
#: （异常消息不写在 raise 处）与「失败信息要说人话」同时成立。
_CANCELLED_BUT_COMPLETED = "已请求取消时 complete() 仍然正常返回"


class _ContractBase:
    """夹具缺失时给出可读的失败信息，而不是 `AttributeError`。"""

    def _required(self, name: str) -> None:
        raise NotImplementedError(
            f"{type(self).__name__} 必须实现 {name}()——契约测试需要被测实现的构造方式。"
        )


class ModelProviderContract(_ContractBase):
    """`ModelProvider` 的通用契约（需求 §9.5）。

    后续应补齐：限流/超时/认证失败到 `ErrorCode` 的映射（`MOD-003`）、流式中途失败必须
    先 yield `DONE(ERROR)`、`CONTENT_FILTER` 作为正常响应而非异常。这些都需要能操纵
    provider 的对端，属于各实现自带的注入夹具，不能在通用基类里假设。
    """

    def make_provider(self) -> ModelProvider:
        """返回待测 provider。用例可能多次调用它，每次都应给出可用的新实例。"""
        self._required("make_provider")
        raise AssertionError  # pragma: no cover - `_required` 一定抛

    def model_id(self) -> str:
        """待测 provider 支持的一个模型标识。"""
        return "fake-model"

    def make_request(self, *, stream: bool = False) -> ModelRequest:
        """构造一次最小请求。实现方通常不需要覆盖。"""
        return ModelRequest(
            model_id=self.model_id(),
            messages=(ModelMessage(role=Role.USER, content="ping"),),
            correlation=make_correlation(),
            stream=stream,
        )

    def test_describe_reports_the_requested_model(self) -> None:
        info = self.make_provider().describe(self.model_id())
        assert info.model_id == self.model_id()
        assert info.provider
        # `CTX-003` 的预算推导直接读这个数；声明 0 等于让组装器无从判断上限。
        assert info.context_window_tokens > 0

    async def test_complete_returns_a_response_for_the_same_model(self) -> None:
        provider = self.make_provider()
        response = await provider.complete(self.make_request(), ManualCancel())
        assert response.model_id == self.model_id()

    async def test_stream_matches_the_declared_streaming_capability(self) -> None:
        """声明了流式就必须能流；没声明就必须报缺失，不得静默降级（`MOD-005`）。"""
        provider = self.make_provider()
        declared = ModelCapability.STREAMING in provider.describe(self.model_id()).capabilities
        request = self.make_request(stream=True)
        try:
            chunks = [chunk async for chunk in provider.stream(request, ManualCancel())]
        except KaryviaError as exc:
            assert not declared, "声明了 STREAMING 却拒绝流式请求"
            assert exc.category is ErrorCategory.CAPABILITY_MISSING
            return
        assert declared, "未声明 STREAMING 却接受了流式请求"
        assert chunks and chunks[-1].kind is ChunkKind.DONE, "流式必须以 DONE 分片收尾"

    async def test_complete_honours_an_already_requested_cancellation(self) -> None:
        """「进入网络调用前检查 `cancel`」——已取消就不该再产生一次外部往返。"""
        cancel = ManualCancel()
        cancel.request()
        try:
            await self.make_provider().complete(self.make_request(), cancel)
        except KaryviaError as exc:
            assert exc.category is ErrorCategory.CANCELLED
            return
        raise AssertionError(_CANCELLED_BUT_COMPLETED)


class SessionStoreContract(_ContractBase):
    """`SessionStore` 的通用契约（需求 §9.7）。

    后续应补齐：并发写入的顺序保证（`SES-002`）、损坏记录必须抛
    `PERSISTENCE_RECORD_CORRUPT` 而不是伪装成空历史、跨实现的格式迁移（`SES-006`）。
    后两项需要能构造坏数据，属于实现自带的夹具。
    """

    def make_store(self) -> SessionStore:
        self._required("make_store")
        raise AssertionError  # pragma: no cover

    def make_key(self, name: str) -> SessionKey:
        """每个用例用独立的 key，实现之间不必操心清理。"""
        return SessionKey(channel_id="contract", conversation_id=name)

    def make_message(self, message_id: str, content: str = "hello") -> SessionMessage:
        return SessionMessage(
            message_id=message_id,
            role=Role.USER,
            content=content,
            created_at=datetime.now(UTC),
        )

    async def test_loading_an_unknown_session_returns_an_empty_snapshot(self) -> None:
        """「第一次说话」不是错误，不得抛。"""
        snapshot = await self.make_store().load(self.make_key("unknown"))
        assert snapshot.messages == ()
        assert snapshot.compacted_through == 0

    async def test_append_then_load_round_trips(self) -> None:
        store = self.make_store()
        key = self.make_key("round-trip")
        await store.append(key, [self.make_message("m1"), self.make_message("m2", "second")])
        snapshot = await store.load(key)
        assert [m.message_id for m in snapshot.messages] == ["m1", "m2"]
        assert snapshot.messages[1].content == "second"

    async def test_delete_reports_whether_the_session_existed(self) -> None:
        store = self.make_store()
        key = self.make_key("deletable")
        assert await store.delete(key) is False
        await store.append(key, [self.make_message("m1")])
        assert await store.delete(key) is True
        assert (await store.load(key)).messages == ()

    async def test_list_keys_contains_written_sessions(self) -> None:
        store = self.make_store()
        key = self.make_key("listed")
        await store.append(key, [self.make_message("m1")])
        assert key in await store.list_keys()

    async def test_compact_advances_the_watermark(self) -> None:
        """`load()` 之后 `compacted_through` 必须等于 `through`（`SES-005`）。"""
        store = self.make_store()
        key = self.make_key("compacted")
        await store.append(key, [self.make_message(f"m{i}") for i in range(4)])
        summary = SessionMessage(
            message_id="summary",
            role=Role.SYSTEM,
            content="前两条的摘要",
            created_at=datetime.now(UTC),
        )
        await store.compact(key, 2, summary)
        snapshot = await store.load(key)
        assert snapshot.compacted_through == 2
        assert any(m.message_id == "summary" for m in snapshot.live_messages)


class ContextProviderContract(_ContractBase):
    """`ContextProvider` 的通用契约（需求 §9.6）。

    后续应补齐：`CTX-005`（非关键 Provider 异常时被跳过）与预算裁剪下的行为——两者都要
    Kernel 的组装器参与，属于 `D08` 的集成测试而不是单个实现的契约。
    """

    def make_provider(self) -> ContextProvider:
        self._required("make_provider")
        raise AssertionError  # pragma: no cover

    def make_snapshot(self) -> SessionSnapshot:
        return SessionSnapshot(session_key=SessionKey(channel_id="contract", conversation_id="ctx"))

    async def test_provide_returns_a_tuple_of_fragments(self) -> None:
        fragments = await self.make_provider().provide(
            self.make_snapshot(), make_correlation(), ManualCancel()
        )
        assert isinstance(fragments, tuple)
        assert all(isinstance(fragment, ContextFragment) for fragment in fragments)

    async def test_an_empty_session_is_not_an_error(self) -> None:
        """空会话下返回空元组是正常结果；`CTX-001` 不允许把「没有贡献」当成失败。"""
        await self.make_provider().provide(
            SessionSnapshot(session_key=SessionKey(channel_id="contract", conversation_id="empty")),
            make_correlation(),
            ManualCancel(),
        )


class TurnContextCompactorContract(_ContractBase):
    """`TurnContextCompactor` 的最小可替换性契约。"""

    def make_compactor(self) -> TurnContextCompactor:
        self._required("make_compactor")
        raise AssertionError  # pragma: no cover

    def make_request(self) -> TurnCompactionRequest:
        message = ModelMessage(role=Role.ASSISTANT, content="较早的临时上下文")
        request = ModelRequest(
            model_id="fake-model",
            messages=(message, ModelMessage(role=Role.USER, content="继续")),
            correlation=make_correlation(),
        )
        unit = TurnContextUnit(
            unit_id="unit-1",
            kind=TurnContextUnitKind.BASE,
            messages=(message,),
            estimated_tokens=16,
        )
        return TurnCompactionRequest(
            request=request,
            units=(unit,),
            target_tokens=128,
            estimated_tokens=48,
            correlation=request.correlation,
        )

    async def test_compact_returns_a_non_empty_prefix_replacement(self) -> None:
        result = await self.make_compactor().compact(
            self.make_request(), _ContractCompactionModel(), ManualCancel()
        )
        assert isinstance(result, TurnCompactionResult)
        assert result.through_units >= 1
        assert result.summary.strip()


class _ContractCompactionModel:
    @property
    def info(self) -> ModelInfo:
        return ModelInfo(
            model_id="fake-model",
            provider="fake",
            context_window_tokens=1024,
            max_output_tokens=128,
        )

    async def complete(
        self,
        messages: Sequence[ModelMessage],
        cancel: CancelSignal,
        *,
        max_output_tokens: int | None = None,
    ) -> ModelResponse:
        del messages, cancel, max_output_tokens
        return ModelResponse("fake-model", StopReason.END_TURN, content="摘要")


class ToolContract(_ContractBase):
    """`ToolHandler` + `ToolSpec` 的通用契约（需求 §9.9）。

    后续应补齐：取消宽限期内必须返回结果并如实标注 `side_effect`（`EDG-407`）、
    超长输出的截断与 `truncated=True`（`TOL-003`）——前者需要能让工具阻塞住的夹具。
    """

    def make_tool(self) -> tuple[ToolSpec, ToolHandler]:
        self._required("make_tool")
        raise AssertionError  # pragma: no cover

    def valid_arguments(self) -> dict[str, JsonValue]:
        """一组符合 `spec.parameters` 的参数。"""
        return {}

    def invalid_arguments(self) -> dict[str, JsonValue] | None:
        """一组**不**符合 schema 的参数；返回 `None` 表示该工具没有可失败的输入。"""
        return None

    def _invocation(self, arguments: dict[str, JsonValue]) -> ToolInvocation:
        spec, _ = self.make_tool()
        return ToolInvocation(
            call=ToolCall(call_id="call-1", name=spec.name, arguments=arguments),
            correlation=make_correlation(),
            timeout_ms=5_000,
        )

    def test_spec_declares_an_object_parameter_schema(self) -> None:
        spec, _ = self.make_tool()
        assert spec.parameters.get("type") == "object", "工具参数 schema 必须是 object"
        assert spec.description

    async def test_execute_answers_the_same_call_id(self) -> None:
        _, handler = self.make_tool()
        result = await handler.execute(self._invocation(self.valid_arguments()), ManualCancel())
        assert result.call_id == "call-1"

    async def test_a_read_only_tool_reports_no_side_effect(self) -> None:
        spec, handler = self.make_tool()
        if not spec.read_only:
            return
        result = await handler.execute(self._invocation(self.valid_arguments()), ManualCancel())
        assert result.side_effect is SideEffect.NONE

    async def test_bad_arguments_come_back_as_a_result_not_an_exception(self) -> None:
        """`TOL` 的约定：失败是一等结果。逸出的异常会让 Kernel 只能标 `UNKNOWN` 副作用。"""
        arguments = self.invalid_arguments()
        if arguments is None:
            return
        _, handler = self.make_tool()
        result = await handler.execute(self._invocation(arguments), ManualCancel())
        assert result.ok is False
        assert result.error is not None


class ChannelContract(_ContractBase):
    """`Channel` 的通用契约（需求 §9.4）。

    后续应补齐：入站归一化（原始 SDK 对象不得进入 Kernel，`MSG-004`）、平台长度上限下的
    分段降级（`MSG-003`）、`is_complete_answer=False` 时必须附加标记（`EDG-304`）——
    这三条都要一个可断言的假平台，属于各 Channel 自带的夹具。
    """

    def make_channel(self) -> Channel:
        self._required("make_channel")
        raise AssertionError  # pragma: no cover

    def make_outbound(self, channel: Channel) -> OutboundMessage:
        """构造一条可投递的出站消息。平台对内容有特殊要求时覆盖它。"""
        key = SessionKey(channel_id=channel.channel_id, conversation_id="contract")
        return OutboundMessage(
            session_key=key,
            channel_id=key.channel_id,
            conversation_id=key.conversation_id,
            turn_id=TurnId("turn-1"),
            content="hello",
            stream_state=StreamState.FINAL,
        )

    def test_channel_id_is_stable(self) -> None:
        channel = self.make_channel()
        assert channel.channel_id
        assert channel.channel_id == channel.channel_id

    async def test_stop_is_safe_to_call_twice(self) -> None:
        """`stop()` 约定不抛：停止阶段的异常只会拖住整个实例退出（`EDG-104`）。"""
        channel = self.make_channel()
        await channel.start()
        await channel.stop()
        await channel.stop()

    async def test_receive_yields_an_async_iterator(self) -> None:
        channel = self.make_channel()
        stream = channel.receive()
        assert hasattr(stream, "__anext__")
        await channel.stop()

    async def test_deliver_accepts_a_final_message(self) -> None:
        """投递不接受取消，也不该因为「还没 start」就炸——`start()` 之后即可用。"""
        channel = self.make_channel()
        await channel.start()
        await channel.deliver(self.make_outbound(channel))
        await channel.stop()


class MemoryProviderContract(_ContractBase):
    """`MemoryProvider` 的通用契约（需求 §9.8 `MEM-001`–`MEM-005`）。

    `MEM-001`「Kernel 只依赖 Memory Interface，不假设文件、向量或图数据库」的全部意义
    就是后端可替换——而没有一个可执行的契约基类，那句话只是文档。这个基类是它的可执行形态：
    换后端的人继承它，就拿到「换完之后调用方看到的行为不变」的证明。

    实现方要提供 `make_provider()`。`make_fragment()` 有默认实现，用 `AGENT` 范围——
    契约的三个方法**都不带 `SessionKey`**，因此只有实例级范围在这条接口上无歧义；
    需要别的范围的实现方自己覆盖它。

    后续应补齐：`MEM-003` 的降级（后端不可用时 Kernel 按配置继续对话）需要装配根参与，
    属于集成测试而不是单个实现的契约。
    """

    def make_provider(self) -> MemoryProvider:
        self._required("make_provider")
        raise AssertionError  # pragma: no cover

    def make_fragment(self, content: str = "记住这件事") -> ContextFragment:
        return ContextFragment(
            source="contract:memory",
            kind=FragmentKind.MEMORY,
            content=content,
            priority=50,
            estimated_tokens=len(content),
            scope=FragmentScope.AGENT,
            # 模型生成内容应以 `UNTRUSTED` 写入——召回时会被包裹为数据块（`EDG-306`）。
            trust=TrustLevel.UNTRUSTED,
        )

    def recall_scope(self) -> FragmentScope:
        """`recall()` 用哪个范围。与 `make_fragment()` 的范围必须一致。"""
        return FragmentScope.AGENT

    async def test_remember_returns_a_usable_record_id(self) -> None:
        """`remember()` 交出的标识必须是非空字符串，且能喂回 `forget()`。"""
        provider = self.make_provider()
        record_id = await provider.remember(self.make_fragment(), ManualCancel())
        assert isinstance(record_id, str)
        assert record_id
        assert await provider.forget(record_id) is True

    async def test_forget_reports_whether_the_record_existed(self) -> None:
        """不存在返回 `False`**且不抛**（契约原文）——重复删除是幂等的。"""
        provider = self.make_provider()
        record_id = await provider.remember(self.make_fragment(), ManualCancel())
        assert await provider.forget(record_id) is True
        assert await provider.forget(record_id) is False

    async def test_recall_keys_can_be_fed_back_to_forget(self) -> None:
        """`recall()` 返回映射而不是元组，正是为了让 `MEM-005` 的删除拿得到标识。"""
        provider = self.make_provider()
        content = "召回之后要能删掉这一条"
        await provider.remember(self.make_fragment(content), ManualCancel())
        recalled = await provider.recall(
            content, scope=self.recall_scope(), limit=5, cancel=ManualCancel()
        )
        assert recalled, "刚写进去的内容用它自己当查询词，必须召得回来"
        for record_id, fragment in recalled.items():
            assert isinstance(fragment, ContextFragment)
            assert await provider.forget(record_id) is True

    async def test_recall_respects_the_limit(self) -> None:
        provider = self.make_provider()
        for index in range(4):
            await provider.remember(self.make_fragment(f"限额用例第 {index} 条"), ManualCancel())
        recalled = await provider.recall(
            "限额用例", scope=self.recall_scope(), limit=2, cancel=ManualCancel()
        )
        assert len(recalled) <= 2

    async def test_recall_order_is_relevance_order(self) -> None:
        """**顺序即相关性排序**（契约原文）：实现方必须按相关性从高到低插入。

        判据只用契约保证得了的那一条：拿其中一条的原文当查询词，它必须排第一。
        「第二名该是谁」是实现的自由。
        """
        provider = self.make_provider()
        target = "锦瑟无端五十弦"
        for content in ("春江潮水连海平", target, "千里莺啼绿映红"):
            await provider.remember(self.make_fragment(content), ManualCancel())
        recalled = await provider.recall(
            target, scope=self.recall_scope(), limit=3, cancel=ManualCancel()
        )
        assert recalled, "用原文当查询词必须召得回来"
        assert next(iter(recalled.values())).content == target

    async def test_recall_on_an_empty_store_is_not_an_error(self) -> None:
        """查不到东西返回空映射，不抛——但**故障时不得**用空结果掩盖（那条由实现自测）。"""
        recalled = await self.make_provider().recall(
            "这个实例里不存在的查询词", scope=self.recall_scope(), limit=3, cancel=ManualCancel()
        )
        assert dict(recalled) == {}
