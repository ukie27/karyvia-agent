"""公开测试夹具：Fake 能力实现与构造辅助（技术方案 §12.3）。

职责：提供可脚本化的 `FakeModelProvider`、`InMemorySessionStore`、`RecordingHook` 与
`ManualCancel`，以及一组构造契约对象的小辅助。
不负责：断言（那在 `contracts.py` 的契约测试基类里）、模拟真实 IO、模拟 Kernel 调度。

**Fake 必须在公开 SDK 内**：它同时是插件开发者的验收工具。插件作者要证明自己的能力
实现能在真实 turn 里工作，就需要一个不依赖网络与磁盘、又符合契约的对手方；把它藏在
仓库的 `tests/` 下等于让每个插件仓库自己再造一遍，而各造各的 Fake 正是「大家都以为
自己符合契约」的开始。

这些 Fake **不模拟失败注入**：需要失败路径的测试自己写一个抛错的实现即可（几行），
而把 `fail_on_call=3` 这类开关堆进 Fake，会让它慢慢长成第二个 Kernel。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence
from datetime import UTC, datetime

from karyvia.contracts import (
    CancelSignal,
    ChunkKind,
    Correlation,
    ErrorCode,
    HookContext,
    HookOutcome,
    InstanceId,
    KaryviaError,
    ModelCapability,
    ModelChunk,
    ModelInfo,
    ModelRequest,
    ModelResponse,
    SessionKey,
    SessionMessage,
    SessionSnapshot,
    StopReason,
    TokenUsage,
    ToolCall,
    TurnId,
)

__all__ = [
    "FAKE_MODEL_ID",
    "FakeModelProvider",
    "InMemorySessionStore",
    "ManualCancel",
    "RecordingHook",
    "make_correlation",
    "text_response",
    "tool_call_response",
]

#: Fake Provider 的默认模型标识。测试里出现这个串就说明走的是 Fake，不是真 Provider。
FAKE_MODEL_ID = "fake-model"


def make_correlation(
    *,
    turn_id: str = "turn-1",
    instance_id: str = "test",
    session_key: SessionKey | None = None,
) -> Correlation:
    """构造一个 `Correlation`。几乎每个能力方法都要它，逐个测试手写太吵。"""
    return Correlation(
        instance_id=InstanceId(instance_id),
        session_key=session_key or SessionKey(channel_id="cli", conversation_id="local"),
        turn_id=TurnId(turn_id),
    )


def text_response(content: str, *, model_id: str = FAKE_MODEL_ID) -> ModelResponse:
    """一条正常结束的文本响应。"""
    return ModelResponse(model_id, StopReason.END_TURN, content=content, usage=TokenUsage(1, 1))


def tool_call_response(*calls: ToolCall, model_id: str = FAKE_MODEL_ID) -> ModelResponse:
    """一条要求调用工具的响应。多次调用即一轮内的并行 Tool Call。"""
    return ModelResponse(
        model_id,
        StopReason.TOOL_CALLS,
        tool_calls=tuple(calls),
        usage=TokenUsage(1, 1),
    )


class ManualCancel:
    """手动控制的 `CancelSignal`。

    `CancelSignal` 只有观测面，因此测试没法用它自己触发取消；这里补上 `request()`。
    它不是 `D08` 的 `CancelToken`（那个还有 `child()` 与预算联动），只是最小的开关。
    """

    def __init__(self, *, code: ErrorCode = ErrorCode.CANCELLED_BY_USER) -> None:
        self._requested = False
        self._code = code

    @property
    def requested(self) -> bool:
        return self._requested

    def request(self) -> None:
        """请求取消。幂等。"""
        self._requested = True

    def raise_if_requested(self) -> None:
        if self._requested:
            raise KaryviaError(self._code, "已请求取消。")


class FakeModelProvider:
    """按脚本逐条返回响应的 `ModelProvider`。

    脚本是 `ModelResponse` 序列而不是「输入 -> 输出」映射：多轮 tool-call 循环里，
    第 N 轮的请求内容取决于前 N-1 轮的工具结果，用映射写测试等于先把被测逻辑抄一遍。
    序列则直接表达「第一轮要求调工具、第二轮给最终答案」。

    `stream()` 由同一条脚本派生分片，因此流式与非流式测试共用一份脚本——它们本就该
    产生同样的语义结果（`MOD-005` 不允许 Provider 在两条路径上给出不同能力）。
    """

    def __init__(
        self,
        responses: Iterable[ModelResponse] = (),
        *,
        model_id: str = FAKE_MODEL_ID,
        capabilities: frozenset[ModelCapability] | None = None,
        context_window_tokens: int = 8192,
    ) -> None:
        self._responses = list(responses)
        self._model_id = model_id
        self._capabilities = (
            capabilities
            if capabilities is not None
            else frozenset({ModelCapability.TOOL_CALLS, ModelCapability.STREAMING})
        )
        self._context_window_tokens = context_window_tokens
        #: 收到过的全部请求，按顺序。断言「Kernel 到底送了什么进模型」用它。
        self.requests: list[ModelRequest] = []

    def describe(self, model_id: str) -> ModelInfo:
        return ModelInfo(
            model_id=model_id,
            provider="fake",
            capabilities=self._capabilities,
            context_window_tokens=self._context_window_tokens,
            max_output_tokens=1024,
        )

    async def complete(self, request: ModelRequest, cancel: CancelSignal) -> ModelResponse:
        cancel.raise_if_requested()
        self.requests.append(request)
        return self._next()

    def stream(self, request: ModelRequest, cancel: CancelSignal) -> AsyncIterator[ModelChunk]:
        if ModelCapability.STREAMING not in self._capabilities:
            raise KaryviaError(
                ErrorCode.CAPABILITY_MISSING,
                "本 Fake 未声明流式能力。",
                detail={"model_id": self._model_id},
            )
        return self._stream(request, cancel)

    async def _stream(
        self, request: ModelRequest, cancel: CancelSignal
    ) -> AsyncIterator[ModelChunk]:
        cancel.raise_if_requested()
        self.requests.append(request)
        response = self._next()
        if response.content:
            yield ModelChunk(kind=ChunkKind.TEXT, text=response.content)
        for call in response.tool_calls:
            cancel.raise_if_requested()
            yield ModelChunk(kind=ChunkKind.TOOL_CALL, tool_call=call)
        yield ModelChunk(kind=ChunkKind.USAGE, usage=response.usage)
        yield ModelChunk(kind=ChunkKind.DONE, stop_reason=response.stop_reason)

    def _next(self) -> ModelResponse:
        if not self._responses:
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "FakeModelProvider 的脚本已用尽——被测代码多要了一轮。",
                detail={"model_id": self._model_id},
            )
        return self._responses.pop(0)


class InMemorySessionStore:
    """内存版 `SessionStore`。用 `storage_id()` 作为键，与真实实现的归属规则一致。

    刻意不用 `SessionKey` 本身作字典键（它可哈希，看上去更直接）：`storage_id()` 是
    已发布的持久化契约，让 Fake 也走一遍，任何破坏「不同 key 不撞同一个 id」的改动
    在这里就会被撞出来，而不是等到真实存储上线（`EDG-203`）。
    """

    def __init__(self) -> None:
        self._messages: dict[str, list[SessionMessage]] = {}
        self._keys: dict[str, SessionKey] = {}
        self._compacted: dict[str, int] = {}
        self._created: dict[str, datetime] = {}
        self._updated: dict[str, datetime] = {}

    async def load(self, key: SessionKey) -> SessionSnapshot:
        storage_id = key.storage_id()
        if storage_id not in self._messages:
            return SessionSnapshot(session_key=key)
        return SessionSnapshot(
            session_key=key,
            messages=tuple(self._messages[storage_id]),
            created_at=self._created[storage_id],
            updated_at=self._updated[storage_id],
            compacted_through=self._compacted[storage_id],
        )

    async def append(self, key: SessionKey, messages: Sequence[SessionMessage]) -> None:
        storage_id = key.storage_id()
        now = datetime.now(UTC)
        if storage_id not in self._messages:
            self._messages[storage_id] = []
            self._keys[storage_id] = key
            self._compacted[storage_id] = 0
            self._created[storage_id] = now
        self._messages[storage_id].extend(messages)
        self._updated[storage_id] = now

    async def compact(self, key: SessionKey, through: int, summary: SessionMessage) -> None:
        storage_id = key.storage_id()
        existing = self._messages.get(storage_id, [])
        if not 0 <= through <= len(existing):
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                "压缩水位超出会话记录范围。",
                detail={"through": through, "messages": len(existing)},
            )
        # 摘要**插在水位处**而不是替换掉前缀：`SessionStore.compact()` 要求 `load()` 之后
        # `compacted_through == through`，同时摘要必须出现在 `live_messages` 里。原地插入
        # 同时满足两者；原始记录是否物理删除由实现自行决定，这里留着便于测试比对。
        self._messages[storage_id] = [*existing[:through], summary, *existing[through:]]
        self._compacted[storage_id] = through
        self._updated[storage_id] = datetime.now(UTC)

    async def delete(self, key: SessionKey) -> bool:
        storage_id = key.storage_id()
        existed = storage_id in self._messages
        for table in (self._messages, self._keys, self._compacted, self._created, self._updated):
            table.pop(storage_id, None)
        return existed

    async def list_keys(self) -> tuple[SessionKey, ...]:
        return tuple(self._keys.values())


class RecordingHook:
    """记录每次分发的 `HookHandler`，可选返回一个固定的 `HookOutcome`。

    Observer 的返回值本就被忽略，因此默认返回 `None`；要测拦截行为就把想要的
    `HookOutcome` 传进来——它是否被采纳，取决于 Kernel 而不是 handler，这正是要测的。
    """

    def __init__(self, outcome: HookOutcome | None = None) -> None:
        self._outcome = outcome
        #: 收到过的全部上下文，按顺序。
        self.contexts: list[HookContext] = []

    @property
    def call_count(self) -> int:
        return len(self.contexts)

    def hooks_seen(self) -> tuple[str, ...]:
        """按顺序列出被触发的 Hook 名。断言「顺序」比断言「次数」有用得多。"""
        return tuple(context.hook.value for context in self.contexts)

    async def handle(self, context: HookContext) -> HookOutcome | None:
        self.contexts.append(context)
        return self._outcome
