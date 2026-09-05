"""Anthropic Messages API 的**请求侧**线格式翻译（开发方案 `D32`）。

职责：`ModelRequest` → `POST /messages` 请求体：system 提升、`tool_result` 折叠、消息序列
规整、工具名与 `tool_use_id` 编码、`cache_control` 布点、thinking 四形态。
不负责：发起 HTTP、读配置、解码响应或 SSE（分别在 `provider.py` / `settings.py` /
`decode.py`）——**本模块不做任何 IO**，因此每一条线格式规则都能被单独一条用例逐字节钉住。

四件写错就是 400 或静默丢数据的事：

- **工具名必须编码。** `contracts/tool.py` 的工具名式样是点分命名空间（`fs.read`），
  而 Anthropic 的 `tools[].name` 只收 `^[a-zA-Z0-9_-]{1,64}$`——`.` 直接 400。契约名恒不含
  `-`，因此 `.` ↔ `-` 是**无碰撞双射**，`encode_tool_name` / `decode_tool_name` 各自可逆。
  这与内建 `model_openai` 那句「工具名原样透传」是本模块最大的一处差异。
- **`tool_result` 是 user 轮里的一个块，不是一条 tool 消息。** 契约的 `Role.TOOL` 在
  Anthropic 这边没有对应角色，必须折进**前一条 user 轮**；前一条不是 user 就新开一条。
- **不许尾部 assistant，也不许首部 assistant。** 前者是 prefill（Anthropic 直接 400），
  后者会让 `messages[0].role` 不是 `user`。两条的补救都要跳过带 `tool_use` 的消息——
  `tool_use` 块在 user 轮里非法，改投或前插会把 `tool_use`/`tool_result` 配对拆散，
  把一个可恢复的 400 变成更难诊断的那种。
- **`max_tokens` 是必填字段。** OpenAI 那边省略它意味着「用服务端默认」，这里没有这一档。

**不移植 legacy 的四张按模型名版本号 gating 的表**（`_ADAPTIVE_ONLY_MIN_VERSIONS` 等）。
`D19` 拒过同类的 slug 猜表，理由不变：表只会越滚越大，而用户换一个新模型要等我们发版。
四种 thinking 形状因此由 `ThinkingSpec.mode` 直接选，运维改一行配置即可。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from karyvia.contracts import (
    JsonValue,
    ModelMessage,
    ModelRequest,
    OpaqueBlock,
    Role,
    ToolCall,
    ToolSpec,
)

__all__ = [
    "CACHE_TTLS",
    "CONVERSATION_CONTINUED",
    "EFFORT_LEVELS",
    "EMPTY_TEXT",
    "MESSAGES_PATH",
    "PROVIDER_NAME",
    "THINKING_ADAPTIVE",
    "THINKING_BUDGET",
    "THINKING_KINDS",
    "THINKING_DISABLED",
    "THINKING_MODES",
    "THINKING_OFF",
    "CachingSpec",
    "ThinkingSpec",
    "build_payload",
    "decode_tool_name",
    "encode_messages",
    "encode_tool_name",
    "encode_tools",
    "normalize_turns",
    "sanitize_tool_id",
    "strip_lone_surrogates",
]

#: 请求路径。拼在 `base_url` 之后，`base_url` 自身原样使用。
MESSAGES_PATH: Final = "/messages"

#: `ModelInfo.provider` 与 `OpaqueBlock.provider`：诊断里「这个回答是谁生成的」的答案，
#: 也是 opaque 块的所有权标记（`D45`）。**定义在这里而不是 `settings.py`**：`settings`
#: 已经 import 本模块，反向 import 会成环；而这个串本来就是线格式一侧的身份。
#: `settings.py` 原样再导出它，既有引用一个没变。
PROVIDER_NAME: Final = "anthropic"

#: 两种要原样回传的思考块（`D45`）。`redacted_thinking` 的内容被供应商加密了——它同样
#: 必须回传，而且**只能**原样回传（我们连读都读不了）。`decode.py` 产出它们，
#: `thinking_blocks()` 把它们编回线格式。
THINKING_KINDS: Final[tuple[str, ...]] = ("thinking", "redacted_thinking")

#: 四种 thinking 形态。它们是**线格式的四种形状**，不是四个模型家族——选哪一个由运维
#: 按自己在用的模型决定，本模块不猜。
THINKING_OFF: Final = "off"
THINKING_ADAPTIVE: Final = "adaptive"
THINKING_BUDGET: Final = "budget"
THINKING_DISABLED: Final = "disabled"
THINKING_MODES: Final[frozenset[str]] = frozenset(
    {THINKING_OFF, THINKING_ADAPTIVE, THINKING_BUDGET, THINKING_DISABLED}
)

#: `cache_control` 支持的两个 TTL。
CACHE_TTLS: Final[frozenset[str]] = frozenset({"5m", "1h"})

#: `output_config.effort` 的取值。
EFFORT_LEVELS: Final[frozenset[str]] = frozenset({"low", "medium", "high", "xhigh", "max"})

#: 首轮是 assistant 时前插的合成 user 轮。内容是常量而不是随机文案——它会进模型上下文，
#: 每次不一样会让 prompt caching 的前缀失效。
CONVERSATION_CONTINUED: Final = "(conversation continued)"

#: Anthropic 拒绝空的 `text` 块，而契约允许空 `content`（例如只带工具调用的 assistant
#: 轮被改投成 user 轮之后）。这是那种情况下的地板值。
EMPTY_TEXT: Final = "(empty)"

#: Anthropic 的 `tool_use.id` / `tool_result.tool_use_id` 式样。不匹配即 400。
_TOOL_ID_PATTERN: Final = re.compile(r"^[a-zA-Z0-9_-]+$")
_TOOL_ID_ILLEGAL: Final = re.compile(r"[^a-zA-Z0-9_-]")
_TOOL_ID_MAX: Final = 48

#: 孤立的 UTF-16 代理码位。理由与 `model_openai/wire.py` 那份完全相同：留着它们，
#: httpx 会在编码请求体时抛 `UnicodeEncodeError`，一次正常对话因为用户粘贴了一段
#: Windows 控制台文本就整轮失败，且错误信息指不到原因。
_LONE_SURROGATE: Final = re.compile("[\ud800-\udfff]")

#: 每请求的 `cache_control` 断点上限（Anthropic 的硬限制）。本模块按构造最多放 3 个
#: （tools / system / history），因此这个常量只是让那条约束在代码里查得到。
MAX_CACHE_BREAKPOINTS: Final = 4


def strip_lone_surrogates(text: str) -> str:
    """剔除孤立代理码位。合法文本经过它逐字符不变。"""
    return _LONE_SURROGATE.sub("", text)


# ------------------------------------------------------------------------------ 名字与 id


def encode_tool_name(name: str) -> str:
    """契约工具名 → Anthropic 工具名。

    契约式样是 `^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)*$`（有 `.`、没有 `-`），Anthropic 收的是
    `^[a-zA-Z0-9_-]{1,64}$`（有 `-`、没有 `.`）。因为契约名里**永远不会出现 `-`**，
    `.` → `-` 是无碰撞的双射，`decode_tool_name` 原路还原。
    """
    return name.replace(".", "-")


def decode_tool_name(name: str) -> str:
    """Anthropic 工具名 → 契约工具名。`encode_tool_name` 的逆运算。"""
    return name.replace("-", ".")


def sanitize_tool_id(raw: str) -> str:
    """把任意来源的 `call_id` 收窄成 Anthropic 接受的形状。

    历史里的 `call_id` 可能来自另一个 Provider（OpenAI 的 `call_xxx` 没问题，但网关会发
    带管道符或点号的 id），发过去就是一句 "String should match pattern" 的 400。
    命中式样的原样返回——绝大多数情况下这是一次恒等变换，历史因此不会因为换 Provider 而
    产生一批新 id。改写过的追加 8 位摘要，让两个只差非法字符的 id 不会撞在一起。
    """
    if raw and _TOOL_ID_PATTERN.match(raw):
        return raw
    digest = hashlib.sha1(raw.encode("utf-8", "surrogatepass")).hexdigest()[:8]
    stem = _TOOL_ID_ILLEGAL.sub("_", raw)[:_TOOL_ID_MAX].strip("_")
    return f"{stem}_{digest}" if stem else f"toolu_{digest}"


def _id_map(messages: Sequence[ModelMessage]) -> dict[str, str]:
    """整份消息列表跑一次，建「原始 call_id → 清洗后 id」的映射。

    **刻意不移植 legacy 的 `pending_tool_ids: dict[str, deque[str]]`。** 那套队列存在是
    因为 legacy 回放的是无类型 dict、同一个原始 id 可能在历史里出现多次；而契约层的
    `ModelResponse.__post_init__` 已经强制单次响应内 `call_id` 唯一，一张 dict 就够。
    清洗后撞车时追加序号——两个不同的原始 id 必须映射到两个不同的目标 id，否则
    `tool_result` 会认错它对应的 `tool_use`。
    """
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for message in messages:
        for call in message.tool_calls:
            if call.call_id in mapping:
                continue
            candidate = sanitize_tool_id(call.call_id)
            suffix = 2
            while candidate in used:
                candidate = f"{sanitize_tool_id(call.call_id)}_{suffix}"
                suffix += 1
            used.add(candidate)
            mapping[call.call_id] = candidate
    return mapping


# ------------------------------------------------------------------------------ 消息编码


def _text_block(text: str) -> dict[str, JsonValue]:
    return {"type": "text", "text": strip_lone_surrogates(text)}


def _tool_use_blocks(calls: Sequence[ToolCall], ids: Mapping[str, str]) -> list[JsonValue]:
    return [
        {
            "type": "tool_use",
            "id": ids.get(call.call_id, sanitize_tool_id(call.call_id)),
            "name": encode_tool_name(call.name),
            "input": dict(call.arguments),
        }
        for call in calls
    ]


def _blocks_of(turn: Mapping[str, JsonValue]) -> list[JsonValue]:
    content = turn.get("content")
    return list(content) if isinstance(content, list) else []


def _has_tool_use(turn: Mapping[str, JsonValue]) -> bool:
    """该轮是否带 `tool_use` 块。

    带 `tool_use` 的消息不能被改投成 user 轮（那种块在 user 轮里非法），也不该被前插一条
    合成轮隔开（会让紧随其后的 `tool_result` 找不到配对）。
    """
    return any(
        isinstance(block, Mapping) and block.get("type") == "tool_use" for block in _blocks_of(turn)
    )


def _append_block(turns: list[dict[str, JsonValue]], block: JsonValue) -> None:
    """把一个块追加进最后一条 user 轮；没有就新开一条。"""
    if turns and turns[-1].get("role") == "user":
        turns[-1]["content"] = [*_blocks_of(turns[-1]), block]
        return
    turns.append({"role": "user", "content": [block]})


def encode_messages(
    messages: Sequence[ModelMessage],
) -> tuple[list[JsonValue], list[dict[str, JsonValue]]]:
    """契约消息 → `(system 块, messages)`（`EDG-305`：投影可以变，持久化格式不跟着变）。

    system 恒为**块数组**而不是字符串：`cache_control` 只能挂在块上，做成字符串就等于
    放弃在 system 上设断点。多条 system 消息变成多个 text 块，不拼成一个——拼接会让
    「哪一段是谁加的」不可分，而组装器本来就按片段交下来。
    """
    ids = _id_map(messages)
    system: list[JsonValue] = []
    turns: list[dict[str, JsonValue]] = []
    for message in messages:
        if message.role is Role.SYSTEM:
            if message.content:
                system.append(_text_block(message.content))
            continue
        if message.role is Role.TOOL:
            raw_id = message.tool_call_id or ""
            _append_block(
                turns,
                {
                    "type": "tool_result",
                    "tool_use_id": ids.get(raw_id, sanitize_tool_id(raw_id)),
                    "content": strip_lone_surrogates(message.content),
                },
            )
            continue
        if message.role is Role.ASSISTANT:
            # **thinking 块必须排在最前面**（`D45`）：Anthropic 要求续写时把上一轮的
            # thinking 块原样放回同一条 assistant 轮的开头，顺序也要保持。
            blocks: list[JsonValue] = list(thinking_blocks(message.provider_blocks))
            if message.content:
                blocks.append(_text_block(message.content))
            blocks.extend(_tool_use_blocks(message.tool_calls, ids))
            turns.append({"role": "assistant", "content": blocks})
            continue
        turns.append({"role": "user", "content": [_text_block(message.content)]})
    return system, normalize_turns(turns)


def thinking_blocks(blocks: Sequence[OpaqueBlock]) -> list[dict[str, JsonValue]]:
    """`OpaqueBlock` → Anthropic 的 `thinking` / `redacted_thinking` 块（`D45`）。

    **不是自己产出的一律跳过**（`OpaqueBlock.owned_by`，`EDG-305`）：`payload` 的形状是私有
    的，别家的 `thinking` 块不是同一种东西，把它当成自己的塞进请求体换来的是一个 400。
    切换 Provider 之后同一段会话历史因此**不会**带着上一家的块跑。

    **没有 `signature` 的 `thinking` 块也跳过**：Anthropic 拒绝无签名的思考块，留一半比
    不留更糟（那正是 `D32` 当初选择整块丢弃的理由，现在只作用在残缺的块上）。
    `redacted_thinking` 例外——它的凭据在 `data` 里，本来就没有 `signature`。

    **不认识的 `kind` 跳过**：本插件只产出那两种，第三种只可能来自手改的历史或未来版本。
    """
    encoded: list[dict[str, JsonValue]] = []
    for block in blocks:
        if not block.owned_by(PROVIDER_NAME) or block.kind not in THINKING_KINDS:
            continue
        payload = dict(block.payload)
        if block.kind == "redacted_thinking":
            data = payload.get("data")
            if isinstance(data, str) and data:
                encoded.append({"type": "redacted_thinking", "data": data})
            continue
        signature = payload.get("signature")
        if not isinstance(signature, str) or not signature:
            continue
        text = payload.get("thinking")
        encoded.append(
            {
                "type": "thinking",
                # 空正文是合法的（`display: "omitted"`）；Anthropic 要的是签名。
                "thinking": text if isinstance(text, str) else "",
                "signature": signature,
            }
        )
    return encoded


def _merge_same_role(turns: Sequence[Mapping[str, JsonValue]]) -> list[dict[str, JsonValue]]:
    merged: list[dict[str, JsonValue]] = []
    for turn in turns:
        if merged and merged[-1]["role"] == turn["role"]:
            merged[-1]["content"] = [*_blocks_of(merged[-1]), *_blocks_of(turn)]
            continue
        merged.append({"role": turn["role"], "content": list(_blocks_of(turn))})
    return merged


def _fill_empty(turns: list[dict[str, JsonValue]]) -> list[dict[str, JsonValue]]:
    """丢掉空 text 块；整轮空了给一个地板值。Anthropic 拒绝空 `text` 与空 `content`。"""
    for turn in turns:
        blocks = [
            block
            for block in _blocks_of(turn)
            if not (
                isinstance(block, Mapping) and block.get("type") == "text" and not block.get("text")
            )
        ]
        turn["content"] = blocks or [_text_block(EMPTY_TEXT)]
    return turns


def normalize_turns(turns: Sequence[Mapping[str, JsonValue]]) -> list[dict[str, JsonValue]]:
    """三条规整，顺序固定：合并同角色 → 剥尾部 assistant → 补首部 user。

    顺序有讲究：先合并才能让「尾部有没有 assistant」这个判断只看一条轮；先剥后补是因为
    剥掉尾部可能把序列剥空，那时首部规则要作用在改投出来的那条轮上。
    """
    merged = _merge_same_role(turns)

    # 规则 2：剥尾部 assistant。Anthropic 不支持 assistant prefill，尾部留着就是 400。
    popped: dict[str, JsonValue] | None = None
    while merged and merged[-1].get("role") == "assistant":
        popped = merged.pop()
    # 剥空了就把最后那条改投成 user——否则换来的是一句「messages 为空」的 400，
    # 那比原来的问题更难诊断。带 `tool_use` 的不改投（那种块在 user 轮里非法）。
    if not merged and popped is not None and not _has_tool_use(popped):
        merged.append({"role": "user", "content": list(_blocks_of(popped))})

    # 规则 3：首轮必须是 user。带 `tool_use` 的首轮**不动**——前插一条会让紧随其后的
    # `tool_result` 找不到配对，把一个可恢复的 400 变成更难诊断的那种。
    if merged and merged[0].get("role") == "assistant" and not _has_tool_use(merged[0]):
        merged.insert(0, {"role": "user", "content": [_text_block(CONVERSATION_CONTINUED)]})

    return _fill_empty(merged)


def encode_tools(tools: Sequence[ToolSpec]) -> list[JsonValue]:
    """工具声明。`parameters` 已经是 JSON Schema，原样透传；只有**名字**要编码。"""
    return [
        {
            "name": encode_tool_name(spec.name),
            "description": spec.description,
            "input_schema": spec.parameters,
        }
        for spec in tools
    ]


# ------------------------------------------------------------------------------ 请求组装


@dataclass(frozen=True, slots=True)
class ThinkingSpec:
    """一份已校验的 thinking 设置。由 `settings.py` 构造，本模块只负责把它变成线格式。"""

    mode: str = THINKING_OFF
    budget_tokens: int = 0
    display: str = ""

    @property
    def enabled(self) -> bool:
        """是否会真的产出思考内容。`disabled` 是**显式关掉**，因此不算。"""
        return self.mode in {THINKING_ADAPTIVE, THINKING_BUDGET}

    def payload(self) -> dict[str, JsonValue] | None:
        """线格式形状。`off` 返回 `None`——那一档是「这个键根本不发」。"""
        if self.mode == THINKING_OFF:
            return None
        if self.mode == THINKING_DISABLED:
            return {"type": "disabled"}
        if self.mode == THINKING_BUDGET:
            return {"type": "enabled", "budget_tokens": self.budget_tokens}
        shape: dict[str, JsonValue] = {"type": "adaptive"}
        if self.display:
            shape["display"] = self.display
        return shape


@dataclass(frozen=True, slots=True)
class CachingSpec:
    """一份已校验的 prompt caching 设置。三个断点各自可关。"""

    enabled: bool = False
    ttl: str = ""
    system: bool = True
    tools: bool = True
    history: bool = True

    def marker(self) -> dict[str, JsonValue]:
        marker: dict[str, JsonValue] = {"type": "ephemeral"}
        if self.ttl:
            marker["ttl"] = self.ttl
        return marker


def _mark_last_block(blocks: list[JsonValue], marker: Mapping[str, JsonValue]) -> None:
    """给最后一个块挂 `cache_control`。空列表时什么都不做。"""
    if not blocks:
        return
    last = blocks[-1]
    if isinstance(last, Mapping):
        blocks[-1] = {**last, "cache_control": dict(marker)}


def _apply_caching(
    caching: CachingSpec,
    *,
    system: list[JsonValue],
    turns: list[dict[str, JsonValue]],
    tools: list[JsonValue],
) -> None:
    """就地布 `cache_control` 断点。按构造最多 3 个，不会撞上 4 个的上限。

    三个位置沿用 legacy `_apply_cache_control` 的选择，理由是把**稳定前缀**（工具定义、
    系统指令、上一轮之前的历史）与本轮变动分开：断点之前的内容命中缓存，之后的每轮重算。
    历史断点放在 `messages[-2]` 而不是 `[-1]`：最后一条正是本轮新增的那条，给它设断点
    等于每轮都在写一份用不上的缓存。
    """
    if not caching.enabled:
        return
    marker = caching.marker()
    if caching.tools and tools:
        _mark_last_block(tools, marker)
    if caching.system and system:
        _mark_last_block(system, marker)
    if caching.history and len(turns) >= 3:
        blocks = _blocks_of(turns[-2])
        _mark_last_block(blocks, marker)
        turns[-2]["content"] = blocks


def build_payload(
    request: ModelRequest,
    *,
    max_output_tokens: int,
    supports_temperature: bool = True,
    thinking: ThinkingSpec = ThinkingSpec(),
    caching: CachingSpec = CachingSpec(),
    effort: str = "",
    stream: bool = False,
) -> dict[str, JsonValue]:
    """组装 `POST /messages` 的请求体。

    `supports_temperature=False` 时 `temperature` 被**省略而不是钳到某个值**：Opus 4.7+ 与
    Sonnet 5 对这个字段直接 400，而替用户挑一个温度是在替它改采样行为。
    **legacy 会在 thinking 开启时强制 `temperature=1.0`，这里不做**——「这个模型拒绝采样
    参数」的答案是 `supports_temperature: false`，一条配置能说清的事不该藏在代码里。

    `params.seed` **被丢弃**：Anthropic 没有这个参数。`SamplingParams` 是通用的，为一个
    这家不支持的旋钮让整轮请求失败不合算。
    """
    system, turns = encode_messages(request.messages)
    tools = encode_tools(request.tools) if request.tools else []
    _apply_caching(caching, system=system, turns=turns, tools=tools)

    params = request.params
    body: dict[str, JsonValue] = {
        "model": request.model_id,
        "messages": list(turns),
        # `max_tokens` 是必填的，没有「用服务端默认」这一档。
        "max_tokens": params.max_output_tokens or max_output_tokens,
    }
    if system:
        body["system"] = system
    if params.temperature is not None and supports_temperature:
        body["temperature"] = params.temperature
    if params.top_p is not None:
        body["top_p"] = params.top_p
    if params.stop_sequences:
        body["stop_sequences"] = list(params.stop_sequences)
    if tools:
        body["tools"] = tools
        # `ModelRequest` 没有 `tool_choice` 槽，因此恒发 `auto`——`legacy` 的
        # `_convert_tool_choice` 在这里没有输入源。
        body["tool_choice"] = {"type": "auto"}
    shape = thinking.payload()
    if shape is not None:
        body["thinking"] = shape
    if effort:
        body["output_config"] = {"effort": effort}
    if stream:
        body["stream"] = True
    return body
