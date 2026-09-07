"""OpenAI 兼容的线格式：路由、请求校验、SSE 分片与响应体。

职责：`POST /v1/chat/completions`（流式与非流式）、`GET /v1/models`、`GET /health`；
把请求翻成一次提交，把出站消息翻成 OpenAI 的分片或响应体。
不负责：起服务（`channel.py`）、关联请求与 turn（`hub.py`）、执行 turn（Kernel）。

**不支持的东西一律显式拒绝而不是静默忽略**，这是本模块最重要的一条。`system` 消息、
客户端工具、多模态内容都会 400——静默丢掉它们会让客户端相信自己设置了一个根本没生效
的东西，而那类误解要等到输出不对劲时才暴露。相反，采样类参数（`temperature`、
`max_tokens` 等）**接受并忽略**：它们归实例的模型配置与 `TurnLimits` 管，为它们报错
会让绝大多数现成客户端直接不可用。

**只提交最后一条 user 消息**。历史归会话存储（`SES-*`），由 `conversation_id` 索引；
把客户端送来的整段历史再喂一遍等于把同一段对话讲两遍。
"""

from __future__ import annotations

import hmac
import json
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Final, cast

from karyvia.contracts import CancelReason, StreamState

from .hub import SessionHub, Waiter
from .usage import TurnUsage

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from aiohttp import web

__all__ = ["build_app"]

#: 会话标识的来源，按优先级。请求头优先于 `user`——前者是显式的，后者 OpenAI 定义为
#: 「终端用户标识」，只是恰好能当会话用。
CONVERSATION_HEADER: Final = "X-Karyvia-Conversation"

#: 明确拒绝的请求字段。值是给用户的那句话——为什么不支持，以及该往哪走。
_REFUSED: Final[dict[str, str]] = {
    "tools": "工具由实例配置决定，不接受客户端传入；用 karyvia capabilities 查看生效的工具。",
    "functions": "工具由实例配置决定，不接受客户端传入。",
    "tool_choice": "工具调度由 Kernel 决定，不接受客户端指定。",
    "response_format": "结构化输出尚未支持。",
}


#: `aiohttp` 3.9 起要求用 `AppKey` 而不是裸字符串键。它要 import aiohttp 才建得出来，
#: 而本模块只在 `channel.start()` 之后才被导入，因此在 `build_app()` 里惰性初始化。
_hub_key: web.AppKey[SessionHub]
_api_key_key: web.AppKey[str | None]


def _init_keys() -> None:
    global _hub_key, _api_key_key
    from aiohttp import web

    if "_hub_key" not in globals():
        _hub_key = web.AppKey("hub", SessionHub)
        _api_key_key = web.AppKey("api_key")


def build_app(hub: SessionHub, *, api_key: str | None = None) -> web.Application:
    """装配 aiohttp 应用。`aiohttp` 由调用方保证已可导入（`channel.start()`）。"""
    from aiohttp import web

    _init_keys()
    app = web.Application()
    app[_hub_key] = hub
    app[_api_key_key] = api_key
    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_get("/health", handle_health)
    return app


# ---------------------------------------------------------------------- 端点


async def handle_health(request: web.Request) -> web.StreamResponse:
    from aiohttp import web

    return web.json_response({"status": "ok"})


async def handle_models(request: web.Request) -> web.StreamResponse:
    """`GET /v1/models`。只有一个条目：这个实例配的那个模型。"""
    from aiohttp import web

    hub: SessionHub = request.app[_hub_key]
    denied = _unauthorized(request)
    if denied is not None:
        return denied
    model_id = hub.settings.model_id or "karyvia"
    return web.json_response(
        {
            "object": "list",
            "data": [
                {
                    "id": model_id,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "karyvia",
                }
            ],
        }
    )


