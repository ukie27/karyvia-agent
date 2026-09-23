"""OpenAI 兼容 Chat Completions 的线格式翻译。

职责：`ModelRequest` → 请求体 JSON；响应体 JSON → `ModelResponse`；SSE 分片 → `ModelChunk`；
流式 tool_call 增量按 `index` 的拼装与 `call_id` 补救。
不负责：发起 HTTP、读配置、把异常映射成 `KaryviaError`（分别在 `provider.py` /
`settings.py` / `faults.py`）——**本模块不做任何 IO**，因此每一条线格式规则都能被单独一条
用例逐字节钉住。

四件在真实端点上被反复验证、写错就是 400 或静默丢数据的事：

- **Kernel 点分工具名不能直接进入线格式。** OpenAI-compatible 接口不接受名称中的点号；
  发送时改成保留语义的短横线名称，响应时必须按本次请求的映射恢复，不能根据模型返回值
  猜测内部工具名。

- **`index` 是流式 tool_call 的身份键，不是 `id`。** 并行调用交错到达，`id` 与
  `function.name` **只在首片**出现，后续片只有 `{"index": 0, "function": {"arguments": …}}`。
  `arguments` 是被任意切碎的 JSON **字符串**，只能 `+=` 拼接，绝不能在流结束前解析。
  判空一律用真值而不是 `is not None`——有端点在首片发 `"arguments": ""`，用后者会把已累积
  的状态清掉。
- **`call_id` 去重是强制项。** `ModelResponse.__post_init__` 对重复 `call_id` 直接抛错，
  而部分端点会让并行调用共用一个 `id`（或干脆不给）。补了什么必须在
  `provider_metadata` 里查得到——静默补一个 id 和静默丢一个调用一样不可接受。
- **带 `tool_calls` 的 assistant 消息 `content` 必须是 `null`**，`function.arguments`
  必须始终是 JSON 对象字符串（缺省 `"{}"`），没有 tools 时 `tools` 与 `tool_choice` 两个键
  都省掉。这三条都不是风格问题，发错就是 400。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, cast

from karyvia.contracts import (
    ChunkKind,
    ErrorCode,
    JsonValue,
    KaryviaError,
    ModelChunk,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    Role,
    StopReason,
    TokenUsage,
    ToolCall,
    ToolSpec,
)

__all__ = [
    "MAX_COMPLETION_TOKENS_FIELD",
    "MAX_TOKENS_FIELD",
    "SSE_DONE",
    "StreamDecoder",
    "ToolCallAccumulator",
    "ToolNameMap",
    "build_payload",
    "decode_response",
    "decode_stop_reason",
    "decode_usage",
    "encode_messages",
    "encode_tools",
    "parse_sse_data",
    "strip_lone_surrogates",
]

#: 两种输出上限字段名。gpt-5、o1/o3/o4 只认后者，发错就是 400——所以它是配置项而不是
#: 不维护按模型名前缀猜测行为的表；该信息由配置明确声明。
MAX_TOKENS_FIELD: Final = "max_tokens"
MAX_COMPLETION_TOKENS_FIELD: Final = "max_completion_tokens"

#: SSE 流的终止哨兵。它不是 JSON，先判它再 `json.loads`。
SSE_DONE: Final = "[DONE]"

#: `data:` 行前缀。SSE 允许冒号后有一个可选空格。
_SSE_DATA_PREFIX: Final = "data:"

#: 孤立的 UTF-16 代理码位。Windows 控制台粘贴进来的文本会带上它们，而 `str.encode("utf-8")`
#: 对它们抛 `UnicodeEncodeError`——那是第三方原生异常从 `complete()` 逸出
#: （`protocols.py` 禁止），用户只会看到一段与模型毫无关系的 traceback。
_LONE_SURROGATE: Final = re.compile("[\ud800-\udfff]")

#: 供应商 `finish_reason` 到 `StopReason` 的唯一映射表。
#:
#: **`stop_sequence` 不可达**：OpenAI 对「自然结束」与「撞上 stop 序列」都回 `"stop"`，
#: 线格式里没有第三种取值。契约有那个枚举值不等于这个 Provider 分得出来，因此不猜。
_STOP_REASONS: Final[Mapping[str, StopReason]] = {
    "stop": StopReason.END_TURN,
    "tool_calls": StopReason.TOOL_CALLS,
    "function_call": StopReason.TOOL_CALLS,
    "length": StopReason.MAX_TOKENS,
    "content_filter": StopReason.CONTENT_FILTER,
}

#: 缓存 token 的三种厂商拼法，按优先级排列。少认一种就是把 `prompt_caching` 的观测
#: 信号丢掉，而那正是用户判断「缓存到底生效没有」的唯一依据。
_CACHED_TOKEN_PATHS: Final[tuple[tuple[str, ...], ...]] = (
    ("prompt_tokens_details", "cached_tokens"),
    ("cached_tokens",),
    ("prompt_cache_hit_tokens",),
)

_BAD_TOOL_CALL: Final = "模型返回的工具调用不符合契约。"
_BAD_ARGUMENTS: Final = "模型返回的工具调用参数不是合法的 JSON 对象。"
_BAD_RESPONSE_SHAPE: Final = "供应商响应的形状不符合 OpenAI Chat Completions。"
_UNKNOWN_WIRE_TOOL: Final = "模型返回了本次请求未声明的工具名。"

#: OpenAI function.name 的上限比部分兼容端点更严格；取 64 可同时覆盖两边。
_WIRE_TOOL_NAME_MAX_LENGTH: Final = 64


def _external(message: str, **detail: object) -> KaryviaError:
    """本模块唯一的抛出形态：坏数据来自外部服务，不是用户也不是 Kernel 的错。"""
    return KaryviaError(ErrorCode.EXTERNAL_MODEL_PROVIDER, message, detail=detail)


class ToolNameMap:
    """一次请求内 Kernel 工具名与供应商线格式名称的双向映射。

    Kernel 用点分名称表达命名空间，但 OpenAI-compatible 接口只接受字母、数字、下划线和
    短横线。Kernel 名称自身不允许短横线，因此 ``.`` → ``-`` 既保留语义又不会与另一个
    合法 Kernel 名称碰撞。过长名称保留前缀并附稳定摘要；真正的反向恢复始终查表，不根据
    外部字符串猜测。
    """

    __slots__ = ("_canonical_to_wire", "_wire_to_callable")

    def __init__(
        self,
        canonical_to_wire: Mapping[str, str],
        wire_to_callable: Mapping[str, str],
    ) -> None:
        self._canonical_to_wire = dict(canonical_to_wire)
        self._wire_to_callable = dict(wire_to_callable)

    @classmethod
    def for_request(cls, request: ModelRequest) -> ToolNameMap:
        """覆盖工具声明与历史调用；只有本次声明的工具允许从响应映回 Kernel。"""
        callable_names = {spec.name for spec in request.tools}
        ordered_names = list(dict.fromkeys(spec.name for spec in request.tools))
        ordered_names.extend(
            name
            for name in dict.fromkeys(
                call.name for message in request.messages for call in message.tool_calls
            )
            if name not in callable_names
        )

        canonical_to_wire: dict[str, str] = {}
        used: dict[str, str] = {}
        for canonical in ordered_names:
            wire = cls._wire_name(canonical, used)
            canonical_to_wire[canonical] = wire
            used[wire] = canonical
        return cls(
            canonical_to_wire,
            {
                canonical_to_wire[name]: name
                for name in callable_names
                if name in canonical_to_wire
            },
        )

    @staticmethod
    def _wire_name(canonical: str, used: Mapping[str, str]) -> str:
        semantic = canonical.replace(".", "-")
        if len(semantic) <= _WIRE_TOOL_NAME_MAX_LENGTH and semantic not in used:
            return semantic

        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        suffix = f"-{digest}"
        candidate = f"{semantic[: _WIRE_TOOL_NAME_MAX_LENGTH - len(suffix)]}{suffix}"
        if candidate not in used or used[candidate] == canonical:
            return candidate

        # 摘要碰撞极不现实，但线格式名称必须确定地保持唯一，不能靠概率维护调用对应关系。
        ordinal = 1
        while True:
            suffix = f"-{digest}-{ordinal}"
            candidate = f"{semantic[: _WIRE_TOOL_NAME_MAX_LENGTH - len(suffix)]}{suffix}"
            if candidate not in used or used[candidate] == canonical:
                return candidate
            ordinal += 1

    def encode(self, canonical: str) -> str:
        """将 Kernel 名称写入请求；缺席表示请求结构没有被完整纳入映射。"""
        try:
            return self._canonical_to_wire[canonical]
        except KeyError as exc:
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "模型请求中的工具名没有对应的线格式映射。",
                detail={"name": canonical},
            ) from exc

    def decode_callable(self, wire: str) -> str:
        """只接受本次 request.tools 声明过的别名，拒绝模型臆造或调用旧工具。"""
        try:
            return self._wire_to_callable[wire]
        except KeyError as exc:
            raise _external(_UNKNOWN_WIRE_TOOL, name=wire) from exc


def strip_lone_surrogates(text: str) -> str:
    """剔除孤立代理码位。

    这是本模块唯一一处**输入规整**。它不是「静默修正坏输入」的例外，而是序列化边界的
    必需品：留着它们，`httpx` 会在编码请求体时抛 `UnicodeEncodeError`，一次正常的对话
    因为用户粘贴了一段 Windows 控制台文本就整轮失败，且错误信息指不到原因。
    合法文本经过它逐字符不变。
    """
    return _LONE_SURROGATE.sub("", text)


# ------------------------------------------------------------------------------ 请求编码


def _encode_tool_calls(calls: Sequence[ToolCall], tool_names: ToolNameMap) -> list[JsonValue]:
    """assistant 消息上的 `tool_calls`。

    `arguments` **始终**是 JSON 对象字符串：契约里它是已解析的 `Mapping`，而线格式要的是
    字符串，空参数的地板值是 `"{}"` 而不是缺席或 `null`。
    """
    return [
        {
            "id": call.call_id,
            "type": "function",
            "function": {
                "name": tool_names.encode(call.name),
                "arguments": json.dumps(dict(call.arguments), ensure_ascii=False),
            },
        }
        for call in calls
    ]


def encode_messages(
    messages: Sequence[ModelMessage], tool_names: ToolNameMap
) -> list[JsonValue]:
    """把契约消息投影成 OpenAI 线格式（投影可以变，持久化格式不跟着变）。

    **带 `tool_calls` 的 assistant 消息 `content` 必须是 `null`**：多个兼容网关会拒绝
    「正文与 tool_calls 同时非空」的 assistant 消息。这不是我们的偏好，是它们的校验。
    """
    encoded: list[JsonValue] = []
    for message in messages:
        item: dict[str, JsonValue] = {"role": message.role.value}
        if message.role is Role.ASSISTANT and message.tool_calls:
            item["content"] = None
            item["tool_calls"] = _encode_tool_calls(message.tool_calls, tool_names)
        else:
            item["content"] = strip_lone_surrogates(message.content)
        if message.tool_call_id is not None:
            item["tool_call_id"] = message.tool_call_id
        encoded.append(item)
    return encoded


def encode_tools(tools: Sequence[ToolSpec], tool_names: ToolNameMap) -> list[JsonValue]:
    """工具声明。`parameters` 已经是 JSON Schema，原样透传。"""
    return [
        {
            "type": "function",
            "function": {
                "name": tool_names.encode(spec.name),
                "description": spec.description,
                "parameters": spec.parameters,
            },
        }
        for spec in tools
    ]


def build_payload(
    request: ModelRequest,
    *,
    tool_names: ToolNameMap | None = None,
    max_tokens_field: str = MAX_TOKENS_FIELD,
    supports_temperature: bool = True,
    default_max_output_tokens: int,
    stream: bool = False,
    include_usage: bool = False,
) -> dict[str, JsonValue]:
    """组装请求体。

    `supports_temperature=False` 时 `temperature` 被**省略而不是钳到某个值**：推理模型
    对这个字段直接 400，而替用户挑一个温度是在替它改采样行为。
    """
    names = tool_names or ToolNameMap.for_request(request)
    payload: dict[str, JsonValue] = {
        "model": request.model_id,
        "messages": encode_messages(request.messages, names),
    }
    params = request.params
    if params.temperature is not None and supports_temperature:
        payload["temperature"] = params.temperature
    if params.top_p is not None:
        payload["top_p"] = params.top_p
    if params.stop_sequences:
        payload["stop"] = list(params.stop_sequences)
    if params.seed is not None:
        payload["seed"] = params.seed
    payload[max_tokens_field] = params.max_output_tokens or default_max_output_tokens
    # 没有工具时两个键都省掉：只发 `tool_choice` 会让若干网关直接 400。
    if request.tools:
        payload["tools"] = encode_tools(request.tools, names)
        payload["tool_choice"] = "auto"
    if stream:
        payload["stream"] = True
        if include_usage:
            payload["stream_options"] = {"include_usage": True}
    return payload


# ------------------------------------------------------------------------------ 响应解码


def _as_mapping(value: object, *, where: str) -> Mapping[str, JsonValue]:
    if not isinstance(value, dict):
        raise _external(_BAD_RESPONSE_SHAPE, field=where, actual_type=type(value).__name__)
    # 边界窄化：响应体来自 `json.loads`，在这里定型成契约层的 `JsonValue`。
    return cast("Mapping[str, JsonValue]", value)


def _read_int(source: Mapping[str, JsonValue], key: str) -> int:
    value = source.get(key)
    # `bool` 是 `int` 的子类，但 `"input_tokens": true` 是坏数据而不是 1。
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _read_cached_tokens(usage: Mapping[str, JsonValue]) -> int:
    for path in _CACHED_TOKEN_PATHS:
        cursor: JsonValue | None = dict(usage)
        for step in path:
            cursor = cursor.get(step) if isinstance(cursor, Mapping) else None
        if isinstance(cursor, int) and not isinstance(cursor, bool) and cursor >= 0:
            return cursor
    return 0


def decode_usage(body: Mapping[str, JsonValue]) -> TokenUsage:
    """用量统计。缺失一律记 0——用量是可观测信号，缺了不该让整轮失败。"""
    raw = body.get("usage")
    if not isinstance(raw, Mapping):
        return TokenUsage()
    details = raw.get("completion_tokens_details")
    reasoning = _read_int(details, "reasoning_tokens") if isinstance(details, Mapping) else 0
    return TokenUsage(
        input_tokens=_read_int(raw, "prompt_tokens"),
        output_tokens=_read_int(raw, "completion_tokens"),
        cached_input_tokens=_read_cached_tokens(raw),
        reasoning_tokens=reasoning,
    )


def decode_stop_reason(finish_reason: object, *, has_tool_calls: bool) -> StopReason:
    """`finish_reason` → `StopReason`。

    缺席或不认识时按 `TOOL_CALLS if has_tool_calls else END_TURN` 推断——与
    `kernel/turn/folding.py` 对「流结束但没有 DONE 分片」的推断同口径。
    """
    if isinstance(finish_reason, str):
        known = _STOP_REASONS.get(finish_reason)
        if known is not None:
            return known
    return StopReason.TOOL_CALLS if has_tool_calls else StopReason.END_TURN


@dataclass(slots=True)
class _PendingCall:
    """一次尚未定案的工具调用。三个字段都可能分多片到达。"""

    call_id: str = ""
    name: str = ""
    arguments: str = ""

    def absorb(self, delta: Mapping[str, JsonValue]) -> None:
        """并入一片增量。**一律用真值判断**，空串不得覆盖已累积的内容。"""
        raw_id = delta.get("id")
        if isinstance(raw_id, str) and raw_id:
            self.call_id = raw_id
        function = delta.get("function")
        if not isinstance(function, Mapping):
            return
        name = function.get("name")
        if isinstance(name, str) and name:
            self.name = name
        arguments = function.get("arguments")
        if isinstance(arguments, str) and arguments:
            # 唯一一处 `+=`：分片切在任意字节边界上，拼完才能解析。
            self.arguments += arguments


def _decode_arguments(text: str) -> Mapping[str, JsonValue]:
    """解析累积完的参数串。**解析失败抛错而不是修复**。

    `json_repair` 是仓库依赖，但用它意味着替模型猜它想说什么，然后拿一份猜出来的参数去
    产生真实副作用。一个解析不出来的调用执行不了，悄悄丢掉又会让模型意图凭空消失，
    所以如实报错。
    """
    stripped = text.strip()
    if not stripped:
        return {}
    try:
        parsed = json.loads(stripped)
    except ValueError as exc:
        raise _external(_BAD_ARGUMENTS, reason=type(exc).__name__) from exc
    if not isinstance(parsed, dict):
        raise _external(_BAD_ARGUMENTS, actual_type=type(parsed).__name__)
    # 边界窄化：参数串来自模型、经 `json.loads`，定型成契约层的 `JsonValue`。
    return cast("Mapping[str, JsonValue]", parsed)


def _finalize(
    pending: Sequence[_PendingCall], tool_names: ToolNameMap
) -> tuple[tuple[ToolCall, ...], int]:
    """定案：补齐缺失/重复的 `call_id`，解析参数，构造 `ToolCall`。

    返回补救次数，由调用方写进 `provider_metadata`——补了什么必须查得到。
    """
    seen: set[str] = set()
    repairs = 0
    calls: list[ToolCall] = []
    for index, item in enumerate(pending):
        call_id = item.call_id
        if not call_id or call_id in seen:
            repairs += 1
            call_id = f"call_auto_{index}"
            while call_id in seen:  # pragma: no cover - 需要模型真的吐出 call_auto_N
                call_id = f"{call_id}_x"
        seen.add(call_id)
        try:
            calls.append(
                ToolCall(
                    call_id=call_id,
                    name=tool_names.decode_callable(item.name),
                    arguments=_decode_arguments(item.arguments),
                )
            )
        except KaryviaError as exc:
            if exc.code is ErrorCode.EXTERNAL_MODEL_PROVIDER:
                raise
            # 契约拒绝了模型给的形状（多半是工具名不合 `fs.read` 那个式样）。
            # 不做大小写规整——那是替模型改主意；`detail` 只放名字，不回显参数。
            raise _external(_BAD_TOOL_CALL, name=item.name, reason=exc.code.value) from exc
    return tuple(calls), repairs


class ToolCallAccumulator:
    """流式 tool_call 的按 `index` 累积器。

    `index` 缺席时退回该片在 `delta.tool_calls` 数组里的位置——非规范网关会漏这个字段，
    而漏了之后唯一还站得住的相关性就是数组顺序。
    """

    __slots__ = ("_pending", "_tool_names")

    def __init__(self, tool_names: ToolNameMap) -> None:
        self._pending: dict[int, _PendingCall] = {}
        self._tool_names = tool_names

    def absorb(self, deltas: Sequence[JsonValue]) -> None:
        """并入一个分片里的全部 tool_call 增量。"""
        for position, raw in enumerate(deltas):
            if not isinstance(raw, Mapping):
                continue
            index = raw.get("index")
            key = index if isinstance(index, int) and not isinstance(index, bool) else position
            self._pending.setdefault(key, _PendingCall()).absorb(raw)

    @property
    def empty(self) -> bool:
        return not self._pending

    def finish(self) -> tuple[tuple[ToolCall, ...], int]:
        """按 `index` 升序定案。返回 `(调用, 补救次数)`。"""
        ordered = [self._pending[key] for key in sorted(self._pending)]
        return _finalize(ordered, self._tool_names)


def _decode_message_tool_calls(
    message: Mapping[str, JsonValue], tool_names: ToolNameMap
) -> tuple[tuple[ToolCall, ...], int]:
    raw = message.get("tool_calls")
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        return (), 0
    pending: list[_PendingCall] = []
    for item in raw:
        call = _PendingCall()
        if isinstance(item, Mapping):
            call.absorb(item)
        pending.append(call)
    return _finalize(pending, tool_names)


def _metadata(body: Mapping[str, JsonValue], repairs: int) -> dict[str, JsonValue]:
    """已归一化的供应商元数据。供应商私有对象连塞进来的机会都没有（契约强制）。"""
    meta: dict[str, JsonValue] = {}
    for key in ("id", "system_fingerprint"):
        value = body.get(key)
        if isinstance(value, str) and value:
            meta[key] = value
    if repairs:
        meta["repaired_tool_call_ids"] = repairs
    return meta


def decode_response(
    body: Mapping[str, JsonValue], *, model_id: str, tool_names: ToolNameMap
) -> ModelResponse:
    """非流式响应体 → `ModelResponse`。

    **内容过滤是 HTTP 200 上的正常响应**，走 `StopReason.CONTENT_FILTER` 而不是异常
    （`is_complete_answer` 因此为假，Channel 侧的呈现规则据此区分）。
    """
    choices = body.get("choices")
    if not isinstance(choices, Sequence) or isinstance(choices, str | bytes) or not choices:
        raise _external(_BAD_RESPONSE_SHAPE, field="choices")
    choice = _as_mapping(choices[0], where="choices[0]")
    message = _as_mapping(choice.get("message", {}), where="choices[0].message")
    tool_calls, repairs = _decode_message_tool_calls(message, tool_names)
    raw_content = message.get("content")
    return ModelResponse(
        model_id=model_id,
        stop_reason=decode_stop_reason(
            choice.get("finish_reason"), has_tool_calls=bool(tool_calls)
        ),
        content=raw_content if isinstance(raw_content, str) else "",
        tool_calls=tool_calls,
        usage=decode_usage(body),
        provider_metadata=_metadata(body, repairs),
    )


# ------------------------------------------------------------------------------ 流式解码


def parse_sse_data(line: str) -> str | None:
    """从一行 SSE 里取出 `data:` 载荷。非 data 行与空行返回 `None`。"""
    if not line.startswith(_SSE_DATA_PREFIX):
        return None
    return line[len(_SSE_DATA_PREFIX) :].strip()


@dataclass(slots=True)
class StreamDecoder:
    """把 SSE 分片推成 `ModelChunk` 的状态机。

    **`finish_reason` 不一定在最后一片上**：开了 `stream_options.include_usage` 时，收尾
    那一片的 `choices` 是空数组、只带 `usage`。因此终止原因每片都读、后到的覆盖先到的，
    而不是「取最后一片的」。
    """

    tool_names: ToolNameMap
    finish_reason: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    _meta: dict[str, JsonValue] = field(default_factory=dict)
    calls: ToolCallAccumulator = field(init=False)

    def __post_init__(self) -> None:
        self.calls = ToolCallAccumulator(self.tool_names)

    def push(self, event: Mapping[str, JsonValue]) -> tuple[ModelChunk, ...]:
        """并入一个 SSE 分片，产出它带来的**文本**增量。

        工具调用与用量要等流结束才发得出：`ModelChunk(TOOL_CALL)` 携带的是完整的
        `ToolCall`，而参数此刻还是半截字符串。
        """
        for key in ("id", "system_fingerprint"):
            value = event.get(key)
            if isinstance(value, str) and value and key not in self._meta:
                self._meta[key] = value
        raw_usage = event.get("usage")
        if isinstance(raw_usage, Mapping):
            self.usage = decode_usage(event)
        choices = event.get("choices")
        if not isinstance(choices, Sequence) or isinstance(choices, str | bytes) or not choices:
            return ()
        choice = _as_mapping(choices[0], where="choices[0]")
        reason = choice.get("finish_reason")
        if isinstance(reason, str) and reason:
            self.finish_reason = reason
        delta = choice.get("delta")
        if not isinstance(delta, Mapping):
            return ()
        tool_deltas = delta.get("tool_calls")
        if isinstance(tool_deltas, Sequence) and not isinstance(tool_deltas, str | bytes):
            self.calls.absorb(tool_deltas)
        content = delta.get("content")
        if isinstance(content, str) and content:
            return (ModelChunk(kind=ChunkKind.TEXT, text=content),)
        return ()

    def finish(self) -> tuple[ModelChunk, ...]:
        """收尾：工具调用分片 → 用量分片 → **恰好一个** DONE 分片。"""
        calls, repairs = self.calls.finish()
        if repairs:
            self._meta["repaired_tool_call_ids"] = repairs
        chunks: list[ModelChunk] = [
            ModelChunk(kind=ChunkKind.TOOL_CALL, tool_call=call) for call in calls
        ]
        if self.usage.total_tokens or self.usage.cached_input_tokens:
            chunks.append(ModelChunk(kind=ChunkKind.USAGE, usage=self.usage))
        chunks.append(
            ModelChunk(
                kind=ChunkKind.DONE,
                stop_reason=decode_stop_reason(
                    self.finish_reason or None, has_tool_calls=bool(calls)
                ),
            )
        )
        return tuple(chunks)

    @property
    def metadata(self) -> Mapping[str, JsonValue]:
        return dict(self._meta)
