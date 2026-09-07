"""工具实现的公共骨架：参数读取、失败折叠与 `ToolResult` 构造。

职责：`FsTool` 基类（把 `execute()` 的「约定不抛」一次性做对）、一组参数读取器、
以及 `ToolResult` 的两个构造口。
不负责：具体工具的语义（`readers.py` / `writers.py` / `search.py`）、路径判定
（`paths.py`）、截断规则（`content.py`）。

**`execute()` 约定不抛，这里是那条约定的唯一实现处**。契约写得很清楚：逸出的异常会被
Kernel 记成 `side_effect=UNKNOWN`——它无从判断外部世界变了没有，只能如实说不知道。而本包
的五个工具**总是知道**：所有失败都发生在真正落盘之前（`writers.py` 用临时文件 + 原子
替换，替换成功之后就没有可失败的步骤了），因此折出来的失败一律 `SideEffect.NONE`。
让每个工具各写一遍 try/except 迟早会漏掉一个分支，而漏掉的代价是一次谎报。

**堆栈不进 `content`**：那是模型可见文本。折叠后的 `content` 只有一句人话，机器可读的
细节在 `error.detail` 里（`KaryviaError` 构造时已完成脱敏）。
"""

from __future__ import annotations

import time
from collections.abc import Collection, Mapping
from typing import Final

from karyvia.contracts import (
    CancelSignal,
    ErrorCode,
    JsonValue,
    KaryviaError,
    SideEffect,
    ToolInvocation,
    ToolResult,
    TrustLevel,
)

from .content import truncate
from .paths import WorkspaceGuard
from .settings import FsToolSettings

__all__ = [
    "FsTool",
    "failure",
    "optional_bool",
    "optional_int",
    "optional_str",
    "reject_unknown_arguments",
    "require_str",
    "success",
]

#: `OSError` 折成的两个错误码。读写分开是为了让诊断能直接回答「是读不出来还是写不进去」。
_READ_FAILED: Final = ErrorCode.PERSISTENCE_READ_FAILED
_WRITE_FAILED: Final = ErrorCode.PERSISTENCE_WRITE_FAILED


def _malformed(message: str, **detail: object) -> KaryviaError:
    return KaryviaError(ErrorCode.INPUT_MALFORMED, message, detail=detail)


def reject_unknown_arguments(
    arguments: Mapping[str, JsonValue], allowed: Collection[str]
) -> None:
    """表外参数是错误，不是可以忽略的多余字段。

    Kernel 的 `ToolInvoker` 已经按 schema 校验过一遍（`additionalProperties: false`），
    这里再挡一次是因为 `ToolHandler` 是公开契约：插件作者可以直接调它，`sdk.testing` 的
    `ToolContract` 也直接调它。只在一处校验等于把「谁负责校验」的答案藏进了调用链。
    """
    unknown = sorted(set(arguments) - set(allowed))
    if unknown:
        raise _malformed("出现了未知参数。", unknown=unknown, allowed=sorted(allowed))


def require_str(arguments: Mapping[str, JsonValue], key: str) -> str:
    """取一个必填的非空字符串参数。"""
    value = arguments.get(key)
    if not isinstance(value, str) or not value:
        raise _malformed(
            "缺少必填参数或类型不对（应为非空字符串）。",
            argument=key,
            actual_type=type(value).__name__,
        )
    return value


def optional_str(arguments: Mapping[str, JsonValue], key: str, default: str) -> str:
    """取一个可选的字符串参数。给了 `null` 视作没给。"""
    value = arguments.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise _malformed(
            "参数类型不对（应为字符串）。", argument=key, actual_type=type(value).__name__
        )
    return value


def optional_int(
    arguments: Mapping[str, JsonValue], key: str, default: int, *, minimum: int
) -> int:
    """取一个可选的整数参数。`bool` 不算整数——`"limit": true` 显然是写错了。"""
    value = arguments.get(key)
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise _malformed(
            "参数类型不对或超出范围。",
            argument=key,
            minimum=minimum,
            actual_type=type(value).__name__,
        )
    return value


def optional_bool(arguments: Mapping[str, JsonValue], key: str, default: bool) -> bool:
    """取一个可选的布尔参数。"""
    value = arguments.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise _malformed(
            "参数类型不对（应为布尔值）。", argument=key, actual_type=type(value).__name__
        )
    return value


