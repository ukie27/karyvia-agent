"""内建能力测试的公共闸门：断言它们不碰真实网络。

职责：一条 autouse 夹具，把出站连接与名字解析拦掉。 的 `model_openai` 是第一个会
发 HTTP 的内建，它的用例全部走 `httpx.MockTransport`——本夹具是「一个 socket 都没开」
这件事的可执行断言，而不是一句承诺。
不负责：限制文件访问（`session_jsonl` 的用例本来就只写 `tmp_path`）。

**为什么不整体拦掉 `socket.socket` 的构造**：Windows 上 asyncio 的 `ProactorEventLoop`
自身要用 `socket.socketpair()` 做 self-pipe，而那是一对回环连接。拦构造会把事件循环一起
拦掉，测试就只能证明「起不来」。因此闸门放在**去哪儿**上：回环放行，其余一律失败并指名
道姓。与 `tests/integration/conftest.py` 同一条判据。
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import pytest

#: 允许的目标。事件循环的 self-pipe 只连这几个。
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", "", None})

_MESSAGE = "内建能力的测试试图访问真实网络"


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
