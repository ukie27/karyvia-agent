"""`ctx.net` 的生产实现：带 SSRF 守卫的出网门面（`sdk.HttpAccess`）。

职责：`GuardedHttpAccess`——校验 URL 形状、把主机名解析成 IP 并逐个判定、**每一次重定向
都重新走一遍同一套判定**、最后经 httpx 发出请求。
不负责：给内建提供 HTTP（`model_openai` 直接用 httpx，因为它要连本地 vLLM / Ollama，
而那正是本守卫要拒的网段）。

**判定的顺序是「解析之后」而不是「解析之前」**：只看主机名的黑名单挡不住
`http://127.0.0.1.nip.io/`，也挡不住一个解析到 `169.254.169.254` 的普通域名。因此这里先
`getaddrinfo()`，再对**每一个**返回的地址判定——只要有一个落在私有/回环/链路本地网段就
整体拒绝，而不是「挑一个能用的」。

**重定向手动跟随**（`follow_redirects=False` + 自己循环）：交给 httpx 跟随等于让第 2 跳
绕过守卫，而  点名的就是这条路。

**挡不住 DNS 重绑定，如实写在这里**：校验时解析到的地址与 httpx 真正连接时解析到的地址
之间存在 TOCTOU 窗口。挡住它要求在校验后把连接钉死在已判定的 IP 上（自定义
transport + Host 头 + 证书校验的名字要另说），那是一整套超出应用级门面的工程。这与
`paths.py` 的 TOCTOU 那段同一种诚实：更严格的隔离由可选独立宿主、网络命名空间或部署环境
承担。

**httpx 惰性 import**：既定冷启动预算是 300 ms，而 `import httpx` 单独一项就约
280 ms。在函数体里 import，没有插件用 `ctx.net` 时一分钱都不付。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

from karyvia.contracts import ErrorCode, KaryviaError
from karyvia.sdk import HttpResponse

if TYPE_CHECKING:  # pragma: no cover - 仅为类型标注，运行时不 import httpx
    import httpx

__all__ = ["MAX_REDIRECTS", "GuardedHttpAccess", "address_is_blocked"]

#: 重定向跳数上限。超过即拒绝——一条无限重定向链在守卫下的表现应当是「被拒」，
#: 而不是「一直在校验」。
MAX_REDIRECTS: Final = 5

_ALLOWED_SCHEMES: Final = frozenset({"http", "https"})
_REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})

#: 解析主机名的函数形状。参数化只为让测试喂一份确定的解析结果——真去解析一个域名会让
#: 用例依赖 DNS，而「零真实网络」是 `tests/` 的一条 autouse 闸门。
Resolver = Callable[[str, int], Sequence[str]]


def address_is_blocked(address: str) -> str:
    """判定一个 IP 字面量是否被守卫拒绝。返回拒绝原因，放行则返回空串。

    拒的是：回环、私有网段、链路本地（`169.254.0.0/16` 覆盖云元数据地址
    `169.254.169.254`）、唯一本地、组播、保留段与未指定地址。**这不是一份可配置的名单**
    ——它是「插件的出网不该打到基础设施上」这条判断本身。
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return "地址解析不出来"
    if parsed.is_loopback:
        return "回环地址"
    if parsed.is_link_local:
        return "链路本地地址（含云元数据地址）"
    if parsed.is_private:
        return "私有网段"
    if parsed.is_multicast:
        return "组播地址"
    if parsed.is_reserved or parsed.is_unspecified:
        return "保留地址"
    return ""


