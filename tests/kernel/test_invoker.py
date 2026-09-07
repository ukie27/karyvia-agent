"""`ToolExecutor` 的行为测试。

| 组 | 验收内容 |
| --- | --- |
| A 准备 | 只读工具带幂等键 |
| B 校验 | 参数不合 schema → `ok=False` 且 `side_effect=NONE`，约定不抛 |
| C 执行 | 正常返回原样交回；handler 逸出的异常折成 `UNKNOWN` |
| D 宽限期 | 超时后请求取消并等宽限期；仍不回来 → `TIMEOUT_TOOL_CANCEL` + 孤儿登记 |
| E 注册 | `tools_from()` 认 `RegisteredTool`，别的载荷当场报错 |
"""

from __future__ import annotations

import asyncio

import pytest

from karyvia.contracts import (
    Builtin,
    CapabilityKind,
    ErrorCode,
    KaryviaError,
    RiskLevel,
    SideEffect,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from karyvia.kernel.registry import CapabilityRegistry
from karyvia.kernel.turn import CancelToken, RegisteredTool, ToolExecutor, tools_from

from ._engine_support import CORRELATION
from ._orchestrator_support import FakeToolHandler

READ_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"],
    "additionalProperties": False,
}


def spec(
    name: str = "fs.read",
    *,
    read_only: bool = True,
    parameters: dict | None = None,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"测试工具 {name}",
        parameters=parameters if parameters is not None else READ_SCHEMA,
        read_only=read_only,
        risk=RiskLevel.SAFE if read_only else RiskLevel.MUTATING,
    )


def call(name: str = "fs.read", *, call_id: str = "c1", **arguments: object) -> ToolCall:
    return ToolCall(call_id=call_id, name=name, arguments=arguments or {"path": "a.txt"})  # type: ignore[arg-type]


def executor(
    handler: FakeToolHandler | None = None,
    *,
    tool: ToolSpec | None = None,
    grace_ms: int = 20,
) -> tuple[ToolExecutor, FakeToolHandler]:
    impl = handler or FakeToolHandler()
    registered = RegisteredTool(spec=tool or spec(), handler=impl)  # type: ignore[arg-type]
    return ToolExecutor([registered], grace_ms=grace_ms), impl


async def invoke(exec_: ToolExecutor, tool_call: ToolCall, *, timeout_ms: int = 1000) -> ToolResult:
    invocation = exec_.prepare(tool_call, correlation=CORRELATION, timeout_ms=timeout_ms)
    return await exec_.invoke(invocation, CancelToken().child())


# ------------------------------------------------------------------ A 准备


def test_read_only_tools_get_an_idempotency_key_and_writers_do_not() -> None:
    reader, _ = executor()
    writer, _ = executor(
        tool=spec("fs.write", read_only=False)
    )

    assert reader.prepare(call(), correlation=CORRELATION, timeout_ms=1).auto_retry_allowed
    assert not writer.prepare(
        call("fs.write"), correlation=CORRELATION, timeout_ms=1
    ).auto_retry_allowed


def test_prepare_does_not_raise_for_an_unknown_tool() -> None:
    """`before_tool_call` 的必填槽是 `invocation`，这里抛就等于那个 Hook 永远收不到它。"""
    exec_, _ = executor()
    invocation = exec_.prepare(call("nope.missing"), correlation=CORRELATION, timeout_ms=1)
    assert invocation.call.name == "nope.missing"


# ------------------------------------------------------------------ B 校验


async def test_arguments_that_break_the_schema_are_rejected_before_execution() -> None:
    exec_, handler = executor()

    result = await invoke(exec_, ToolCall(call_id="c1", name="fs.read", arguments={"path": 42}))

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is ErrorCode.INPUT_MALFORMED
    assert result.side_effect is SideEffect.NONE
    assert handler.calls == []


async def test_a_missing_required_argument_is_rejected() -> None:
    exec_, handler = executor()

    result = await invoke(exec_, ToolCall(call_id="c1", name="fs.read", arguments={}))

    assert result.ok is False
    assert handler.calls == []


async def test_schema_errors_never_carry_the_argument_values() -> None:
    exec_, _ = executor()

    result = await invoke(
        exec_, ToolCall(call_id="c1", name="fs.read", arguments={"path": "ok", "token": "sk-live-x"})
    )

    assert result.ok is False
    assert "sk-live-x" not in repr(result.error) + str(result.error.detail if result.error else "")


async def test_an_unknown_tool_is_a_capability_missing_result() -> None:
    exec_, _ = executor()

    result = await invoke(exec_, call("nope.missing", **{"path": "a"}))

    assert result.error is not None
    assert result.error.code is ErrorCode.CAPABILITY_MISSING


async def test_a_broken_schema_declaration_is_reported_not_raised() -> None:
    exec_, _ = executor(tool=spec(parameters={"type": "not-a-type"}))

    result = await invoke(exec_, call())

    assert result.error is not None
    assert result.error.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


# ------------------------------------------------------------------ C 执行


async def test_a_successful_result_is_returned_untouched() -> None:
    handler = FakeToolHandler(
        ToolResult(
            call_id="c1", ok=True, content="12 个文件", truncated=False,
            side_effect=SideEffect.NONE,
        )
    )
    exec_, _ = executor(handler)

    result = await invoke(exec_, call())

    assert result.ok is True
    assert result.content == "12 个文件"


async def test_an_escaping_exception_becomes_an_unknown_side_effect() -> None:
    exec_, _ = executor(FakeToolHandler(error=RuntimeError("token=sk-live-leak")))

    result = await invoke(exec_, call())

    assert result.ok is False
    assert result.side_effect is SideEffect.UNKNOWN
    assert "sk-live-leak" not in repr(result.error)


