"""`web` 插件用例的替身与工厂。**模块名带插件前缀**是刻意的。

`testpaths` 一次收集整个 `plugins/`，而 pytest 按模块名去重：两个插件各有一个
`_fakes.py` 时，先导入的会顶掉后一个，另一棵测试树整体 `ImportError`。
**单独跑各自目录看不出来，跑全量才炸**（`D34` 就是这么发现的）。

职责：一个实现 `sdk.api.HttpAccess` 的出网替身、一个把它接上去的 `PluginContext`、
以及构造 `ToolInvocation` 的小工厂。
不负责：断言（在各 `test_web_*.py` 里）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from karyvia.contracts import (
    ErrorCode,
    JsonValue,
    KaryviaError,
    ToolCall,
    ToolInvocation,
)
from karyvia.sdk import HttpResponse
from karyvia.sdk.testing import FakePluginContext, make_correlation


class RecordedRequest:
    """`StubNet` 收到的一次请求。"""

    __slots__ = ("body", "headers", "max_bytes", "method", "timeout_ms", "url")

    def __init__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str] | None,
        body: bytes | None,
        timeout_ms: int,
        max_bytes: int | None,
    ) -> None:
        self.method = method
        self.url = url
        self.headers = dict(headers or {})
        self.body = body
        self.timeout_ms = timeout_ms
        #: `D42` 起 `web.fetch` 必须把字节上界交给门面，而不是自己读完再切。
        self.max_bytes = max_bytes


class StubNet:
    """`HttpAccess` 的替身：按脚本回响应，或抛一个预置的错误。

    **刻意不模拟 SSRF 守卫**：那份判定在 `runtime/access/net.py`，有它自己的用例；
    在这里复刻一遍只会变成第二份会漂移的实现。本插件对守卫的全部依赖就是「调它」，
    而那一条由 `test_web_plugin.py::test_fetch_goes_through_the_guarded_facade` 断言。
    """

    def __init__(
        self,
        responses: Sequence[HttpResponse] = (),
        *,
        error: KaryviaError | None = None,
    ) -> None:
        self._responses = list(responses)
        self._error = error
        self.requests: list[RecordedRequest] = []

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
        self.requests.append(
            RecordedRequest(method, url, headers, body, timeout_ms, max_bytes)
        )
        if self._error is not None:
            raise self._error
        if not self._responses:
            raise AssertionError("StubNet 的脚本已经用完，但又来了一次请求")
        response = self._responses.pop(0)
        if max_bytes is None or len(response.body) <= max_bytes:
            return response
        # **替身必须照做**：真门面在 `max_bytes` 处停止读取并标 `truncated`
        # （`runtime/access/net.py`）。不照做的话，「上界交给了门面」这件事在用例里
        # 就看不出效果——`web.fetch` 从 `D42` 起不再自己切第二刀。
        return replace(response, body=response.body[:max_bytes], truncated=True)


class WebContext(FakePluginContext):
    """把 `StubNet` 接上 `ctx.net` 的上下文。

    `FakePluginContext.net` 不提供真实网络实现，因此这里覆盖那个 property。
    """

    def __init__(
        self,
        net: StubNet | None = None,
        *,
        config: Mapping[str, JsonValue] | None = None,
        secrets: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__("web", config=config, secrets=secrets)
        self.stub_net = net if net is not None else StubNet()

    @property
    def net(self) -> StubNet:
        return self.stub_net


def html_response(
    body: bytes, *, status: int = 200, content_type: str = "text/html; charset=utf-8"
) -> HttpResponse:
    return HttpResponse(status=status, headers={"content-type": content_type}, body=body)


def invocation(name: str, arguments: Mapping[str, JsonValue]) -> ToolInvocation:
    return ToolInvocation(
        call=ToolCall(call_id="call-1", name=name, arguments=dict(arguments)),
        correlation=make_correlation(),
        timeout_ms=5_000,
    )


def missing_secret() -> KaryviaError:
    return KaryviaError(ErrorCode.CONFIG_SECRET_MISSING, "没配。")
