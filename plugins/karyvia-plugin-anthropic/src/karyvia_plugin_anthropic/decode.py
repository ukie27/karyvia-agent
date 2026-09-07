"""Anthropic Messages API 的**响应侧**线格式翻译。

职责：响应体 JSON → `ModelResponse`；SSE 事件 → `ModelChunk`；`stop_reason` 与 usage 映射；
流式 `tool_use` 增量按 `index` 的拼装。
不负责：发起 HTTP、读配置、把 HTTP 故障映射成 `KaryviaError`（分别在 `provider.py` /
`settings.py` / `faults.py`）——**本模块不做任何 IO**。

四件与内建 `model_openai` 不同、写错就静默丢数据的事：

- **`index` 是流式 `tool_use` 的身份键，而且这里它是唯一的选择。** `id` 与 `name` 只在
  `content_block_start` 出现一次，后续帧只有 `{"index": N, "delta": {"partial_json": …}}`。
  好消息是 Anthropic 首帧就给全 `id` 与 `name`，因此 `model_openai` 那套「补一个
  `call_auto_N` 并记进 metadata」的补救在这里不存在。
- **`usage` 分两处到达。** `input_tokens` 与两个 cache 字段只在 `message_start` 出现一次，
  `output_tokens` 在 `message_delta` 里是**累计值**（覆盖而不是相加）。
- **`input_tokens` 必须与两个 cache 字段相加。** 线格式里的 `input_tokens` 只是**未命中
  缓存的余量**，不相加会让报出去的输入量凭空少一大截（开了 prompt caching 之后差得尤其远）。
- **只解析 `data:` 行、按载荷自带的 `type` 分派。** SSE 帧同时有 `event:` 与 `data:`，两个
  真相来源会在中转改写 `event:` 时静默分叉。也没有 `[DONE]` 哨兵——流结束就是迭代结束。

**`thinking` 块经 `OpaqueBlock` 带出来**。– 期间它们被丢掉：契约里没有
放 `signature` 的地方，而没有签名的思考块回放过去会被 Anthropic 拒绝，留一半比不留更糟——
代价是 thinking 与工具调用**不能同时用**。`contracts.OpaqueBlock` 补上了那个槽位，因此这里
改为把 `thinking` / `redacted_thinking` 原样带出（连着 `signature`），由
`wire.encode_messages` 在续写时原样回传。

**三条不变的判定：**

- **`display: "omitted"` 的空思考块仍然产出 `OpaqueBlock`**（正文为空但 `signature` 有值）：
  Anthropic 要的是那个签名，正文是不是空与它无关。
- **文本增量与 opaque 块是两条独立通路**：`thinking_delta` 仍然产出
  `ChunkKind.REASONING`（给人看，进事件流不进答案），签名走 `ChunkKind.OPAQUE`。同一段思考
  因此在两个地方各出现一次，那是刻意的——它们的消费者不同。
- **块里只放 Anthropic 自己要回来的字段**，不塞我们的推断。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, cast

from karyvia.contracts import (
    ChunkKind,
    ErrorCode,
    JsonValue,
    KaryviaError,
    ModelChunk,
    ModelResponse,
    OpaqueBlock,
    StopReason,
    TokenUsage,
    ToolCall,
)

from .wire import PROVIDER_NAME, THINKING_KINDS, decode_tool_name

__all__ = [
    "SSE_DATA_PREFIX",
    "StreamDecoder",
    "decode_response",
    "decode_stop_reason",
    "decode_usage",
    "parse_sse_data",
]

#: `data:` 行前缀。SSE 允许冒号后有一个可选空格。
SSE_DATA_PREFIX: Final = "data:"

#: `stop_reason` → `StopReason` 的唯一映射表。
#:
#: **`stop_sequence` 在本项目里第一次可达**：`model_openai/wire.py` 的注释写着 OpenAI 对
#: 「自然结束」与「撞上 stop 序列」都回 `"stop"`、线格式里没有第三种取值；Anthropic 明确
#: 分得开，因此契约里那个枚举值在这里终于有了产出点。
#:
#: **`refusal` 是 HTTP 200 上的正常响应**，走 `CONTENT_FILTER` 而不是异常
#: （`is_complete_answer` 因此为假，Channel 侧的呈现规则据此区分）。
_STOP_REASONS: Final[Mapping[str, StopReason]] = {
    "end_turn": StopReason.END_TURN,
    "tool_use": StopReason.TOOL_CALLS,
    "max_tokens": StopReason.MAX_TOKENS,
    "stop_sequence": StopReason.STOP_SEQUENCE,
    "refusal": StopReason.CONTENT_FILTER,
}

_BAD_TOOL_USE: Final = "模型返回的工具调用不符合契约。"
_BAD_ARGUMENTS: Final = "模型返回的工具调用参数不是 JSON 对象。"
_BAD_SHAPE: Final = "供应商响应的形状不符合 Anthropic Messages API。"


def _external(message: str, **detail: object) -> KaryviaError:
    """本模块唯一的抛出形态：坏数据来自外部服务，不是用户也不是 Kernel 的错。"""
    return KaryviaError(ErrorCode.EXTERNAL_MODEL_PROVIDER, message, detail=detail)


def _read_int(source: Mapping[str, JsonValue], key: str) -> int:
    value = source.get(key)
    # `bool` 是 `int` 的子类，但 `"input_tokens": true` 是坏数据而不是 1。
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _mapping(value: JsonValue | None) -> Mapping[str, JsonValue]:
    return cast("Mapping[str, JsonValue]", value) if isinstance(value, dict) else {}


def _blocks(value: JsonValue | None) -> Sequence[JsonValue]:
    return value if isinstance(value, list) else ()


# ------------------------------------------------------------------------------ 用量与终态


def decode_usage(raw: JsonValue | None) -> TokenUsage:
    """`usage` 对象 → `TokenUsage`。缺失一律记 0——用量是可观测信号，缺了不该让整轮失败。

    `reasoning_tokens` **恒为 0**：Anthropic 不单独报思考 token，它们含在 `output_tokens`
    里。**不估算**——一个猜出来的数字会被当成实测值写进事件日志。
    `cost_usd` 同理留空：定价表会过期，不进代码。
    """
    usage = _mapping(raw)
    if not usage:
        return TokenUsage()
    cache_read = _read_int(usage, "cache_read_input_tokens")
    return TokenUsage(
        input_tokens=_read_int(usage, "input_tokens")
        + _read_int(usage, "cache_creation_input_tokens")
        + cache_read,
        output_tokens=_read_int(usage, "output_tokens"),
        cached_input_tokens=cache_read,
    )


def decode_stop_reason(raw: JsonValue | None, *, has_tool_calls: bool) -> StopReason:
    """`stop_reason` → `StopReason`。

    缺席或不认识（例如只有声明了 server tool 才会出现的 `pause_turn`，而本插件从不声明）
    时按 `TOOL_CALLS if has_tool_calls else END_TURN` 推断——与
    `kernel/turn/folding.py` 对「流结束但没有 DONE 分片」的推断同口径。
    """
    if isinstance(raw, str):
        known = _STOP_REASONS.get(raw)
        if known is not None:
            return known
    return StopReason.TOOL_CALLS if has_tool_calls else StopReason.END_TURN


# ------------------------------------------------------------------------------ 工具调用


def _arguments(raw: JsonValue | None, *, name: str) -> Mapping[str, JsonValue]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _external(_BAD_ARGUMENTS, name=name, actual_type=type(raw).__name__)
    return cast("Mapping[str, JsonValue]", raw)


def _tool_call(*, call_id: str, name: str, arguments: Mapping[str, JsonValue]) -> ToolCall:
    """构造一个 `ToolCall`，把契约的拒绝翻成「外部服务给了坏数据」。

    `detail` 里**只放名字，不回显参数**——参数是模型生成的自由文本，可能带着它从上下文里
    抄来的凭据。
    """
    try:
        return ToolCall(call_id=call_id, name=name, arguments=arguments)
    except KaryviaError as exc:
        raise _external(_BAD_TOOL_USE, name=name, reason=exc.code.value) from exc


# ------------------------------------------------------------------------------ 非流式解码


@dataclass(slots=True)
class _Decoded:
    """一份响应体拆出来的四样东西。"""

    text: str = ""
    calls: tuple[ToolCall, ...] = ()
    #: 要原样回传的思考块。**按到达顺序**——Anthropic 要求续写时 thinking 块排在
    #: 同一条 assistant 轮的最前面且保持原序。
    blocks: tuple[OpaqueBlock, ...] = ()
    unknown: int = 0


def _thinking_block(kind: str, block: Mapping[str, JsonValue]) -> OpaqueBlock:
    """一个 `thinking` / `redacted_thinking` 块 → `OpaqueBlock`。

    **只保留 Anthropic 自己要回来的字段**（`thinking` / `signature` / `data`），不塞我们的
    推断。原样把整个块搬进 payload 也行得通，但那会让「这里到底依赖了哪几个字段」不可读，
    而下一个人改 `wire.thinking_blocks()` 时读的正是这一条。
    """
    payload: dict[str, JsonValue] = {}
    for key in ("thinking", "signature", "data"):
        value = block.get(key)
        if isinstance(value, str) and value:
            payload[key] = value
    return OpaqueBlock(provider=PROVIDER_NAME, kind=kind, payload=payload)


def _decode_blocks(blocks: Sequence[JsonValue]) -> _Decoded:
    parts: list[str] = []
    calls: list[ToolCall] = []
    opaque: list[OpaqueBlock] = []
    unknown = 0
    for raw in blocks:
        block = _mapping(raw)
        kind = block.get("type")
        if kind == "text":
            value = block.get("text")
            if isinstance(value, str):
                parts.append(value)
        elif kind == "tool_use":
            name = block.get("name")
            call_id = block.get("id")
            if not isinstance(name, str) or not isinstance(call_id, str):
                raise _external(_BAD_SHAPE, field="content[].tool_use")
            decoded = decode_tool_name(name)
            calls.append(
                _tool_call(
                    call_id=call_id,
                    name=decoded,
                    arguments=_arguments(block.get("input"), name=decoded),
                )
            )
        elif isinstance(kind, str) and kind in THINKING_KINDS:
            opaque.append(_thinking_block(kind, block))
        else:
            unknown += 1
    return _Decoded(
        text="".join(parts),
        calls=tuple(calls),
        blocks=tuple(opaque),
        unknown=unknown,
    )


def _metadata(
    body: Mapping[str, JsonValue],
    decoded: _Decoded,
    *,
    request_id: str = "",
) -> dict[str, JsonValue]:
    """已归一化的供应商元数据。供应商私有对象连塞进来的机会都没有（契约强制）。

    `model` 记的是**服务端真的用了哪个模型**，它可能与请求的不同；`request_id` 是
    Anthropic 支持工单唯一认的东西，不敏感。
    """
    meta: dict[str, JsonValue] = {}
    for key in ("id", "model"):
        value = body.get(key)
        if isinstance(value, str) and value:
            meta[key] = value
    if request_id:
        meta["request_id"] = request_id
    stop_sequence = body.get("stop_sequence")
    if isinstance(stop_sequence, str) and stop_sequence:
        meta["stop_sequence"] = stop_sequence
    raw_stop = body.get("stop_reason")
    if isinstance(raw_stop, str) and raw_stop and raw_stop not in _STOP_REASONS:
        # 不认识的终止原因照实记下来：推断出来的那个终态是我们的结论，不是它说的。
        meta["raw_stop_reason"] = raw_stop
    if decoded.blocks:
        # 「思考发生过、而且带出来了」的观测信号。 之前这里记的是
        # `dropped_thinking_blocks`——现在没有东西被丢掉了，留着那个名字会说反话。
        meta["thinking_blocks"] = len(decoded.blocks)
    if decoded.unknown:
        meta["unknown_content_blocks"] = decoded.unknown
    usage = _mapping(body.get("usage"))
    created = _read_int(usage, "cache_creation_input_tokens")
    if created:
        # 「缓存到底写进去没有」的唯一观测信号；`TokenUsage` 里没有对应字段。
        meta["cache_creation_input_tokens"] = created
    return meta


def decode_response(
    body: Mapping[str, JsonValue], *, model_id: str, request_id: str = ""
) -> ModelResponse:
    """非流式响应体 → `ModelResponse`。"""
    decoded = _decode_blocks(_blocks(body.get("content")))
    return ModelResponse(
        model_id=model_id,
        stop_reason=decode_stop_reason(body.get("stop_reason"), has_tool_calls=bool(decoded.calls)),
        content=decoded.text,
        tool_calls=decoded.calls,
        usage=decode_usage(body.get("usage")),
        provider_metadata=_metadata(body, decoded, request_id=request_id),
        provider_blocks=decoded.blocks,
    )


# ------------------------------------------------------------------------------ 流式解码


def parse_sse_data(line: str) -> str | None:
    """从一行 SSE 里取出 `data:` 载荷。非 data 行与空行返回 `None`。"""
    if not line.startswith(SSE_DATA_PREFIX):
        return None
    return line[len(SSE_DATA_PREFIX) :].strip()


@dataclass(slots=True)
class _PendingThinking:
    """一个尚未定案的思考块。

    流式下它分三处到达：`content_block_start` 给出块类型（`thinking` / `redacted_thinking`
    与 `redacted_thinking` 的 `data`），`thinking_delta` 给正文，`signature_delta` 给签名。
    **签名通常最后到**，因此块只能在流结束时才发得出——与 `tool_use` 完全同一个理由。
    """

    kind: str = "thinking"
    text: str = ""
    signature: str = ""
    data: str = ""

    def to_block(self) -> OpaqueBlock:
        payload: dict[str, JsonValue] = {}
        for key, value in (("thinking", self.text), ("signature", self.signature), ("data", self.data)):
            if value:
                payload[key] = value
        return OpaqueBlock(provider=PROVIDER_NAME, kind=self.kind, payload=payload)


@dataclass(slots=True)
class _PendingCall:
    """一次尚未定案的 `tool_use`。`id` 与 `name` 首帧即到，只有参数分多片。"""

    call_id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass(slots=True)
class StreamDecoder:
    """把 SSE 事件推成 `ModelChunk` 的状态机。

    调用方每收到一个 `data:` 载荷就 `push()` 一次，流干净结束时 `finish()` 一次。
    `finish()` 恒以**恰好一个** `DONE` 收尾。
    """

    stop_reason: JsonValue | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    _pending: dict[int, _PendingCall] = field(default_factory=dict)
    #: 按 `index` 索引的待定思考块。`index` 是流式下唯一的身份键（与 `tool_use` 同）。
    _thinking: dict[int, _PendingThinking] = field(default_factory=dict)
    _meta: dict[str, JsonValue] = field(default_factory=dict)
    _output_tokens: int = 0

    def push(self, event: Mapping[str, JsonValue]) -> tuple[ModelChunk, ...]:
        """并入一个 SSE 事件，产出它带来的文本 / 思考增量。

        工具调用与用量要等流结束才发得出：`ModelChunk(TOOL_CALL)` 携带的是完整的
        `ToolCall`，而参数此刻还是半截字符串。
        """
        kind = event.get("type")
        if kind == "message_start":
            self._absorb_message_start(_mapping(event.get("message")))
            return ()
        if kind == "content_block_start":
            self._absorb_block_start(event)
            return ()
        if kind == "content_block_delta":
            return self._absorb_delta(event)
        if kind == "message_delta":
            self._absorb_message_delta(event)
        return ()

    def _absorb_message_start(self, message: Mapping[str, JsonValue]) -> None:
        for key in ("id", "model"):
            value = message.get(key)
            if isinstance(value, str) and value:
                self._meta.setdefault(key, value)
        # `input_tokens` 与两个 cache 字段**只在这里出现一次**。
        self.usage = decode_usage(message.get("usage"))
        created = _read_int(_mapping(message.get("usage")), "cache_creation_input_tokens")
        if created:
            self._meta["cache_creation_input_tokens"] = created

    def _absorb_block_start(self, event: Mapping[str, JsonValue]) -> None:
        block = _mapping(event.get("content_block"))
        kind = block.get("type")
        index = _read_int(event, "index")
        if isinstance(kind, str) and kind in THINKING_KINDS:
            # `redacted_thinking` 的全部内容都在这一帧的 `data` 里（它没有 delta）。
            data = block.get("data")
            signature = block.get("signature")
            self._thinking[index] = _PendingThinking(
                kind=kind,
                data=data if isinstance(data, str) else "",
                signature=signature if isinstance(signature, str) else "",
            )
            return
        if kind != "tool_use":
            return
        call_id = block.get("id")
        name = block.get("name")
        self._pending[index] = _PendingCall(
            call_id=call_id if isinstance(call_id, str) else "",
            name=decode_tool_name(name) if isinstance(name, str) else "",
        )

    def _absorb_delta(self, event: Mapping[str, JsonValue]) -> tuple[ModelChunk, ...]:
        delta = _mapping(event.get("delta"))
        kind = delta.get("type")
        if kind == "text_delta":
            text = delta.get("text")
            if isinstance(text, str) and text:
                return (ModelChunk(kind=ChunkKind.TEXT, text=text),)
            return ()
        if kind == "thinking_delta":
            text = delta.get("thinking")
            if isinstance(text, str) and text:
                self._thinking_at(event).text += text
                # 文本增量与 opaque 块是**两条独立通路**：这一条给人看（进事件流不进答案），
                # 签名那一条给 Anthropic。同一段思考在两处各出现一次是刻意的。
                return (ModelChunk(kind=ChunkKind.REASONING, text=text),)
            # `display: "omitted"` 时思考块恒为空串，跳过而不是发一堆空分片。**但块本身
            # 仍然要建**——Anthropic 要的是签名，正文是不是空与它无关。
            self._thinking_at(event)
            return ()
        if kind == "signature_delta":
            #此前它在这里被吞掉，于是 thinking 与工具调用不能同时用。
            signature = delta.get("signature")
            if isinstance(signature, str) and signature:
                self._thinking_at(event).signature += signature
            return ()
        if kind == "input_json_delta":
            fragment = delta.get("partial_json")
            if isinstance(fragment, str) and fragment:
                pending = self._pending.setdefault(_read_int(event, "index"), _PendingCall())
                # 唯一一处 `+=`：分片切在任意字节边界上，拼完才能解析。
                pending.arguments += fragment
        return ()

    def _thinking_at(self, event: Mapping[str, JsonValue]) -> _PendingThinking:
        """取（或补建）这个 `index` 上的待定思考块。

        补建是为了容错：中转如果吞掉了 `content_block_start`，签名仍然该留住。默认
        `kind="thinking"`（`redacted_thinking` 没有 delta，走不到这里）。
        """
        return self._thinking.setdefault(_read_int(event, "index"), _PendingThinking())

    def _absorb_message_delta(self, event: Mapping[str, JsonValue]) -> None:
        delta = _mapping(event.get("delta"))
        raw_stop = delta.get("stop_reason")
        if isinstance(raw_stop, str) and raw_stop:
            self.stop_reason = raw_stop
        sequence = delta.get("stop_sequence")
        if isinstance(sequence, str) and sequence:
            self._meta["stop_sequence"] = sequence
        usage = _mapping(event.get("usage"))
        if usage:
            # `output_tokens` 是**累计值**，覆盖而不是相加。
            self._output_tokens = _read_int(usage, "output_tokens")

    def finish(self) -> tuple[ModelChunk, ...]:
        """收尾：opaque 分片 → 工具调用分片 → 用量分片 → **恰好一个** DONE 分片。全部按
        `index` 升序。

        **opaque 排在最前面**：`StreamFolder` 按到达顺序累积它们，而 Anthropic 要求续写时
        thinking 块排在同一条 assistant 轮的最前面且保持原序。在这里就把序摆对，
        `wire.encode_messages` 那边因此不需要再排一次。
        """
        calls = tuple(
            _tool_call(
                call_id=pending.call_id,
                name=pending.name,
                arguments=_parse_arguments(pending.arguments, name=pending.name),
            )
            for pending in (self._pending[key] for key in sorted(self._pending))
        )
        usage = self._final_usage()
        chunks: list[ModelChunk] = [
            ModelChunk(kind=ChunkKind.OPAQUE, block=self._thinking[key].to_block())
            for key in sorted(self._thinking)
        ]
        chunks.extend(ModelChunk(kind=ChunkKind.TOOL_CALL, tool_call=call) for call in calls)
        if usage.total_tokens or usage.cached_input_tokens:
            chunks.append(ModelChunk(kind=ChunkKind.USAGE, usage=usage))
        chunks.append(
            ModelChunk(
                kind=ChunkKind.DONE,
                stop_reason=decode_stop_reason(self.stop_reason, has_tool_calls=bool(calls)),
            )
        )
        return tuple(chunks)

    def _final_usage(self) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.usage.input_tokens,
            output_tokens=self._output_tokens or self.usage.output_tokens,
            cached_input_tokens=self.usage.cached_input_tokens,
        )

    @property
    def metadata(self) -> Mapping[str, JsonValue]:
        """流式的供应商元数据。`provider.py` 不消费它，留给诊断与用例。"""
        meta = dict(self._meta)
        if self._thinking:
            meta["thinking_blocks"] = len(self._thinking)
        raw = self.stop_reason
        if isinstance(raw, str) and raw and raw not in _STOP_REASONS:
            meta["raw_stop_reason"] = raw
        return meta


def _parse_arguments(text: str, *, name: str) -> Mapping[str, JsonValue]:
    """解析累积完的参数串。**解析失败抛错而不是修复**。

    `json_repair` 是宿主仓库的依赖，但用它意味着替模型猜它想说什么，然后拿一份猜出来的
    参数去产生真实副作用。一个解析不出来的调用执行不了，悄悄丢掉又会让模型意图凭空消失，
    因此如实报错（与 `model_openai/wire.py` 同一条判定）。
    """
    stripped = text.strip()
    if not stripped:
        return {}
    try:
        # 边界窄化：参数串来自模型、经 `json.loads`，在这里定型成契约层的 `JsonValue`。
        parsed: JsonValue = json.loads(stripped)
    except ValueError as exc:
        raise _external(_BAD_ARGUMENTS, name=name, reason=type(exc).__name__) from exc
    return _arguments(parsed, name=name)