async def handle_chat_completions(request: web.Request) -> web.StreamResponse:
    """`POST /v1/chat/completions`。"""
    hub: SessionHub = request.app[_hub_key]
    denied = _unauthorized(request)
    if denied is not None:
        return denied
    try:
        # `request.json()` 交回 `Any`。在这里就收成 `object`，否则它会顺着 `body` 一路
        # 漏进下面每一个解析函数（原则 6：边界上把动态数据类型化）。
        parsed: object = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _error(400, "请求体不是合法 JSON。")
    if not isinstance(parsed, Mapping):
        return _error(400, "请求体必须是一个 JSON 对象。")
    # boundary: 顶层已收窄到映射；每个字段的形状由 `_prompt_from` / `_refusals` 等
    # 逐个判定，这里不一次性断言。
    body = cast("Mapping[str, object]", parsed)

    problem = _refusals(body)
    if problem is not None:
        return _error(400, problem)
    prompt, user_id = _prompt_from(body)
    if prompt is None:
        return _error(400, "messages 里必须至少有一条 role=user 的文本消息。")
    conversation_id = _conversation_id(request, body, hub)
    if not conversation_id or len(conversation_id) > 256 or "\n" in conversation_id:
        return _error(400, "会话标识非法（不得为空、含换行或超过 256 字符）。")

    model = _string(body.get("model")) or hub.settings.model_id or "karyvia"
    stream = body.get("stream") is True
    include_usage = _include_usage(body)

    # **先登记、后提交**：反过来会丢掉第一片增量。
    waiter = hub.open(conversation_id)
    try:
        hub.submit(conversation_id, prompt, user_id=user_id or "api")
        if stream:
            return await _stream_response(request, hub, waiter, model, include_usage)
        return await _blocking_response(hub, waiter, model)
    finally:
        hub.discard(waiter)


# ---------------------------------------------------------------------- 响应


async def _blocking_response(
    hub: SessionHub, waiter: Waiter, model: str
) -> web.StreamResponse:
    """非流式：攒到终态再一次性回。"""
    from aiohttp import web

    terminal: StreamState | None = None
    content = ""
    async for message in waiter.stream(timeout_ms=hub.settings.request_timeout_ms):
        if message.stream_state is StreamState.DELTA:
            continue
        terminal = message.stream_state
        content = message.content
    usage = hub.usage.take(waiter.turn_id)
    if terminal is None:
        return _error(504, "等待模型响应超时。", err_type="timeout")
    if terminal is StreamState.FAILED:
        return _error(500, content or "本轮失败。", err_type="server_error")
    return web.json_response(_completion(content, model=model, state=terminal, usage=usage))


async def _stream_response(
    request: web.Request,
    hub: SessionHub,
    waiter: Waiter,
    model: str,
    include_usage: bool,
) -> web.StreamResponse:
    """流式：SSE。断连时请求取消那条 turn（`ctx.turns`）。"""
    from aiohttp import web

    response = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            # 反向代理默认会缓冲 SSE，那会让「流式」在部署后变成一次性返回。
            "X-Accel-Buffering": "no",
        }
    )
    await response.prepare(request)
    chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    opener = _chunk(chunk_id, created, model, delta={"role": "assistant"})
    disconnected = False
    try:
        await _write(response, opener)
        async for message in waiter.stream(timeout_ms=hub.settings.request_timeout_ms):
            if message.stream_state is StreamState.DELTA:
                if message.content:
                    await _write(
                        response, _chunk(chunk_id, created, model, delta={"content": message.content})
                    )
                continue
            await _write(response, _terminal_chunk(chunk_id, created, model, message))
            break
    except (ConnectionResetError, ConnectionError):
        disconnected = True
    usage = hub.usage.take(waiter.turn_id)
    if disconnected:
        _cancel(hub, waiter)
        return response
    if include_usage and usage is not None:
        await _write(response, _usage_chunk(chunk_id, created, model, usage))
    await _write(response, b"data: [DONE]\n\n")
    return response


def _cancel(hub: SessionHub, waiter: Waiter) -> None:
    """客户端断开：请求取消。

    **这是请求而不是保证**：取消经 `CancelToken` 在检查点生效，好让 turn 把已产生的
    内容落库再退出。因此「断连」不等于「立刻停止」。
    """
    if hub.turns is None or waiter.turn_id is None:
        return
    hub.turns.cancel_turn(waiter.turn_id, CancelReason.USER)


# ---------------------------------------------------------------------- 线格式


def _completion(
    content: str, *, model: str, state: StreamState, usage: TurnUsage | None
) -> dict[str, object]:
    body: dict[str, object] = {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": _finish_reason(state),
            }
        ],
    }
    if usage is not None:
        body["usage"] = _usage_body(usage)
    return body


def _usage_body(usage: TurnUsage) -> dict[str, int]:
    """OpenAI 的三个字段。**是整条 turn 之和**，含工具往返（见 `usage.py`）。"""
    return {
        "prompt_tokens": usage.input_tokens,
        "completion_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
    }


def _chunk(
    chunk_id: str,
    created: int,
    model: str,
    *,
    delta: Mapping[str, str],
    finish_reason: str | None = None,
    extra: Mapping[str, str] | None = None,
) -> bytes:
    choice: dict[str, object] = {
        "index": 0,
        "delta": dict(delta),
        "finish_reason": finish_reason,
    }
    if extra:
        choice.update(extra)
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [choice],
    }
    return _sse(payload)


