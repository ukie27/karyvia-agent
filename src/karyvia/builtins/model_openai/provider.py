"""内建 Model Provider：OpenAI 兼容 Chat Completions。

职责：实现 `ModelProvider`（`describe` / `complete` / `stream`），管理 httpx 客户端与
取消检查点；同时提供内建注册入口 `setup(api)`。
不负责：线格式翻译（`wire.py`）、错误分类（`faults.py`）、配置校验（`settings.py`）、
续写与重试策略。

四条决定了本模块形状的规则：

- **凭据只从 `ctx.secret("api_key")` 来。** `SecretStr` 的明文只在拼 Authorization 头的
  那一行经 `reveal()` 取出，不进配置、不进
  `detail`、不进事件。`kernel/config/secrets.py::resolve_text`
  够不着（`R4` 禁止 `builtins/` import `kernel/`），接线由 Runtime 在 ctx 那侧完成。
- **直接用 httpx 而不是 `ctx.net`。** `HttpAccess` 的 SSRF 守卫会拒绝私有网段，而本内建
  明确支持本地 vLLM / Ollama / LM Studio。资源门面是方便插件复用的受约束实现，不是
  强制所有可信插件经过的授权代理。
- **本地端点关 keepalive、关代理。** Ollama / llama.cpp / vLLM 会在客户端 keepalive 到期
  前关掉空闲连接，不关就是每轮第一次调用必失败；而 `HTTP_PROXY` / `ALL_PROXY` 会把
  localhost 流量送进一个够不着它的代理。这两条都是真实端点上验证过的，不是防御性代码。
- **流式中途失败必须先 `yield DONE(ERROR)` 再抛**（`protocols.py` 写死）。
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

from .faults import error_for_status, error_for_transport
from .settings import CAPABILITY_NAME, SECRET_NAME, OpenAISettings, resolve_settings
from .wire import (
    SSE_DONE,
    StreamDecoder,
    build_payload,
    decode_response,
    parse_sse_data,
)

__all__ = [
    "CHAT_COMPLETIONS_PATH",
    "OpenAIModelProvider",
    "is_local_endpoint",
    "read_credential",
    "setup",
]

#: 请求路径。拼在 `base_url` 之后，`base_url` 自身原样使用。
CHAT_COMPLETIONS_PATH: Final = "/chat/completions"

_STREAMING_UNSUPPORTED: Final = (
    "配置未声明该模型支持流式，本 Provider 不会自行降级为一次性返回。"
)
_BAD_JSON_BODY: Final = "供应商返回的响应体不是合法 JSON。"
_STREAM_STALLED: Final = "模型流式响应中断：超过空闲上限没有收到新分片。"


def is_local_endpoint(base_url: str) -> bool:
    """判断端点是否指向本机或私有网段。

    用 `ipaddress` 而不是字符串前缀匹配：`127.0.0.1` 之外还有 `10.x`、`192.168.x`、
    `172.16-31.x` 与 IPv6 回环，而它们全都是「本地模型服务」的常见落点。
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