# ------------------------------------------------------------------ D 宽限期


async def test_a_tool_that_stops_within_the_grace_period_keeps_its_own_result() -> None:
    stopped = ToolResult(
        call_id="c1", ok=False, content="我停下了", truncated=False,
        side_effect=SideEffect.NONE,
        error=KaryviaError(ErrorCode.CANCELLED_BY_USER, "已停止"),
    )

    async def body(invocation, cancel):  # noqa: ANN001, ANN202
        await cancel.wait()  # 收到取消就立刻收摊
        return stopped

    exec_, _ = executor(FakeToolHandler(body=body), grace_ms=1000)

    result = await invoke(exec_, call(), timeout_ms=10)

    assert result.content == "我停下了"
    assert exec_.orphans == ()


async def test_an_uncancellable_tool_returns_unknown_and_is_registered_as_an_orphan() -> None:
    async def body(invocation, cancel):  # noqa: ANN001, ANN202
        await asyncio.Event().wait()  # 永远不理会取消
        raise AssertionError("不可达")

    exec_, _ = executor(FakeToolHandler(body=body), grace_ms=10)

    result = await invoke(exec_, call(), timeout_ms=10)

    assert result.ok is False
    assert result.error is not None
    assert result.error.code is ErrorCode.TIMEOUT_TOOL_CANCEL
    assert result.side_effect is SideEffect.UNKNOWN
    assert [(item.tool, item.call_id) for item in exec_.orphans] == [("fs.read", "c1")]


async def test_the_orphan_table_is_bounded_and_counts_what_it_drops() -> None:
    async def body(invocation, cancel):  # noqa: ANN001, ANN202
        await asyncio.Event().wait()
        raise AssertionError("不可达")

    registered = RegisteredTool(spec=spec(), handler=FakeToolHandler(body=body))  # type: ignore[arg-type]
    exec_ = ToolExecutor([registered], grace_ms=5, max_orphans=1)

    for index in range(3):
        invocation = exec_.prepare(
            call(call_id=f"c{index}"), correlation=CORRELATION, timeout_ms=5
        )
        await exec_.invoke(invocation, CancelToken().child())

    assert len(exec_.orphans) == 1
    assert exec_.orphans_dropped == 2


async def test_invoke_returns_within_timeout_plus_grace() -> None:
    """`deps.ToolInvoker.invoke` 的 docstring 写死了这条上限，engine 不加第二层超时。"""

    async def body(invocation, cancel):  # noqa: ANN001, ANN202
        await asyncio.Event().wait()
        raise AssertionError("不可达")

    exec_, _ = executor(FakeToolHandler(body=body), grace_ms=20)

    async with asyncio.timeout(1):
        result = await invoke(exec_, call(), timeout_ms=20)

    assert result.side_effect is SideEffect.UNKNOWN


# ------------------------------------------------------------------ E 注册


def test_tools_from_reads_registered_tools() -> None:
    registry = CapabilityRegistry()
    with registry.batch(Builtin()) as batch:
        batch.add(
            CapabilityKind.TOOL,
            "fs.read",
            RegisteredTool(spec=spec(), handler=FakeToolHandler()),  # type: ignore[arg-type]

        )
    registry.freeze(registry.registrations)

    assert [item.spec.name for item in tools_from(registry)] == ["fs.read"]


def test_tools_from_rejects_a_foreign_payload() -> None:
    registry = CapabilityRegistry()
    with registry.batch(Builtin()) as batch:
        batch.add(CapabilityKind.TOOL, "fs.read", object())
    registry.freeze(registry.registrations)

    with pytest.raises(KaryviaError) as caught:
        tools_from(registry)
    assert caught.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


def test_specs_are_sorted_by_name() -> None:
    exec_ = ToolExecutor(
        [
            RegisteredTool(spec=spec("z.tool"), handler=FakeToolHandler()),  # type: ignore[arg-type]
            RegisteredTool(spec=spec("a.tool"), handler=FakeToolHandler()),  # type: ignore[arg-type]
        ]
    )
    assert [item.name for item in exec_.specs] == ["a.tool", "z.tool"]


async def test_tools_from_output_plugs_straight_into_the_executor() -> None:
    """`tools_from()` 的返回值就是 `ToolExecutor` 的入参，装配方不必再转一次形状。"""
    registry = CapabilityRegistry()
    with registry.batch(Builtin()) as batch:
        batch.add(
            CapabilityKind.TOOL,
            "fs.read",
            RegisteredTool(spec=spec(), handler=FakeToolHandler()),  # type: ignore[arg-type]

        )
    registry.freeze(registry.registrations)

    result = await invoke(ToolExecutor(tools_from(registry)), call())

    assert result.ok is True


async def test_a_tool_that_raises_during_the_grace_period_still_reports_unknown() -> None:
    """宽限期内它自己炸了：Kernel 仍然不知道外部世界变了没有。"""
    started = asyncio.Event()

    async def body(invocation, cancel):  # noqa: ANN001, ANN202
        started.set()
        await cancel.wait()
        raise RuntimeError("退出时炸了")

    exec_, _ = executor(FakeToolHandler(body=body), grace_ms=1000)

    result = await invoke(exec_, call(), timeout_ms=10)

    assert result.error is not None
    assert result.error.code is ErrorCode.TIMEOUT_TOOL_CANCEL
    assert result.side_effect is SideEffect.UNKNOWN
    assert exec_.orphans == ()  # 它确实结束了，不算孤儿