def _terminal_chunk(
    chunk_id: str, created: int, model: str, message: object
) -> bytes:
    """终态分片。

    **被取消或失败的 turn 不能装成正常收尾**：`finish_reason` 照 OpenAI
    的取值给 `stop`（客户端只认那几个），但额外带一个 `x_karyvia_state`——
    分得出来的客户端能分出来，分不出来的至少不会崩。
    """
    state = getattr(message, "stream_state", StreamState.FINAL)
    content = getattr(message, "content", "")
    delta = {"content": content} if state is StreamState.FAILED and content else {}
    extra = None if state is StreamState.FINAL else {"x_karyvia_state": str(state.value)}
    return _chunk(
        chunk_id,
        created,
        model,
        delta=delta,
        finish_reason=_finish_reason(state),
        extra=extra,
    )


def _usage_chunk(chunk_id: str, created: int, model: str, usage: TurnUsage) -> bytes:
    """`stream_options.include_usage` 打开时的收尾分片：`choices` 为空数组。"""
    return _sse(
        {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [],
            "usage": _usage_body(usage),
        }
    )


def _finish_reason(state: StreamState) -> str:
    """`STOPPED_BY_LIMIT` 也映射到 `length` 之外的 `stop`：`StreamState` 层面看不到
    预算与自然结束的区别。"""
    return "stop"


def _sse(payload: Mapping[str, object]) -> bytes:  # boundary: 见 `_completion`
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


async def _write(response: web.StreamResponse, data: bytes) -> None:
    await response.write(data)


# ---------------------------------------------------------------------- 请求解析


def _prompt_from(body: Mapping[str, object]) -> tuple[str | None, str | None]:
    """取最后一条 user 文本。返回 `(prompt, user_id)`，取不到时 prompt 为 `None`。"""
    messages = body.get("messages")
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        return None, None
    for raw_entry in reversed(list(cast("Sequence[object]", messages))):
        if not isinstance(raw_entry, Mapping):
            continue
        entry = cast("Mapping[str, object]", raw_entry)
        if entry.get("role") != "user":
            continue
        content = entry.get("content")
        if isinstance(content, str) and content.strip():
            return content, _string(body.get("user"))
        # 多模态内容部件（数组形态）暂不支持，让它落到统一的 400。
        return None, None
    return None, None


def _refusals(body: Mapping[str, object]) -> str | None:
    """显式拒绝的字段。返回第一条要说的话。"""
    for field, message in _REFUSED.items():
        if body.get(field):
            return message
    messages = body.get("messages")
    if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes)):
        for raw_entry in cast("Sequence[object]", messages):
            if not isinstance(raw_entry, Mapping):
                continue
            entry = cast("Mapping[str, object]", raw_entry)
            if entry.get("role") == "system":
                return (
                    "system 消息不被接受：系统指令归实例配置"
                    "（plugins.context-basic.config.instructions），不由请求指定。"
                )
            if not isinstance(entry.get("content"), str):
                return "只支持纯文本 content；多模态内容部件尚未支持。"
    n = body.get("n")
    if isinstance(n, int) and not isinstance(n, bool) and n > 1:
        return "一次请求只产生一个回答，n 必须是 1。"
    return None


def _conversation_id(request: web.Request, body: Mapping[str, object], hub: SessionHub) -> str:
    header = request.headers.get(CONVERSATION_HEADER)
    if header:
        return header.strip()
    user = _string(body.get("user"))
    if user:
        return user.strip()
    return hub.settings.default_conversation


def _include_usage(body: Mapping[str, object]) -> bool:
    options = body.get("stream_options")
    if not isinstance(options, Mapping):
        return False
    return cast("Mapping[str, object]", options).get("include_usage") is True


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


# ---------------------------------------------------------------------- 鉴权与错误


def _unauthorized(request: web.Request) -> web.StreamResponse | None:
    """检查 Bearer 凭据。没配 `api_key` 就不鉴权（只允许回环，`settings.py` 保证）。"""
    expected: str | None = request.app[_api_key_key]
    if expected is None:
        return None
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    presented = header[len(prefix) :] if header.startswith(prefix) else ""
    # 定时安全比较：凭据比对不该泄漏前缀长度。
    if hmac.compare_digest(presented, expected):
        return None
    return _error(401, "凭据无效。", err_type="invalid_request_error")


def _error(status: int, message: str, *, err_type: str = "invalid_request_error"):  # noqa: ANN201
    """OpenAI 的错误信封。**不带 detail**——错误文本会回到客户端。"""
    from aiohttp import web

    return web.json_response(
        {"error": {"message": message, "type": err_type, "param": None, "code": None}},
        status=status,
    )
