"""`feishu` 插件测试的公共闸门：断言它不碰真实网络。

职责：一条 autouse 夹具，把出站连接与名字解析拦掉。本插件的用例全部走 `tests/_feishu_fakes.py`
的假平台，**连 `lark-oapi` 都不需要装**——本夹具是「一个 socket 都没开」这件事的可执行
断言，而不是一句承诺。
不负责：限制文件访问（本插件一个字节都不写盘）。

**本测试树里禁止 `pytest.importorskip`。** legacy 的 16 个飞书测试文件里有 2 个用它，
后果是 CI 没装 `lark-oapi` 时那些用例**静默全跳**——一条永远不会失败的测试比没有测试更糟。
需要「没装 SDK 时会怎样」的用例，用
`monkeypatch.setitem(sys.modules, "lark_oapi", None)` 造出那个状态（CPython 有文档的行为）。

**为什么不整体拦掉 `socket.socket` 的构造**：Windows 上 asyncio 的 `ProactorEventLoop`
自身要用 `socket.socketpair()` 做 self-pipe，而那是一对回环连接。拦构造会把事件循环一起
拦掉，测试就只能证明「起不来」。因此闸门放在**去哪儿**上：回环放行，其余一律失败并指名
道姓。

这是全项目第四份同样判据的实现（另三份在 `tests/builtins/`、`tests/integration/` 与
`plugins/karyvia-plugin-anthropic/tests/`）。刻意不共享：`R4` 禁止插件 import 宿主的
测试树，而这些测试树各自可以独立运行。
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import pytest

#: 允许的目标。事件循环的 self-pipe 只连这几个。
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", "", None})

_MESSAGE = "feishu 插件的测试试图访问真实网络"


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
