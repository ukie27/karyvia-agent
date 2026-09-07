"""`tests/e2e/` 的公共闸门与录制装置。

职责：一条 autouse 的「不碰真实网络」夹具，一条把 `httpx` 的出站请求换成录制响应的夹具。
不负责：任何断言。

**网络闸门与录制是两件事，两条都要**：录制夹具保证用例拿得到确定的响应，闸门保证
「没有第二条路溜出去」——`model_openai` 在 `_ensure_client()` 里有本地端点与非本地端点
两条分支，只换掉其中一条而用例照样通过，是完全可能的。判据与
`tests/integration/conftest.py`、`tests/runtime/conftest.py` 完全相同：拦目标、放行回环。
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

#: 允许的目标。事件循环的 self-pipe 只连这几个。
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", "", None})

_MESSAGE = "开箱可用的端到端用例试图访问真实网络"


def _host_of(address: object) -> object:
    return address[0] if isinstance(address, tuple) and address else address


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """出站连接与 DNS 解析只允许打到回环地址。"""
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def guard(target: object) -> None:
        host = _host_of(target)
        if host not in _LOOPBACK:
            raise AssertionError(f"{_MESSAGE}：{host!r}")

    def connect(self: socket.socket, address: Any) -> None:  # boundary: stdlib 的地址联合类型
        guard(address)
        return real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:  # boundary: 同上
        guard(address)
        return real_connect_ex(self, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:  # boundary: 同上
        guard(host)
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield


class Recorder:
    """按顺序回放录制响应的处理器。

    **超出脚本即失败而不是回一个默认响应**：多出来的那次请求正是最值得看见的东西
    （比如工具结果没回给模型，于是它又问了一遍）。
    """

    def __init__(self) -> None:
        self.responses: list[Callable[[httpx.Request], httpx.Response]] = []
        self.requests: list[httpx.Request] = []

    def script(self, *responses: Callable[[httpx.Request], httpx.Response]) -> None:
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = len(self.requests) - 1
        if index >= len(self.responses):
            raise AssertionError(
                f"模型请求次数超出录制脚本（第 {index + 1} 次，脚本只有 "
                f"{len(self.responses)} 条）"
            )
        return self.responses[index](request)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """把 `httpx.AsyncClient` 的传输换成录制的，其余一切照旧。

    **patch 的是 `AsyncClient` 而不是 provider**：`OpenAIModelProvider` 有一个
    `transport=` 注入口，但那条路要绕开 `setup()` 自己构造 provider——那样这套用例验的就
    不再是「装配根装出来的那个实例」。换掉传输层是唯一既保留整条真实路径、又不出网的做法。
    """
    recorder = Recorder()
    real_client = httpx.AsyncClient

    class RecordedClient(real_client):  # type: ignore[misc,valid-type]
        def __init__(self, **kwargs: Any) -> None:  # boundary: 透传 httpx 的构造参数
            kwargs["transport"] = httpx.MockTransport(recorder)
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", RecordedClient)
    return recorder