def success(
    call_id: str,
    content: str,
    *,
    limit: int,
    side_effect: SideEffect,
    data: Mapping[str, JsonValue] | None = None,
    duration_ms: int = 0,
    truncated: bool = False,
    trust: TrustLevel = TrustLevel.UNTRUSTED,
) -> ToolResult:
    """构造一个成功结果，顺带把 `content` 收进上限。

    `truncated` 是**或**上去的：调用方可能已经在更早的地方截过一次（`fs.read` 的
    `max_read_bytes`、`fs.grep` 的匹配数封顶），那一次也算截断。

    **`trust` 由调用方给，默认不可信**。四个读类工具交出的是磁盘上的内容——
    文件正文、匹配行、目录里的名字——那些一个字都不是本工具写的。只有 `fs.write` 的
    回执是自己的话，它显式传 `SYSTEM`。
    """
    text, cut = truncate(content, limit)
    return ToolResult(
        call_id=call_id,
        ok=True,
        content=text,
        truncated=truncated or cut,
        side_effect=side_effect,
        data=data,
        duration_ms=duration_ms,
        trust=trust,
    )


def failure(
    call_id: str,
    error: KaryviaError,
    *,
    limit: int,
    side_effect: SideEffect = SideEffect.NONE,
    duration_ms: int = 0,
) -> ToolResult:
    """把一个错误折成结果（`ok=False` 必带 `error`，契约已强制）。

    **失败一律 `SYSTEM`**：`user_message` 是本层自己写的固定文案，越界错误的 `detail`
    里也只有插件给的那个相对串（见 `paths.py`）。把它包成不可信数据块，会让「工具没能
    做成这件事」读起来像一段来路不明的文本，而模型接下来要照它改正参数。
    """
    text, truncated = truncate(error.user_message, limit)
    return ToolResult(
        call_id=call_id,
        ok=False,
        content=text,
        truncated=truncated,
        side_effect=side_effect,
        error=error,
        duration_ms=duration_ms,
        trust=TrustLevel.SYSTEM,
    )


class FsTool:
    """五个工具的公共外壳：计时、取消检查与失败折叠。

    子类只实现 `run()`，可以自由抛 `KaryviaError` 或 `OSError`——两者都会被折成
    `ok=False` 的结果。子类**不该**捕获它们自己：那样会让「失败是一等结果」这件事在
    五个文件里各实现一遍。
    """

    __slots__ = ("_guard", "_settings")

    #: 折叠 `OSError` 时用的错误码。写工具在 `writers.py` 里覆盖成 `_WRITE_FAILED`。
    os_error_code: ErrorCode = _READ_FAILED

    #: 成功时的副作用。只读工具恒为 `NONE`，契约测试会验这一条。
    side_effect: SideEffect = SideEffect.NONE

    def __init__(self, guard: WorkspaceGuard, settings: FsToolSettings) -> None:
        self._guard = guard
        self._settings = settings

    @property
    def guard(self) -> WorkspaceGuard:
        return self._guard

    @property
    def settings(self) -> FsToolSettings:
        return self._settings

    async def execute(self, invocation: ToolInvocation, cancel: CancelSignal) -> ToolResult:
        """执行一次调用。**约定不抛**，见模块 docstring。

        **取消语义**：入口检查一次，长循环的工具在 `run()` 内部继续检查。取消后仍返回
        `ToolResult`，`side_effect` 如实填 `NONE`——本包的失败全部发生在落盘之前。
        """
        started = time.perf_counter()
        call_id = invocation.call.call_id
        limit = self._settings.max_result_chars
        try:
            cancel.raise_if_requested()
            return await self.run(invocation, cancel, started)
        except KaryviaError as error:
            return failure(call_id, error, limit=limit, duration_ms=_elapsed_ms(started))
        except OSError as error:
            folded = KaryviaError(
                self.os_error_code,
                "文件系统操作失败。",
                detail={"errno": error.errno, "reason": type(error).__name__},
            )
            return failure(call_id, folded, limit=limit, duration_ms=_elapsed_ms(started))

    async def run(
        self, invocation: ToolInvocation, cancel: CancelSignal, started: float
    ) -> ToolResult:
        """子类实现的真正执行体。

        `started` 由 `execute()` 传进来而不是记在 `self` 上：同一个 handler 实例会被并发
        调用（`Concurrency.PARALLEL`），把计时挂在实例上就等于让两次调用互相污染。
        """
        raise NotImplementedError

    def done(
        self,
        invocation: ToolInvocation,
        content: str,
        *,
        started: float,
        data: Mapping[str, JsonValue] | None = None,
        truncated: bool = False,
        trust: TrustLevel = TrustLevel.UNTRUSTED,
    ) -> ToolResult:
        """子类构造成功结果的统一出口。`trust` 默认不可信，见 `success()`。"""
        return success(
            invocation.call.call_id,
            content,
            limit=self._settings.max_result_chars,
            side_effect=self.side_effect,
            data=data,
            duration_ms=_elapsed_ms(started),
            truncated=truncated,
            trust=trust,
        )


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))
