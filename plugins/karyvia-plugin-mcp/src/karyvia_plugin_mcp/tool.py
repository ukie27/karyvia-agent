"""桥接工具的执行体：一次本地调用 → 一次远端调用。

职责：`ToolHandler` 实现，把 `ToolInvocation` 翻成远端 `call_tool`、把结果折成
`ToolResult`。
不负责：连接与会话（`supervisor.py` / `client.py`）、线格式（`translate.py`）。

**`side_effect` 恒为 `UNKNOWN`，这是本插件与其它工具插件最大的一处差异。**
远端工具做了什么、有没有做完，本插件**一无所知**——MCP 协议不报告副作用，而一个
`fs.write` 型的远端工具与一个 `search` 型的在线格式上长得一模一样。因此：

- 成功时标 `UNKNOWN`：调用确实发生了，但「外部世界变了没有」答不上来。
- **失败在发起之前**（参数非法、会话不可用、入口取消）标 `NONE`：那时一个字节都没出去。
- 失败在发起之后（超时、传输中断）标 `UNKNOWN`：请求已经送出去了。

这与 `builtins/tools_shell/executor.py::_fold` 的三档判据是同一条，只是第二档
（「进程自己退出 → `OCCURRED`」）在这里不可达。谎报 `NONE` 会让编排层以为可以安全重试
一次可能已经生效的写操作。

**`read_only` 恒为 `False`、`risk` 恒为 `MUTATING`**，同一条理由：说不清就往严的那边报。
远端 server 的 `annotations.readOnlyHint` 是**它自己说的**，而它正是那个不可信的一方。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Final

from karyvia.contracts import (
    CancelSignal,
    Concurrency,
    ErrorCode,
    JsonValue,
    KaryviaError,
    RiskLevel,
    SideEffect,
    ToolInvocation,
    ToolResult,
    ToolSpec,
    TrustLevel,
)

from .session import McpSession, RemoteTool
from .translate import describe_tool, render_result, tool_parameters, truncate

__all__ = ["BridgedTool", "SessionSource", "tool_spec"]

_SESSION_GONE: Final = "这个 MCP server 的连接已经不可用了。"
_CALL_TIMEOUT: Final = "MCP 工具调用超时。"
_CALL_FAILED: Final = "MCP 工具调用失败。"
_REMOTE_ERROR: Final = "MCP 工具报告了失败。"


class SessionSource:
    """一个可变的会话持有者。

    工具在 `setup()` 里就注册好了，而会话属于一条后台任务（见 `supervisor.py`）。
    直接把 `McpSession` 塞进工具会让「连接断了」变成一个拿着死对象的工具；
    经这一层取，则断连后每次调用都能如实报 `SESSION_GONE`。
    """

    __slots__ = ("session",)

    def __init__(self, session: McpSession | None = None) -> None:
        self.session = session


def tool_spec(local_name: str, server: str, remote: RemoteTool) -> ToolSpec:
    """一条桥接工具的声明。"""
    return ToolSpec(
        name=local_name,
        description=describe_tool(server, remote.name, remote.description),
        parameters=tool_parameters(remote.input_schema),
        # 远端 server 使用自己的进程与网络；这条声明只描述工具执行语义。
        read_only=False,
        risk=RiskLevel.MUTATING,
        # 远端 server 的并发安全性未知，逐个串行是唯一不用赌的选择。
        concurrency=Concurrency.EXCLUSIVE,
    )


class BridgedTool:
    """把一次本地调用转给远端 server。"""

    __slots__ = ("_limit", "_remote_name", "_server", "_source", "_timeout_ms")

    def __init__(
        self,
        source: SessionSource,
        server: str,
        remote_name: str,
        *,
        timeout_ms: int,
        limit: int,
    ) -> None:
        self._source = source
        self._server = server
        self._remote_name = remote_name
        self._timeout_ms = timeout_ms
        self._limit = limit

    async def execute(self, invocation: ToolInvocation, cancel: CancelSignal) -> ToolResult:
        """**约定不抛**。**取消语义**：入口检查一次；远端调用本身由 `timeout_ms` 收口，
        MCP 协议没有取消一次进行中调用的手段，因此中途没有可插的检查点。"""
        started = time.perf_counter()
        try:
            cancel.raise_if_requested()
            session = self._session()
        except KaryviaError as error:
            # 还没发出去：一个字节都没到远端。
            return self._failed(invocation, error, started, SideEffect.NONE)

        try:
            result = await session.call_tool(
                self._remote_name, dict(invocation.call.arguments), timeout_ms=self._timeout_ms
            )
        except TimeoutError:
            return self._failed(
                invocation,
                KaryviaError(
                    ErrorCode.TIMEOUT_TOOL_CALL,
                    _CALL_TIMEOUT,
                    detail={"server": self._server, "tool": self._remote_name},
                ),
                started,
                # 请求已经送出去了，远端可能已经做完了。
                SideEffect.UNKNOWN,
            )
        except Exception as error:  # noqa: BLE001 - 第三方会话什么都可能抛，见下
            # **捕 `Exception` 不捕 `BaseException`**：取消与 Ctrl-C 要放行
            # （engine 与 dispatcher 的同一条）。`detail` 里只放**类型名不放消息**——
            # 第三方 server 的异常文本可能带着它自己的凭据（`D13` 的先例）。
            return self._failed(
                invocation,
                KaryviaError(
                    ErrorCode.EXTERNAL_TOOL_SERVER,
                    _CALL_FAILED,
                    detail={
                        "server": self._server,
                        "tool": self._remote_name,
                        "cause": type(error).__name__,
                    },
                    retryable=True,
                ),
                started,
                SideEffect.UNKNOWN,
            )

        text, cut = render_result(result, self._limit)
        data: Mapping[str, JsonValue] | None = (
            {"structured": result.structured} if result.structured is not None else None
        )
        if result.is_error:
            # **工具自己报告的失败不是传输故障**：模型该看到那段文本并据此改主意。
            return ToolResult(
                call_id=invocation.call.call_id,
                ok=False,
                content=text,
                truncated=cut,
                side_effect=SideEffect.UNKNOWN,
                data=data,
                error=KaryviaError(
                    ErrorCode.EXTERNAL_TOOL_SERVER,
                    _REMOTE_ERROR,
                    detail={"server": self._server, "tool": self._remote_name},
                ),
                duration_ms=_elapsed_ms(started),
            )
        return ToolResult(
            call_id=invocation.call.call_id,
            ok=True,
            content=text,
            truncated=cut,
            side_effect=SideEffect.UNKNOWN,
            data=data,
            duration_ms=_elapsed_ms(started),
            # 远端 server 交回来的文本。**它与 `side_effect=UNKNOWN` 是同一条判断**：
            # 我们对那一端一无所知，因此既不敢说它没有副作用，也不敢说它的输出是可信
            # 指令（`D42`；默认值就是它，写出来是为了让这条判断在代码里可见）。
            trust=TrustLevel.UNTRUSTED,
        )

    def _session(self) -> McpSession:
        session = self._source.session
        if session is None:
            raise KaryviaError(
                ErrorCode.CAPABILITY_MISSING,
                _SESSION_GONE,
                detail={"server": self._server},
            )
        return session

    def _failed(
        self,
        invocation: ToolInvocation,
        error: KaryviaError,
        started: float,
        side_effect: SideEffect,
    ) -> ToolResult:
        text, cut = truncate(error.user_message, self._limit)
        return ToolResult(
            call_id=invocation.call.call_id,
            ok=False,
            content=text,
            truncated=cut,
            side_effect=side_effect,
            error=error,
            duration_ms=_elapsed_ms(started),
            # 本地生成的失败文案（连不上、超时、参数不合）。远端**自己报告**的失败
            # 走上面那条 `is_error` 分支，那段文本是它写的，保持不可信。
            trust=TrustLevel.SYSTEM,
        )


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))