class GuardedHttpAccess:
    """`HttpAccess` 的生产实现。结构化满足契约，不继承任何宿主基类。"""

    __slots__ = ("_plugin_id", "_resolver", "_transport")

    def __init__(
        self,
        *,
        plugin_id: str,
        resolver: Resolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._plugin_id = plugin_id
        self._resolver = resolver if resolver is not None else _resolve
        #: 测试用的替身传输。生产路径上是 `None`。
        self._transport = transport

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout_ms: int = 30_000,
        max_bytes: int | None = None,
    ) -> HttpResponse:
        import httpx  # noqa: PLC0415 - 惰性，见模块 docstring

        if max_bytes is not None and max_bytes <= 0:
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                "max_bytes 必须为正。",
                detail={"plugin": self._plugin_id, "max_bytes": max_bytes},
            )
        current = url
        async with httpx.AsyncClient(
            follow_redirects=False,
            transport=self._transport,
            timeout=timeout_ms / 1000,
        ) as client:
            for _ in range(MAX_REDIRECTS + 1):
                await self._check(current)
                response, payload, cut = await self._send(
                    client, method, current, headers, body, max_bytes
                )
                location = response.headers.get("location")
                if response.status_code not in _REDIRECT_STATUSES or not location:
                    return HttpResponse(
                        status=response.status_code,
                        headers=dict(response.headers),
                        body=payload,
                        truncated=cut,
                    )
                current = str(response.url.join(location))
                # 303 与「POST 收到 301/302」按 RFC 9110 转成 GET；照做而不是原样重发，
                # 否则一次带 body 的写请求会被悄悄发两次。
                if response.status_code == 303 or (
                    response.status_code in {301, 302} and method.upper() == "POST"
                ):
                    method, body = "GET", None
        raise self._denied(url, f"重定向超过 {MAX_REDIRECTS} 跳")

    async def _send(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        headers: Mapping[str, str] | None,
        body: bytes | None,
        max_bytes: int | None = None,
    ) -> tuple[httpx.Response, bytes, bool]:
        """发一次并把响应体读出来。返回 `(响应, 响应体, 是否被截断)`。

        **`max_bytes` 必须让读取本身停下来**，不能读完再切——后者对着一个几百 MB 的 URL
        照样会把它整个装进内存，而那正是这个参数要防的事。因此有上界时走
        `client.stream()`，攒够就 `break`（`async with` 退出时断开连接）。

        没有上界时保持 `client.request()` 的老路：多一条分支比让所有既有调用都改走流式
        安全——后者会改变超时的计时口径（读满整个 body 与拿到响应头是两件事）。
        """
        import httpx  # noqa: PLC0415 - 同上

        try:
            if max_bytes is None:
                response = await client.request(
                    method, url, headers=dict(headers or {}), content=body
                )
                return response, response.content, False
            chunks: list[bytes] = []
            size = 0
            async with client.stream(
                method, url, headers=dict(headers or {}), content=body
            ) as response:
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= max_bytes:
                        break
            payload = b"".join(chunks)
            # 最后一片可能跨过上界，切到正好。「== max_bytes 且流已尽」不算截断——
            # 那份内容是完整的，谎报 `truncated` 会让调用方给模型加一句不实的省略提示。
            return response, payload[:max_bytes], len(payload) > max_bytes
        except httpx.TimeoutException as exc:
            raise KaryviaError(
                ErrorCode.TIMEOUT_HTTP_REQUEST,
                "请求超时。",
                detail={"plugin": self._plugin_id, "url": _safe_url(url)},
            ) from exc
        except httpx.HTTPError as exc:
            raise KaryviaError(
                ErrorCode.EXTERNAL_HTTP_REQUEST,
                "请求失败。",
                detail={
                    "plugin": self._plugin_id,
                    "url": _safe_url(url),
                    "cause": type(exc).__name__,
                },
                retryable=True,
            ) from exc

    # ------------------------------------------------------------------ 守卫

    async def _check(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme.lower() not in _ALLOWED_SCHEMES:
            raise self._denied(url, "只允许 http 与 https")
        if parts.username or parts.password:
            # URL 里的凭据会被原样写进日志、Referer 与重定向目标——这条拒绝顺带堵掉一条
            # 泄漏路径，而不只是形状校验。
            raise self._denied(url, "URL 里不得带凭据")
        host = parts.hostname
        if not host:
            raise self._denied(url, "URL 里没有主机名")

        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
        for address in await self._addresses(host, port):
            reason = address_is_blocked(address)
            if reason:
                # `detail` 里**不放解析出来的 IP**：那是内网拓扑，写进一个插件读得到的
                # 错误就是另一种泄漏。原因文本足够让人判断该改什么。
                raise self._denied(url, f"目标落在被禁止的网段：{reason}")

    async def _addresses(self, host: str, port: int) -> tuple[str, ...]:
        """主机名 → 全部地址。**IP 字面量在这里短路**，根本不问解析器。

        短路放在这一层而不是 `_resolve()` 里，是因为解析器可注入：一条
        `http://127.0.0.1/` 的判定不该取决于调用方给了什么解析器。
        """
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return tuple(await asyncio.to_thread(self._resolver, host, port))
        return (host,)

    def _denied(self, url: str, reason: str) -> KaryviaError:
        return KaryviaError(
            ErrorCode.PERMISSION_DENIED,
            "出网请求被守卫拒绝。",
            detail={"plugin": self._plugin_id, "url": _safe_url(url), "reason": reason},
        )


def _resolve(host: str, port: int) -> tuple[str, ...]:
    """把主机名解析成全部地址。IP 字面量不会走到这里（`_addresses` 已经短路了）。"""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError:
        # 解析不出来交给「地址解析不出来」那条拒绝，而不是放行——一个解析失败的主机名
        # 在真正连接时可能解析成功（DNS 缓存、搜索域），放行等于把判定推给运气。
        return ("",)
    return tuple(str(info[4][0]) for info in infos)


def _safe_url(url: str) -> str:
    """渲染进错误的 URL：去掉 userinfo 与 query。

    query 常常带着签名、令牌与一次性凭据，而错误 `detail` 会进事件流与日志。
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}{parts.path}"
