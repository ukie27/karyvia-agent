"""工具执行器：schema 校验、超时、取消宽限期与孤儿任务表。

职责：实现 `deps.ToolInvoker`——把一次 `ToolCall` 补齐成带幂等键的 `ToolInvocation`，
校验参数，在 `timeout_ms + grace` 内交回一个 `ToolResult`，并把宽限期
用尽仍未返回的协程登记为孤儿任务。
不负责：决定调度顺序与批次（engine 的 `scheduling.py`）、截断结果（engine 的
`folding.py`，且必须在 `after_tool_call` **之后**）、判定「这个工具存不存在」
（engine 按 `ModelRequest.tools` 先拦）。

**三条不变量**：

1. **约定不抛**。schema 不合、超时、handler 逸出的异常一律折成
   `ToolResult(ok=False, error=...)`（`deps.ToolInvoker.invoke` 的 docstring 写死）。
   engine 兜得住逸出的异常，但那时只剩一个 `side_effect=UNKNOWN`，执行侧本来能给出的
   判断就丢了。
2. **`side_effect` 如实标注**。执行**之前**失败（schema）一律 `NONE`——工具还没被
   碰过；宽限期用尽一律 `UNKNOWN`——Kernel 不知道那个还在跑的协程做了什么。谎报 `NONE`
   会让用户据此重试并造成重复副作用。
3. **必须在 `timeout_ms + grace` 内返回**。engine 不加第二层超时，因此这里是唯一的时间
   闸门：先 `wait_for(timeout_ms)`，超时后请求子令牌取消并再等 `grace`，仍不回来就把
   task 丢进孤儿表、自己造一份结果返回。

**为什么 `jsonschema` 在函数内 import**：与 `config/schema.py::to_limits` 同一条理由——
`import kernel.turn` 出现在诊断与配置以外的很多路径上，而 `jsonschema` 的导入是几十毫秒
。真的要执行工具的调用方才付这笔钱，编译好的 validator
按 `ToolSpec` 缓存，一个工具只编译一次。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Final, Protocol, cast

from karyvia.contracts import (
    CancelSignal,
    CapabilityKind,
    Correlation,
    ErrorCode,
    JsonValue,
    KaryviaError,
    SideEffect,
    ToolCall,
    ToolHandler,
    ToolInvocation,
    ToolResult,
    ToolSpec,
)

from ..registry import CapabilityRegistry
from .cancel import DEFAULT_TOOL_CANCEL_GRACE_MS, CancelToken

__all__ = [
    "DEFAULT_MAX_ORPHANS",
    "OrphanTask",
    "RegisteredTool",
    "ToolExecutor",
    "tools_from",
]

#: 孤儿任务表的上限。它只用于诊断（「这个实例留下了几个还在跑的工具」），无界会让一个
#: 反复超时的工具把内存吃光。满了丢最旧并计数。
DEFAULT_MAX_ORPHANS: Final = 64

#: 单次校验最多报几条问题。全报会让一个字段名写错的大对象刷出上百行 detail，而用户
#: 需要的是前几条。
_MAX_SCHEMA_PROBLEMS: Final = 8


class _SchemaProblem(Protocol):
    """`jsonschema.ValidationError` 里本模块用到的两个字段。

    `jsonschema` 没有 `py.typed`，它交出来的一切在类型层都是未知（`AGENTS.md` 原则 6：
    在边界类型化动态数据）。声明一个只含**我们真正读的字段**的 Protocol，再在
    `_compile()` 那一处收口——比让 `Unknown` 沿着调用链渗进 `KaryviaError.detail` 好。
    """

    @property
    def absolute_path(self) -> Iterable[object]: ...

    @property
    def validator(self) -> object: ...


class _Validator(Protocol):
    """编译好的校验器。同样只声明本模块用到的那一个方法。"""

    def iter_errors(self, instance: object) -> Iterable[_SchemaProblem]: ...


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """`CapabilityKind.TOOL` 的注册载荷形状（与 `hooks.RegisteredHook` 同构）。"""

    spec: ToolSpec
    handler: ToolHandler


@dataclass(frozen=True, slots=True)
class OrphanTask:
    """一个宽限期用尽仍未返回的工具协程。

    留 `call_id` 与 `turn_id` 而不是 task 对象本身的 repr：诊断要回答的是「哪一次调用
    可能还在改外部世界」，而不是「哪个 Python 对象还活着」。
    """

    call_id: str
    tool: str
    turn_id: str
    grace_ms: int


def tools_from(registry: CapabilityRegistry) -> tuple[RegisteredTool, ...]:
    """从已冻结的 registry 取出全部生效的工具。

    返回元组而不是 `name -> tool` 的映射：`ToolExecutor` 只接一种入参形状，装配方就不必
    在两种形状之间挑一个（而「`Mapping` 也是 `Iterable`」这件事对类型检查器同样是歧义）。

    **异常约定**：registry 未冻结或载荷形状不对时抛 `KERNEL_INVARIANT_VIOLATED`。
    """
    tools: list[RegisteredTool] = []
    for registration in registry.of_kind(CapabilityKind.TOOL):
        payload = registration.payload
        if not isinstance(payload, RegisteredTool):
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "TOOL 能力的注册载荷必须是 RegisteredTool。",
                detail={"capability": registration.ref.target},
                capability=registration.ref,
            )
        tools.append(payload)
    return tuple(tools)


class ToolExecutor:
    """`deps.ToolInvoker` 的生产实现。"""

    def __init__(
        self,
        tools: Iterable[RegisteredTool] = (),
        *,
        grace_ms: int = DEFAULT_TOOL_CANCEL_GRACE_MS,
        max_orphans: int = DEFAULT_MAX_ORPHANS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._tools: dict[str, RegisteredTool] = {item.spec.name: item for item in tools}
        self._grace_ms = grace_ms
        self._max_orphans = max_orphans
        self._clock = clock
        self._orphans: list[OrphanTask] = []
        self._orphans_dropped = 0
        self._validators: dict[str, _Validator] = {}

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        """全部生效工具的声明，按名字排序。模型可见列表与调度列表由此同源。"""
        return tuple(sorted((item.spec for item in self._tools.values()), key=lambda s: s.name))

    @property
    def orphans(self) -> tuple[OrphanTask, ...]:
        """当前登记的孤儿任务（实例停止报告用）。"""
        return tuple(self._orphans)

    @property
    def orphans_dropped(self) -> int:
        """因超出上限被挤掉的孤儿条数。「表里没有」与「被挤掉了」是两个结论。"""
        return self._orphans_dropped

    def prepare(
        self, call: ToolCall, *, correlation: Correlation, timeout_ms: int
    ) -> ToolInvocation:
        """补齐执行上下文中的幂等键。

        **异常约定**：约定不抛。工具不存在时仍返回调用上下文——`invoke()` 会把它
        折成 `CAPABILITY_MISSING` 结果，而在这里抛会让 engine 的 `before_tool_call` Hook
        永远拿不到 `invocation`（那是它的必填槽）。
        **取消语义**：纯策略查询，不涉及取消。
        """
        registered = self._tools.get(call.name)
        read_only = registered is not None and registered.spec.read_only
        return ToolInvocation(
            call=call,
            correlation=correlation,
            timeout_ms=timeout_ms,
            # 只有只读工具默认带幂等键：重放一次只读调用永远安全，而写类工具的「重试是否
            # 安全」只有工具自己知道。
            idempotency_key=f"{call.name}:{call.call_id}" if read_only else None,
        )

    async def invoke(self, invocation: ToolInvocation, cancel: CancelSignal) -> ToolResult:
        """执行一次调用。顺序固定为 schema → 执行。

        **异常约定**：约定不抛。
        **取消语义**：`cancel` 是本次调用专属的子令牌；宽限期赛跑与孤儿登记在这里完成。
        """
        call = invocation.call
        registered = self._tools.get(call.name)
        if registered is None:
            return self._failed(
                call,
                KaryviaError(
                    ErrorCode.CAPABILITY_MISSING,
                    "没有名为该名字的生效工具。",
                    detail={"tool": call.name, "call_id": call.call_id},
                ),
            )

        invalid = self._validate(registered.spec, call)
        if invalid is not None:
            return self._failed(call, invalid)

        return await self._execute(registered, invocation, cancel)

    # ------------------------------------------------------------------ 内部

    async def _execute(
        self, registered: RegisteredTool, invocation: ToolInvocation, cancel: CancelSignal
    ) -> ToolResult:
        """跑 handler，超时后与宽限期赛跑，仍不回来就登记孤儿。"""
        call = invocation.call
        started = self._clock()
        task = asyncio.ensure_future(registered.handler.execute(invocation, cancel))
        try:
            result = await asyncio.wait_for(
                asyncio.shield(task), timeout=invocation.timeout_ms / 1000
            )
        except TimeoutError:
            return await self._after_timeout(task, invocation, cancel, started)
        # handler 约定不抛；逸出的异常折成结果，但 `side_effect` 只能是 UNKNOWN——
        # 一个抛到一半的 handler 有没有改外部世界，Kernel 无从判断。
        except Exception as error:
            return self._failed(
                call,
                KaryviaError(
                    ErrorCode.KERNEL_UNEXPECTED,
                    "工具实现抛出了异常。",
                    detail={"tool": call.name, "exception": type(error).__name__},
                ),
                side_effect=SideEffect.UNKNOWN,
                duration_ms=self._elapsed_ms(started),
            )
        return result

    async def _after_timeout(
        self,
        task: asyncio.Future[ToolResult],
        invocation: ToolInvocation,
        cancel: CancelSignal,
        started: float,
    ) -> ToolResult:
        """超时后的宽限期：请求取消，再等 `grace_ms`，仍不回来就登记孤儿。

        **不 `task.cancel()`**：`CancelledError` 会在任意 await 点炸开，工具没有机会
        「保存已做的事再退出」——`CancelToken` 的存在本身就是为了避免这种停法（§6.4）。
        """
        if isinstance(cancel, CancelToken):
            cancel.request()
        call = invocation.call
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=self._grace_ms / 1000)
        except TimeoutError:
            self._register_orphan(invocation)
        except Exception:
            # 宽限期内它自己炸了：仍然按超时结论返回——那个 `UNKNOWN` 是对的。
            pass
        return self._failed(
            call,
            KaryviaError(
                ErrorCode.TIMEOUT_TOOL_CANCEL,
                "工具超时后在取消宽限期内仍未返回，副作用未知。",
                detail={
                    "tool": call.name,
                    "timeout_ms": invocation.timeout_ms,
                    "grace_ms": self._grace_ms,
                },
            ),
            side_effect=SideEffect.UNKNOWN,
            duration_ms=self._elapsed_ms(started),
        )

    def _register_orphan(self, invocation: ToolInvocation) -> None:
        if len(self._orphans) >= self._max_orphans:
            self._orphans.pop(0)
            self._orphans_dropped += 1
        self._orphans.append(
            OrphanTask(
                call_id=invocation.call.call_id,
                tool=invocation.call.name,
                turn_id=invocation.correlation.turn_id,
                grace_ms=self._grace_ms,
            )
        )

    def _validate(self, spec: ToolSpec, call: ToolCall) -> KaryviaError | None:
        """按 `spec.parameters` 校验参数（`§10.2` 第 10 步）。

        错误里只放 schema 路径与校验器给的说明，**不放参数值**：工具参数里可能是凭据、
        文件内容或用户隐私，而 `KaryviaError.detail` 会原样进事件与日志。
        """
        validator = self._validators.get(spec.name)
        if validator is None:
            compiled = _compile(dict(spec.parameters))
            if isinstance(compiled, KaryviaError):
                return compiled
            validator = self._validators.setdefault(spec.name, compiled)
        try:
            problems = sorted(
                validator.iter_errors(dict(call.arguments)),
                key=lambda item: [str(part) for part in item.absolute_path],
            )
        # 校验器自己炸了（罕见的 `$ref` / 自定义 format 问题）同样折成结果，不打掉 turn。
        except Exception as error:
            return KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "校验工具参数时校验器自身失败。",
                detail={"tool": spec.name, "exception": type(error).__name__},
            )
        if not problems:
            return None
        return KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            "工具参数不符合它声明的 schema。",
            detail={
                "tool": spec.name,
                "call_id": call.call_id,
                "problems": [
                    {
                        "path": "/" + "/".join(str(part) for part in problem.absolute_path),
                        "validator": str(problem.validator),
                    }
                    for problem in problems[:_MAX_SCHEMA_PROBLEMS]
                ],
            },
        )

    def _elapsed_ms(self, started: float) -> int:
        return max(0, int((self._clock() - started) * 1000))

    def _failed(
        self,
        call: ToolCall,
        error: KaryviaError,
        *,
        side_effect: SideEffect = SideEffect.NONE,
        duration_ms: int = 0,
    ) -> ToolResult:
        return ToolResult(
            call_id=call.call_id,
            ok=False,
            content=error.user_message,
            truncated=False,
            side_effect=side_effect,
            error=error,
            duration_ms=duration_ms,
        )


def _compile(schema: dict[str, JsonValue]) -> _Validator | KaryviaError:
    """把一份 JSON Schema 编译成校验器。这是 `jsonschema` 唯一的接触点。

    **惰性 import**：与 `config/schema.py::to_limits` 同一条理由——`import kernel.turn`
    出现在诊断与配置以外的很多路径上，而 `jsonschema` 的导入是几十毫秒（ 给整个
    冷启动 300 ms）。真的要执行工具的调用方才付这笔钱。

    **schema 写错了返回错误而不是抛**：那是工具作者的 bug，不该打掉整个 turn。

    `cast` 有运行时检查支撑（`isinstance` 对 `runtime_checkable` Protocol 查属性存在性），
    这是 `AGENTS.md` 原则 6 对 `cast`当前约束：`jsonschema` 没有 `py.typed`，它交出来的
    一切在类型层都是未知，收口必须在这一处完成。
    """
    import jsonschema.validators as js  # boundary: 无类型标注的第三方库，形状在本函数内收口

    try:
        cls = js.validator_for(schema)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        # `check_schema` 必须显式调用：不调用的话，一个写错的 schema 要等到**校验参数时**
        # 才炸，异常从 `iter_errors` 里逸出，「约定不抛」当场失效。
        cls.check_schema(schema)  # pyright: ignore[reportUnknownMemberType]
        compiled: object = cls(schema)
    except Exception as error:
        return KaryviaError(
            ErrorCode.KERNEL_INVARIANT_VIOLATED,
            "工具声明的参数 schema 本身不合法。",
            detail={"exception": type(error).__name__},
        )
    if not callable(getattr(compiled, "iter_errors", None)):
        return KaryviaError(
            ErrorCode.KERNEL_INVARIANT_VIOLATED,
            "jsonschema 交回的对象没有 iter_errors，无法用作校验器。",
            detail={"type": type(compiled).__name__},
        )
    return cast(_Validator, compiled)
