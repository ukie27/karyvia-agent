"""`mcp` 插件测试的公共闸门：断言它不碰真实网络。

职责：一条 autouse 夹具，把出站连接与名字解析拦掉。本插件的用例全部走一个不碰
`mcp` SDK 的 `Connector` 替身——本夹具是「一个 socket 都没开」这件事的可执行断言。
不负责：拦子进程（本插件的用例一个都不起）。

**为什么不整体拦掉 `socket.socket` 的构造**：Windows 上 asyncio 的 `ProactorEventLoop`
自身要用 `socket.socketpair()` 做 self-pipe。拦构造会把事件循环一起拦掉。
因此闸门放在**去哪儿**上：回环放行，其余一律失败并指名道姓。

这是全项目第八份同样判据的实现。刻意不共享：`R4` 禁止插件 import 宿主的测试树。
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import pytest

_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", "", None})

_MESSAGE = "mcp 插件的测试试图访问真实网络"


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
