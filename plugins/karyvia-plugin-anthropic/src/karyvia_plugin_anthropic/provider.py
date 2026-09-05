"""Anthropic 原生 Messages API 的 `ModelProvider` 实现（开发方案 `D32`）。

职责：实现 `describe` / `complete` / `stream`，管理 httpx 客户端与取消检查点；
同时提供插件注册入口 `setup(api)`。
不负责：线格式翻译（`wire.py` / `decode.py`）、错误分类（`faults.py`）、
配置校验（`settings.py`）、续写与重试策略（编排层，技术方案 §6.2.2）。

四条决定了本模块形状的规则：

- **直接用 httpx，不用 `anthropic` 官方 SDK。** 与内建 `model_openai` 同构：`transport`
  可注入意味着整套用例能走 `httpx.MockTransport`——`ModelProviderContract` 会**不带参数**
  地反复构造 provider 并真的发起请求，没有这个注入口，「测试不依赖真实网络」就只能是
  一句承诺。顺带地，宿主发行版因此不必再依赖 `anthropic`。
- **凭据只从 `ctx.secret("api_key")` 来。** 明文只在拼认证头的那一行经 `reveal()` 取出，
  不进配置、不进 `detail`、不进事件（`MOD-002`、`CFG-003`）。
- **直接用 httpx 而不是 `ctx.net`。** `HttpAccess` 的 SSRF 守卫会拒绝私有网段，而中转与
  本地 relay 正是本插件的交付要点。资源门面是插件可以复用的受约束实现，不是可信插件
  必须经过的授权代理。
- **流式中途失败必须先 `yield DONE(ERROR)` 再抛**（`protocols.py` 写死、`EDG-304`）。
  `kernel/turn/folding.py` 据此把已收到的文本按 `interrupted=True` 落库，而不是把半截
  输出当成完整答案。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
from collections.abc import AsyncIterator, Mapping
from typing import Final
from urllib.parse import urlsplit

import httpx

from karyvia.contracts import (
    CancelSignal,
    ChunkKind,
    ErrorCode,
    JsonValue,
    KaryviaError,
    ModelCapability,
    ModelChunk,
    ModelInfo,
    ModelRequest,
    ModelResponse,
    SecretStr,
    StopReason,
)
from karyvia.sdk import KaryviaAPI, PluginContext

from .decode import StreamDecoder, decode_response, parse_sse_data
from .faults import error_for_event, error_for_status, error_for_transport
from .settings import (
    CAPABILITY_NAME,
    SECRET_NAME,
    AnthropicSettings,
    resolve_settings,
)
from .wire import MESSAGES_PATH, build_payload

__all__ = [
    "AnthropicModelProvider",
    "is_local_endpoint",
    "read_credential",
    "setup",
]

_STREAMING_UNSUPPORTED: Final = (
    "配置未声明该模型支持流式，本 Provider 不会自行降级为一次性返回（MOD-005）。"
)
_BAD_JSON_BODY: Final = "供应商返回的响应体不是合法 JSON。"
_STREAM_STALLED: Final = "模型流式响应中断：超过空闲上限没有收到新分片。"

#: SSE 事件里表示「服务端报错」的载荷类型。流一旦建立，状态码就已经是 200 了。
_ERROR_EVENT: Final = "error"


def is_local_endpoint(base_url: str) -> bool:
    """判断端点是否指向本机或私有网段。

    与内建 `model_openai.is_local_endpoint` 逐条相同（`R4` 禁止插件 import `builtins/`，
    因此是第二份实现，由一条对照用例钉住）。用 `ipaddress` 而不是字符串前缀匹配：
    `127.0.0.1` 之外还有 `10.x`、`192.168.x`、`172.16-31.x` 与 IPv6 回环。
    """
    host = urlsplit(base_url).hostname
    if host is None:
        return False
    if host in {"localhost", "host.docker.internal"}:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


class AnthropicModelProvider:
    """`ModelProvider` 的 Anthropic 原生实现。"""

    __slots__ = ("_client", "_credential", "_settings", "_transport")

    def __init__(
        self,
        settings: AnthropicSettings,
        *,
        credential: SecretStr | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._credential = credential
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    @property
    def settings(self) -> AnthropicSettings:
        return self._settings

    @property
    def uses_local_endpoint(self) -> bool:
        """端点是否落在本机或私有网段。决定要不要关 keepalive 与代理。"""
        return is_local_endpoint(self._settings.base_url)

    # -------------------------------------------------------------------- 客户端与请求

    def _headers(self) -> dict[str, str]:
        """请求头。**明文只在这里出现一次**，随请求走，不落任何其他结构。

        `anthropic-version` 是必填头；`anthropic-beta` 只在运维配了 `beta_headers` 时才发，
        逗号连接是 Anthropic 规定的多值形式。
        """
        headers = {
            "content-type": "application/json",
            "anthropic-version": self._settings.anthropic_version,
        }
        if self._settings.beta_headers:
            headers["anthropic-beta"] = ",".join(self._settings.beta_headers)
        if self._credential is None:
            return headers
        secret = self._credential.reveal()
        if self._settings.auth == "bearer":
            headers["authorization"] = f"Bearer {secret}"
        elif self._settings.auth == "x_api_key":
            headers["x-api-key"] = secret
        return headers

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        local = self.uses_local_endpoint
        limits = httpx.Limits(keepalive_expiry=0) if local else httpx.Limits()
        transport = self._transport
        if transport is None and local:
            # 本地端点绕开环境里的代理：`ALL_PROXY` 会把 localhost 也送进代理。
            transport = httpx.AsyncHTTPTransport(proxy=None, limits=limits)
        self._client = httpx.AsyncClient(
            base_url=self._settings.base_url,
            limits=limits,
            transport=transport,
            trust_env=not local,
        )
        return self._client

    async def aclose(self) -> None:
        """释放底层连接池。

        `ModelProvider` 协议里没有生命周期钩子，因此这不是契约的一部分——由插件的
        `stop` 路径或实例停止时调用。多次调用安全。
        """
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()

    def _timeout(self, request: ModelRequest) -> httpx.Timeout:
        budget = request.timeout_ms or self._settings.request_timeout_ms
        return httpx.Timeout(budget / 1000)

    def _payload(self, request: ModelRequest, *, stream: bool) -> dict[str, JsonValue]:
        entry = self._settings.entry_for(request.model_id)
        return build_payload(
            request,
            max_output_tokens=entry.max_output_tokens,
            supports_temperature=entry.supports_temperature,
            thinking=entry.thinking,
            caching=entry.caching,
            effort=entry.effort,
            stream=stream,
        )

    # -------------------------------------------------------------------- 契约方法

    def describe(self, model_id: str) -> ModelInfo:
        """声明该模型的能力与窗口（`MOD-001`）。

        **异常约定**：`models` 白名单非空且不含该模型时抛 `CAPABILITY_MISSING`。
        **取消语义**：同步方法，不涉及取消；且**不发网络请求**——它在预算推导路径上。
        """
        return self._settings.describe(model_id)

    async def complete(self, request: ModelRequest, cancel: CancelSignal) -> ModelResponse:
        """一次非流式请求。

        **异常约定**：限流、超时、认证失败按 `faults.py` 的表折成可分类的 `KaryviaError`；
        httpx 的原生异常不逸出。**refusal 不是异常**——它是 HTTP 200 上的
        `StopReason.CONTENT_FILTER`。
        **取消语义**：进入网络调用前检查一次；调用期间被取消时不返回半份响应。
        """
        cancel.raise_if_requested()
        payload = self._payload(request, stream=False)
        client = self._ensure_client()
        try:
            response = await client.post(
                MESSAGES_PATH,
                json=payload,
                headers=self._headers(),
                timeout=self._timeout(request),
            )
        except httpx.HTTPError as exc:
            raise error_for_transport(exc) from exc
        if response.status_code >= 400:
            raise error_for_status(
                response.status_code,
                body=_safe_json(response.text),
                headers=response.headers,
            )
        body = _safe_json(response.text)
        if not isinstance(body, Mapping):
            raise KaryviaError(ErrorCode.EXTERNAL_MODEL_PROVIDER, _BAD_JSON_BODY)
        return decode_response(
            body,
            model_id=request.model_id,
            request_id=response.headers.get("request-id", ""),
        )

    async def stream(
        self, request: ModelRequest, cancel: CancelSignal
    ) -> AsyncIterator[ModelChunk]:
        """一次流式请求，逐片产出增量。

        **异常约定**：同 `complete()`；**已经 yield 过分片后再失败，必须先 yield 一个
        `DONE(ERROR)` 再抛**，让消费方把已收到的文本按 `interrupted=True` 落库
        （`EDG-304`）。配置未声明 `streaming` 时抛 `CAPABILITY_MISSING`，不自行降级。
        **取消语义**：每读一片前检查 `cancel`；已 yield 的分片由调用方保留。
        """
        info = self._settings.describe(request.model_id)
        if not info.supports(ModelCapability.STREAMING):
            raise KaryviaError(
                ErrorCode.CAPABILITY_MISSING,
                _STREAMING_UNSUPPORTED,
                detail={"model_id": request.model_id},
            )
        cancel.raise_if_requested()
        emitted = False
        decoder = StreamDecoder()
        try:
            async for chunk in self._iter_stream(request, cancel, decoder):
                emitted = True
                yield chunk
        except KaryviaError:
            # 已经吐过内容就先给一个 DONE(ERROR)：没有它，消费方分不清「流干净结束了」
            # 和「流断在半截」，一份残缺的回答会被当成完整答案。
            if emitted:
                yield ModelChunk(kind=ChunkKind.DONE, stop_reason=StopReason.ERROR)
            raise
        for chunk in decoder.finish():
            yield chunk

    async def _iter_stream(
        self, request: ModelRequest, cancel: CancelSignal, decoder: StreamDecoder
    ) -> AsyncIterator[ModelChunk]:
        """SSE 读取循环。把 httpx 异常与空闲超时都折成 `KaryviaError`。

        **只解析 `data:` 行**：SSE 帧同时有 `event:` 与 `data:`，而载荷自带 `type`。
        认两个真相来源会在中转改写 `event:` 时静默分叉。Anthropic 也没有 `[DONE]` 哨兵，
        迭代结束即流结束。
        """
        payload = self._payload(request, stream=True)
        client = self._ensure_client()
        idle = self._settings.stream_idle_timeout_ms / 1000
        try:
            async with client.stream(
                "POST",
                MESSAGES_PATH,
                json=payload,
                headers=self._headers(),
                timeout=self._timeout(request),
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise error_for_status(
                        response.status_code,
                        body=_safe_json(response.text),
                        headers=response.headers,
                    )
                async for chunk in self._pump(response.aiter_lines(), cancel, decoder, idle=idle):
                    yield chunk
        except httpx.HTTPError as exc:
            raise error_for_transport(exc) from exc

    async def _pump(
        self,
        lines: AsyncIterator[str],
        cancel: CancelSignal,
        decoder: StreamDecoder,
        *,
        idle: float,
    ) -> AsyncIterator[ModelChunk]:
        while True:
            cancel.raise_if_requested()
            try:
                # 请求级超时保护不了「开了口就不再吐字」的流，因此每片单独计时。
                line = await asyncio.wait_for(anext(lines), timeout=idle)
            except StopAsyncIteration:
                return
            except TimeoutError as exc:
                raise KaryviaError(
                    ErrorCode.TIMEOUT_MODEL_REQUEST,
                    _STREAM_STALLED,
                    detail={"stream_idle_timeout_ms": self._settings.stream_idle_timeout_ms},
                    retryable=True,
                ) from exc
            data = parse_sse_data(line)
            if not data:
                continue
            event = _safe_json(data)
            if not isinstance(event, Mapping):
                raise KaryviaError(ErrorCode.EXTERNAL_MODEL_PROVIDER, _BAD_JSON_BODY)
            if event.get("type") == _ERROR_EVENT:
                raise error_for_event(event)
            for chunk in decoder.push(event):
                yield chunk


def _safe_json(text: str) -> JsonValue | None:
    """尽力解析 JSON。失败返回 `None`，由调用方决定这算不算错——错误响应体经常不是 JSON。"""
    try:
        return json.loads(text)
    except ValueError:
        return None


# ------------------------------------------------------------------------------ 注册入口


def read_credential(ctx: PluginContext, settings: AnthropicSettings) -> SecretStr | None:
    """取凭据。`auth="none"` 时**不碰** `ctx.secret()`——本地 relay 没有密钥，
    去要一个必然缺失的凭据只会让插件加载失败。

    **异常约定**：凭据未配置时原样抛 `CONFIG_SECRET_MISSING`。
    """
    if not settings.requires_credential:
        return None
    return ctx.secret(SECRET_NAME)


def setup(api: KaryviaAPI) -> None:
    """插件注册入口，manifest 的 `setup` 指向它。

    配置与凭据都在这里解析一次，不拖到第一次 turn：一份写错的配置应当在
    `karyvia plugins list` 里就看得见（`D18` 的先例）。
    """
    settings = resolve_settings(api.ctx)
    provider = AnthropicModelProvider(settings, credential=read_credential(api.ctx, settings))
    api.ctx.add_cleanup(provider.aclose)
    api.register_model_provider(CAPABILITY_NAME, provider)
