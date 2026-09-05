"""`test_engine.py` 的夹具：脚本化 Provider、记录型 Invoker/Dispatcher 与构造辅助。

职责：提供 engine 测试用的四个 Fake 与一组契约对象构造器。
不负责：断言任何行为——断言全在 `test_engine.py`；本模块不含 IO、不联网。

**为什么不全用 `sdk/testing/FakeModelProvider`**：它能表达「一串 `ModelResponse`」，engine 的多轮
脚本场景确实够用（`test_engine.py` 的部分用例直接用它）。但它的 `stream()` 是从 `ModelResponse`
派生出**一个** TEXT 分片，而 engine 要测的恰恰是分片级行为：REASONING 分片、`DONE(ERROR)`、
缺 DONE、重复 call_id、第 N 片之后取消。`sdk/testing/fakes.py` 的模块 docstring 明确拒绝把失败
注入开关堆进 SDK Fake（「会让它慢慢长成第二个 Kernel」），并指示需要失败路径的测试自己写一个——
`ScriptedProvider` 就是照这条指示写的。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import replace

from karyvia.contracts import (
    ChunkKind,
    Concurrency,
    Correlation,
    HookAction,
    HookContext,
    HookName,
    HookOutcome,
    InstanceId,
    JsonValue,
    ModelChunk,
    ModelInfo,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    Role,
    SessionKey,
    SideEffect,
    StopReason,
    ToolCall,
    ToolInvocation,
    ToolResult,
    ToolSpec,
    TurnId,
)
from karyvia.kernel.turn import TERMINAL_EVENTS, CancelToken, TurnEvent

__all__ = [
    "CORRELATION",
    "FakeClock",
    "RecordingHookDispatcher",
    "RecordingToolInvoker",
    "ScriptedProvider",
    "chunks_for",
    "collect",
    "make_request",
    "ok_result",
    "text_response",
    "tool_call",
    "tool_response",
    "tool_spec",
    "user_message",
]

CORRELATION = Correlation(
    instance_id=InstanceId("test-instance"),
    session_key=SessionKey("cli", "local"),
    turn_id=TurnId("turn-1"),
)


class FakeClock:
    """手动推进的单调时钟。

    从 `test_limits.py` 抄了一份而不是抽公共夹具：AGENTS.md 原则 5「优先重复而非过早抽象」，
    而且共享一个时钟会让两个测试文件互相牵动。
    """

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance_ms(self, milliseconds: int) -> None:
        self.now += milliseconds / 1000


# --------------------------------------------------------------------------------------
# 契约对象构造器
# --------------------------------------------------------------------------------------


def user_message(text: str = "你好") -> ModelMessage:
    return ModelMessage(role=Role.USER, content=text)


def tool_spec(name: str, *, concurrency: Concurrency = Concurrency.PARALLEL) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"测试工具 {name}",
        parameters={"type": "object"},
        concurrency=concurrency,
    )


def tool_call(name: str, *, call_id: str | None = None, **arguments: JsonValue) -> ToolCall:
    return ToolCall(call_id=call_id or f"call-{name}", name=name, arguments=arguments)


def text_response(text: str = "答案", *, model_id: str = "fake-model") -> ModelResponse:
    return ModelResponse(model_id=model_id, stop_reason=StopReason.END_TURN, content=text)


def tool_response(*calls: ToolCall, model_id: str = "fake-model") -> ModelResponse:
    return ModelResponse(
        model_id=model_id, stop_reason=StopReason.TOOL_CALLS, tool_calls=tuple(calls)
    )


def ok_result(content: str = "工具结果", *, call_id: str = "unused") -> ToolResult:
    """成功结果模板。`call_id` 由 `RecordingToolInvoker` 在返回前改写成真实值。"""
    return ToolResult(
        call_id=call_id,
        ok=True,
        content=content,
        truncated=False,
        side_effect=SideEffect.OCCURRED,
    )


def make_request(
    *,
    tools: Sequence[ToolSpec] = (),
    stream: bool = False,
    messages: Sequence[ModelMessage] = (),
    timeout_ms: int = 0,
) -> ModelRequest:
    return ModelRequest(
        model_id="fake-model",
        messages=tuple(messages) or (user_message(),),
        correlation=CORRELATION,
        tools=tuple(tools),
        stream=stream,
        timeout_ms=timeout_ms,
    )


def chunks_for(response: ModelResponse, *, text_pieces: Sequence[str] = ()) -> list[ModelChunk]:
    """把一个 `ModelResponse` 摊成一串合法分片，末尾带 DONE。"""
    pieces = list(text_pieces) or ([response.content] if response.content else [])
    chunks = [ModelChunk(kind=ChunkKind.TEXT, text=piece) for piece in pieces]
    chunks += [
        ModelChunk(kind=ChunkKind.TOOL_CALL, tool_call=call) for call in response.tool_calls
    ]
    chunks.append(ModelChunk(kind=ChunkKind.DONE, stop_reason=response.stop_reason))
    return chunks


async def collect(events: AsyncIterator[TurnEvent]) -> list[TurnEvent]:
    """把事件流收成列表，并顺手断言「恰好一个终态且在末尾」这条不变量。"""
    collected = [event async for event in events]
    assert collected, "事件流不得为空"
    terminals = [event for event in collected if isinstance(event, TERMINAL_EVENTS)]
    assert len(terminals) == 1, f"终态事件应恰好一个，实得 {len(terminals)}"
    assert isinstance(collected[-1], TERMINAL_EVENTS), "终态事件必须在最后"
    return collected


# --------------------------------------------------------------------------------------
# Fake：模型
# --------------------------------------------------------------------------------------

#: 一条脚本项：整份响应（非流式）、一串分片（流式）、或一个要抛出的异常。
ScriptEntry = ModelResponse | Sequence[ModelChunk] | BaseException


class ScriptedProvider:
    """按脚本回放的 `ModelProvider`。

    `default` 是脚本耗尽后无限重复的那一项——「永远返回 tool_call 的模型」需要它。
    `stream_chunks` 是流式脚本的便捷形态：每次 `stream()` 都回放同一串分片。

    两个取消钩子都在「已经交付内容之后」触发，因为要测的正是「取消后已产生的内容是否
    还在」（`KER-007`）：

    - `cancel_after_chunk=(token, n)`：第 n 个分片**已交给 engine** 后请求取消。
    - `cancel_after_response=token`：响应已构造、即将返回前请求取消。
    """

    def __init__(
        self,
        script: Sequence[ScriptEntry] = (),
        *,
        default: ScriptEntry | None = None,
        model_id: str = "fake-model",
        stream_chunks: Sequence[ModelChunk] | None = None,
        cancel_after_chunk: tuple[CancelToken, int] | None = None,
        cancel_after_response: CancelToken | None = None,
    ) -> None:
        self._script = list(script)
        self._default = stream_chunks if default is None and stream_chunks else default
        self._model_id = model_id
        self._cancel_after_chunk = cancel_after_chunk
        self._cancel_after_response = cancel_after_response
        self._index = 0
        self.requests: list[ModelRequest] = []

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def describe(self, model_id: str) -> ModelInfo:
        return ModelInfo(model_id=model_id, provider="fake", context_window_tokens=8192)

    def _next(self) -> ScriptEntry:
        if self._index < len(self._script):
            entry = self._script[self._index]
            self._index += 1
            return entry
        if self._default is not None:
            return self._default
        raise AssertionError("模型脚本已耗尽")

    async def complete(self, request: ModelRequest, cancel: object) -> ModelResponse:
        del cancel
        self.requests.append(request)
        entry = self._next()
        if isinstance(entry, BaseException):
            raise entry
        if not isinstance(entry, ModelResponse):
            raise AssertionError("非流式请求拿到了分片脚本")
        if self._cancel_after_response is not None:
            self._cancel_after_response.request()
        return entry

    def stream(self, request: ModelRequest, cancel: object) -> AsyncIterator[ModelChunk]:
        """契约要求 `stream` 是普通 `def` 返回 `AsyncIterator`，不是 `async def`。"""
        del cancel
        self.requests.append(request)
        return self._stream(self._next())

    async def _stream(self, entry: ScriptEntry) -> AsyncIterator[ModelChunk]:
        if isinstance(entry, BaseException):
            raise entry
        chunks = chunks_for(entry) if isinstance(entry, ModelResponse) else list(entry)
        for delivered, chunk in enumerate(chunks, start=1):
            yield chunk
            # 在 yield **之后**请求取消：消费方此刻已经拿到这个分片，
            # 于是「取消前产生的内容必须留存」这条断言才有东西可断。
            if self._cancel_after_chunk is not None:
                token, at = self._cancel_after_chunk
                if delivered == at:
                    token.request()


# --------------------------------------------------------------------------------------
# Fake：工具执行器
# --------------------------------------------------------------------------------------


class RecordingToolInvoker:
    """记录型 `ToolInvoker`。

    `results` 按工具名给出要返回的结果或要抛出的异常；未登记的名字返回一条成功结果。
    `handlers` 给出完全自定义的协程（拿到工具名），用于会合、超时这类时序场景——
    **证明并发靠 `asyncio.Barrier`，不靠时序痕迹**：若 engine 把并发批串行化，
    barrier 会直接死锁并被 `asyncio.timeout` 判超时，而时序痕迹在慢机器上会假阳性。

    `cancel_after_first` 在第一次调用**返回之后**请求取消：检查点 6 要断言的是
    「已执行的工具保留真实结果」，取消必须发生在结果产生之后。
    """

    def __init__(
        self,
        *,
        results: Mapping[str, ToolResult | BaseException] | None = None,
        handlers: Mapping[str, Callable[[str], Awaitable[ToolResult]]] | None = None,
        trace: list[str] | None = None,
        prepare_error: BaseException | None = None,
        delays: Mapping[str, int] | None = None,
        cancel_after_first: CancelToken | None = None,
    ) -> None:
        self._results = dict(results or {})
        self._handlers = dict(handlers or {})
        self._trace = trace
        self._prepare_error = prepare_error
        # 每个工具在返回前让出事件循环的轮数。用它制造「完成顺序 != 调用顺序」，
        # 不用 wall-clock sleep：后者会让测试在慢机器上变成偶发失败。
        self._delays = dict(delays or {})
        self._cancel_after_first = cancel_after_first
        self.invocations: list[ToolInvocation] = []
        self.cancels: list[object] = []

    @property
    def invocations_by_name(self) -> list[tuple[str, ToolInvocation]]:
        return [(item.call.name, item) for item in self.invocations]

    def prepare(
        self, call: ToolCall, *, correlation: Correlation, timeout_ms: int
    ) -> ToolInvocation:
        if self._prepare_error is not None:
            raise self._prepare_error
        return ToolInvocation(call=call, correlation=correlation, timeout_ms=timeout_ms)

    async def invoke(self, invocation: ToolInvocation, cancel: object) -> ToolResult:
        self.invocations.append(invocation)
        self.cancels.append(cancel)
        name = invocation.call.name
        if self._trace is not None:
            self._trace.append(f"start:{name}")

        handler = self._handlers.get(name)
        if handler is not None:
            outcome: ToolResult | BaseException = await handler(name)
        else:
            for _ in range(self._delays.get(name, 1)):
                await asyncio.sleep(0)
            outcome = self._results.get(name, ok_result())

        if self._trace is not None:
            self._trace.append(f"end:{name}")
        if self._cancel_after_first is not None and len(self.invocations) == 1:
            self._cancel_after_first.request()
        if isinstance(outcome, BaseException):
            raise outcome
        return replace(outcome, call_id=invocation.call.call_id)


# --------------------------------------------------------------------------------------
# Fake：Hook 分发器
# --------------------------------------------------------------------------------------

#: 一个 Hook 的脚本：固定处置、要抛的异常、或一个按上下文决定的可调用对象
#: （后者用于「在这个 Hook 上请求取消」这类时序场景）。
HookScript = HookOutcome | BaseException | Callable[[HookContext], HookOutcome]

CONTINUE = HookOutcome(action=HookAction.CONTINUE)


class RecordingHookDispatcher:
    """记录型 `HookDispatcher`，默认一律 `CONTINUE`。

    `replace_tool_arguments` 是个便捷开关：设上之后 `before_tool_call` 会返回 `REPLACE`，
    把工具参数换成给定映射——「Hook 能改写工具参数」是 `HOOK_REQUIRED_SLOTS` 里
    `before_tool_call` 必须拿到 `invocation` 的**理由**，值得有一条直测。
    """

    def __init__(self, scripts: Mapping[HookName, HookScript] | None = None) -> None:
        self._scripts = dict(scripts or {})
        self.contexts: list[HookContext] = []
        self.replace_tool_arguments: Mapping[str, JsonValue] | None = None

    @property
    def hooks_seen(self) -> list[HookName]:
        return [context.hook for context in self.contexts]

    def count(self, hook: HookName) -> int:
        return self.hooks_seen.count(hook)

    def contexts_for(self, hook: HookName) -> list[HookContext]:
        return [context for context in self.contexts if context.hook is hook]

    async def dispatch(self, context: HookContext) -> HookOutcome:
        self.contexts.append(context)
        if (
            context.hook is HookName.BEFORE_TOOL_CALL
            and self.replace_tool_arguments is not None
            and context.invocation is not None
        ):
            call = replace(context.invocation.call, arguments=self.replace_tool_arguments)
            return HookOutcome(
                action=HookAction.REPLACE,
                invocation=replace(context.invocation, call=call),
            )
        script = self._scripts.get(context.hook, CONTINUE)
        if isinstance(script, BaseException):
            raise script
        if callable(script):
            return script(context)
        return script


def payload_of(context: HookContext) -> Mapping[str, JsonValue] | None:
    """便于断言的取值助手：拿到 `before_tool_call` 上下文里的工具参数。"""
    return None if context.invocation is None else context.invocation.call.arguments