class OpenAIModelProvider:
    """`ModelProvider` 的内建实现。

    `transport` 可注入是为了让整套用例走 `httpx.MockTransport`——`ModelProviderContract`
    会**不带参数**地反复构造 provider 并真的发起请求，没有这个注入口，「测试不依赖真实
    网络」就只能是一句承诺。
    """

    __slots__ = ("_client", "_credential", "_settings", "_transport")

    def __init__(
        self,
        settings: OpenAISettings,
        *,
        credential: SecretStr | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._credential = credential
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    @property
    def settings(self) -> OpenAISettings:
        return self._settings

    @property
    def uses_local_endpoint(self) -> bool:
        """端点是否落在本机或私有网段。决定要不要关 keepalive 与代理。"""
        return is_local_endpoint(self._settings.base_url)

    # -------------------------------------------------------------------- 客户端与请求

    def _headers(self) -> dict[str, str]:
        """认证头。**明文只在这里出现一次**，随请求走，不落任何其他结构。"""
        headers = {"content-type": "application/json"}
        if self._credential is None:
            return headers
        secret = self._credential.reveal()
        if self._settings.auth == "bearer":
            headers["authorization"] = f"Bearer {secret}"
        elif self._settings.auth == "api_key_header":
            headers["api-key"] = secret
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

        `ModelProvider` 协议里没有生命周期钩子，因此这不是契约的一部分——由的
        装配根在实例停止时调用。多次调用安全。
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
            max_tokens_field=entry.max_tokens_field,
            supports_temperature=entry.supports_temperature,
            default_max_output_tokens=entry.max_output_tokens,
            stream=stream,
            include_usage=self._settings.include_usage and stream,
        )

    # -------------------------------------------------------------------- 契约方法

    def describe(self, model_id: str) -> ModelInfo:
        """声明该模型的能力与窗口。

        **异常约定**：`models` 白名单非空且不含该模型时抛 `CAPABILITY_MISSING`。
        **取消语义**：同步方法，不涉及取消；且**不发网络请求**——它在预算推导路径上。
        """
        return self._settings.describe(model_id)

    async def complete(self, request: ModelRequest, cancel: CancelSignal) -> ModelResponse:
        """一次非流式请求。

        **异常约定**：限流、超时、认证失败按 `faults.py` 的表折成可分类的 `KaryviaError`；
        httpx 的原生异常不逸出。**内容过滤不是异常**——它是 HTTP 200 上的
        `StopReason.CONTENT_FILTER`。
        **取消语义**：进入网络调用前检查一次；调用期间被取消时不返回半份响应。
        """
        cancel.raise_if_requested()
        payload = self._payload(request, stream=False)
        client = self._ensure_client()
        try:
            response = await client.post(
                CHAT_COMPLETIONS_PATH,
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
        return decode_response(body, model_id=request.model_id)

    async def stream(
        self, request: ModelRequest, cancel: CancelSignal
    ) -> AsyncIterator[ModelChunk]:
        """一次流式请求，逐片产出增量。

        **异常约定**：同 `complete()`；**已经 yield 过分片后再失败，必须先 yield 一个
        `DONE(ERROR)` 再抛**，让消费方把已收到的文本按 `interrupted=True` 落库
        。配置未声明 `streaming` 时抛 `CAPABILITY_MISSING`，不自行降级。
        **取消语义**：每产出一片前检查 `cancel`；已 yield 的分片由调用方保留。
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
        """SSE 读取循环。把 httpx 异常与空闲超时都折成 `KaryviaError`。"""
        payload = self._payload(request, stream=True)
        client = self._ensure_client()
        idle = self._settings.stream_idle_timeout_ms / 1000
        try:
            async with client.stream(
                "POST",
                CHAT_COMPLETIONS_PATH,
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
                lines = response.aiter_lines()
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
                    if data == SSE_DONE:
                        return
                    event = _safe_json(data)
                    if not isinstance(event, Mapping):
                        raise KaryviaError(ErrorCode.EXTERNAL_MODEL_PROVIDER, _BAD_JSON_BODY)
                    for chunk in decoder.push(event):
                        yield chunk
        except httpx.HTTPError as exc:
            raise error_for_transport(exc) from exc


def _safe_json(text: str) -> JsonValue | None:
    """尽力解析 JSON。失败返回 `None`，由调用方决定这算不算错——错误响应体经常不是 JSON。"""
    try:
        return json.loads(text)
    except ValueError:
        return None


# ------------------------------------------------------------------------------ 注册入口


def read_credential(ctx: PluginContext, settings: OpenAISettings) -> SecretStr | None:
    """取凭据。`auth="none"` 时**不碰** `ctx.secret()`——本地模型服务没有密钥，
    去要一个必然缺失的凭据只会让实例起不来。

    **异常约定**：凭据没配时保留 `ctx.secret()` 的 `CONFIG_SECRET_MISSING`，这里不吞异常。
    """
    if not settings.requires_credential:
        return None
    return ctx.secret(SECRET_NAME)


def setup(api: KaryviaAPI) -> None:
    """内建注册入口，`BUILTIN_MANIFESTS` 里那条 manifest 的 `setup` 指向它。

    配置与凭据都在这里解析一次。加载失败后 Runtime 会因没有可用模型而拒绝启动，
    不把错误拖到用户发出第一条消息时。
    """
    settings = resolve_settings(api.ctx)
    provider = OpenAIModelProvider(settings, credential=read_credential(api.ctx, settings))
    api.ctx.add_cleanup(provider.aclose)
    api.register_model_provider(CAPABILITY_NAME, provider)
