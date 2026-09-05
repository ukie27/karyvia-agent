"""`HookRouter` 的行为测试（`D14` 验收：§6.6 的两类扩展点）。

| 组 | 验收内容 |
| --- | --- |
| A 顺序 | 拦截器按 `(priority, provider, name)` 顺序执行且多次运行一致（`CTX-002`） |
| B 累积与短路 | `REPLACE` 逐个回灌、`REJECT`/`BLOCK` 首个即短路 |
| C 隔离 | 观察者与拦截器异常/超时不影响 turn（`NFR-204`） |
| E 形状 | 用错处置/载荷被当作插件故障，而不是静默忽略 |
| F 注册 | `bindings_from()` 认 `RegisteredHook`，别的载荷当场报错 |
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from karyvia.contracts import (
    Builtin,
    CapabilityKind,
    ErrorCode,
    HookAction,
    HookContext,
    HookName,
    HookOutcome,
    KaryviaError,
    ModelMessage,
    ModelRequest,
    Plugin,
    PluginId,
    Role,
)
from karyvia.kernel.registry import CapabilityRegistry
from karyvia.kernel.turn import (
    HookBinding,
    HookRouter,
    RegisteredHook,
    bindings_from,
)

from ._engine_support import CORRELATION


def _now() -> datetime:
    return datetime(2026, 8, 12, tzinfo=UTC)


class Script:
    """一个可脚本化的 `HookHandler`，顺带记下自己被调用的次序。"""

    def __init__(
        self,
        label: str,
        trace: list[str],
        *,
        outcome: HookOutcome | None = None,
        error: BaseException | None = None,
        hang: bool = False,
    ) -> None:
        self.label = label
        self._trace = trace
        self._outcome = outcome
        self._error = error
        self._hang = hang

    async def handle(self, context: HookContext) -> HookOutcome | None:
        self._trace.append(self.label)
        if self._hang:
            await asyncio.Event().wait()
        if self._error is not None:
            raise self._error
        return self._outcome


def binding(
    handler: object,
    *,
    hook: HookName = HookName.BEFORE_MODEL_REQUEST,
    priority: int = 0,
    name: str = "a",
    plugin: str | None = None,
) -> HookBinding:
    return HookBinding(
        hook=hook,
        handler=handler,  # type: ignore[arg-type]  # boundary: 结构化子类型
        provider=Plugin(PluginId(plugin)) if plugin else Builtin(),
        name=name,
        priority=priority,
    )


def request_context(text: str = "hi") -> HookContext:
    return HookContext(
        HookName.BEFORE_MODEL_REQUEST,
        correlation=CORRELATION,
        request=ModelRequest(
            model_id="fake-model",
            messages=(ModelMessage(role=Role.USER, content=text),),
            correlation=CORRELATION,
        ),
    )


def replace_request(text: str) -> HookOutcome:
    return HookOutcome(
        action=HookAction.REPLACE,
        request=ModelRequest(
            model_id="fake-model",
            messages=(ModelMessage(role=Role.USER, content=text),),
            correlation=CORRELATION,
        ),
    )


# ------------------------------------------------------------------ A 顺序


async def test_interceptors_run_in_priority_then_provider_then_name_order() -> None:
    trace: list[str] = []
    router = HookRouter(
        [
            binding(Script("plugin-b", trace), priority=10, name="b", plugin="beta"),
            binding(Script("plugin-a", trace), priority=10, name="a", plugin="alpha"),
            binding(Script("builtin-late", trace), priority=10, name="z"),
            binding(Script("builtin-first", trace), priority=0, name="a"),
        ]
    )

    await router.dispatch(request_context())

    assert trace == ["builtin-first", "builtin-late", "plugin-a", "plugin-b"]


async def test_interceptor_order_is_deterministic_across_runs() -> None:
    trace: list[str] = []
    router = HookRouter(
        [
            binding(Script("x", trace), name="x", plugin="one"),
            binding(Script("y", trace), name="y", plugin="two"),
            binding(Script("z", trace), name="z"),
        ]
    )

    for _ in range(5):
        await router.dispatch(request_context())

    assert trace == ["z", "x", "y"] * 5


async def test_no_bindings_is_a_continue() -> None:
    outcome = await HookRouter().dispatch(request_context())
    assert outcome.action is HookAction.CONTINUE


def test_bindings_for_exposes_the_sorted_bindings() -> None:
    router = HookRouter(
        [
            binding(Script("late", []), priority=10, name="b"),
            binding(Script("early", []), priority=0, name="a"),
        ]
    )

    assert [item.name for item in router.bindings_for(HookName.BEFORE_MODEL_REQUEST)] == ["a", "b"]
    assert router.bindings_for(HookName.TURN_END) == ()


# ------------------------------------------------------ B 累积与短路


async def test_replace_accumulates_and_the_last_one_wins() -> None:
    seen: list[str] = []

    class Recorder:
        def __init__(self, label: str, text: str) -> None:
            self.label = label
            self.text = text

        async def handle(self, context: HookContext) -> HookOutcome:
            assert context.request is not None
            seen.append(context.request.messages[0].content)
            return replace_request(self.text)

    router = HookRouter(
        [
            binding(Recorder("first", "改过一次"), priority=0, name="a"),
            binding(Recorder("second", "改过两次"), priority=1, name="b"),
        ]
    )

    outcome = await router.dispatch(request_context("原文"))

    # 第二个 handler 看到的是第一个改写后的请求：累积式，不是各改各的。
    assert seen == ["原文", "改过一次"]
    assert outcome.action is HookAction.REPLACE
    assert outcome.request is not None
    assert outcome.request.messages[0].content == "改过两次"


async def test_block_short_circuits_the_rest() -> None:
    trace: list[str] = []
    router = HookRouter(
        [
            binding(
                Script("blocker", trace, outcome=HookOutcome(HookAction.BLOCK, reason="不许")),
                hook=HookName.BEFORE_TOOL_CALL,
                priority=0,
                name="a",
            ),
            binding(Script("never", trace), hook=HookName.BEFORE_TOOL_CALL, priority=1, name="b"),
        ]
    )

    from karyvia.contracts import ToolCall, ToolInvocation

    outcome = await router.dispatch(
        HookContext(
            HookName.BEFORE_TOOL_CALL,
            correlation=CORRELATION,
            invocation=ToolInvocation(
                call=ToolCall(call_id="c1", name="fs.read"),
                correlation=CORRELATION,
                timeout_ms=1000,
            ),
        )
    )

    assert outcome.action is HookAction.BLOCK
    assert trace == ["blocker"]


async def test_reject_on_turn_start_short_circuits() -> None:
    trace: list[str] = []
    from ._orchestrator_support import inbound

    router = HookRouter(
        [
            binding(
                Script("rejecter", trace, outcome=HookOutcome(HookAction.REJECT, reason="维护中")),
                hook=HookName.TURN_START,
                priority=0,
                name="a",
            ),
            binding(Script("never", trace), hook=HookName.TURN_START, priority=1, name="b"),
        ]
    )

    outcome = await router.dispatch(
        HookContext(HookName.TURN_START, correlation=CORRELATION, message=inbound())
    )

    assert outcome.action is HookAction.REJECT
    assert outcome.reason == "维护中"
    assert trace == ["rejecter"]


# ------------------------------------------------------------------ C 隔离


async def test_observer_failures_do_not_affect_the_turn() -> None:
    trace: list[str] = []
    failures: list[KaryviaError] = []
    router = HookRouter(
        [
            binding(
                Script("boom", trace, error=RuntimeError("炸了")),
                hook=HookName.TURN_END,
                name="a",
            ),
            binding(Script("fine", trace), hook=HookName.TURN_END, name="b"),
        ],
        on_failure=failures.append,
    )

    from karyvia.contracts import TurnOutcome, TurnStatus

    outcome = await router.dispatch(
        HookContext(
            HookName.TURN_END,
            correlation=CORRELATION,
            outcome=TurnOutcome(
                correlation=CORRELATION,
                status=TurnStatus.COMPLETED,
                started_at=_now(),
                finished_at=_now(),
            ),
        )
    )

    assert outcome.action is HookAction.CONTINUE
    assert sorted(trace) == ["boom", "fine"]  # 并发，顺序不保证
    assert [error.code for error in failures] == [ErrorCode.PLUGIN_HOOK_FAILED]


async def test_observer_timeout_is_reported_and_the_task_is_cancelled() -> None:
    failures: list[KaryviaError] = []
    trace: list[str] = []
    router = HookRouter(
        [
            binding(
                Script("slow", trace, hang=True),
                hook=HookName.AFTER_MODEL_RESPONSE,
                name="a",
            )
        ],
        observer_timeout_ms=10,
        on_failure=failures.append,
    )

    from karyvia.contracts import ModelResponse, StopReason

    outcome = await router.dispatch(
        HookContext(
            HookName.AFTER_MODEL_RESPONSE,
            correlation=CORRELATION,
            response=ModelResponse(
                model_id="fake-model", stop_reason=StopReason.END_TURN, content="x"
            ),
        )
    )

    assert outcome.action is HookAction.CONTINUE
    assert [error.code for error in failures] == [ErrorCode.TIMEOUT_HOOK]


async def test_interceptor_failure_is_skipped_and_the_rest_continue() -> None:
    trace: list[str] = []
    failures: list[KaryviaError] = []
    router = HookRouter(
        [
            binding(Script("boom", trace, error=RuntimeError("炸")), priority=0, name="a"),
            binding(Script("after", trace, outcome=replace_request("仍然生效")), priority=1,
                    name="b"),
        ],
        on_failure=failures.append,
    )

    outcome = await router.dispatch(request_context())

    assert trace == ["boom", "after"]
    assert outcome.action is HookAction.REPLACE
    assert len(failures) == 1


async def test_interceptor_timeout_is_isolated_per_handler() -> None:
    failures: list[KaryviaError] = []
    trace: list[str] = []
    router = HookRouter(
        [
            binding(Script("slow", trace, hang=True), priority=0, name="a"),
            binding(Script("fast", trace), priority=1, name="b"),
        ],
        interceptor_timeout_ms=10,
        on_failure=failures.append,
    )

    outcome = await router.dispatch(request_context())

    assert trace == ["slow", "fast"]
    assert outcome.action is HookAction.CONTINUE
    assert [error.code for error in failures] == [ErrorCode.TIMEOUT_HOOK]


async def test_failure_detail_never_carries_the_exception_message() -> None:
    failures: list[KaryviaError] = []
    router = HookRouter(
        [binding(Script("boom", [], error=RuntimeError("token=sk-live-should-not-leak")))],
        on_failure=failures.append,
    )

    await router.dispatch(request_context())

    rendered = repr(failures[0]) + failures[0].user_message + str(failures[0].detail)
    assert "sk-live-should-not-leak" not in rendered


async def test_observer_failure_does_not_raise() -> None:
    """观察者失败只上报，不改变 turn 结果（`NFR-204`）。"""
    from karyvia.contracts import TurnOutcome, TurnStatus

    router = HookRouter(
        [
            binding(
                Script("boom", [], error=RuntimeError("炸")),
                hook=HookName.TURN_END,
            )
        ]
    )

    outcome = await router.dispatch(
        HookContext(
            HookName.TURN_END,
            correlation=CORRELATION,
            outcome=TurnOutcome(
                correlation=CORRELATION,
                status=TurnStatus.COMPLETED,
                started_at=_now(),
                finished_at=_now(),
            ),
        )
    )
    assert outcome.action is HookAction.CONTINUE


async def test_handler_raised_karyvia_error_passes_through_unchanged() -> None:
    original = KaryviaError(ErrorCode.PERMISSION_DENIED, "不许")
    failures: list[KaryviaError] = []
    router = HookRouter(
        [binding(Script("boom", [], error=original))], on_failure=failures.append
    )

    await router.dispatch(request_context())

    assert failures[0] is original


# ------------------------------------------------------------------ E 形状


async def test_wrong_action_for_this_hook_is_a_plugin_failure() -> None:
    failures: list[KaryviaError] = []
    router = HookRouter(
        [binding(Script("bad", [], outcome=HookOutcome(HookAction.REJECT, reason="拒")))],
        on_failure=failures.append,
    )

    outcome = await router.dispatch(request_context())

    assert outcome.action is HookAction.CONTINUE
    assert [error.code for error in failures] == [ErrorCode.PLUGIN_HOOK_FAILED]


async def test_replace_with_the_wrong_payload_is_a_plugin_failure() -> None:
    from ._orchestrator_support import fragment

    failures: list[KaryviaError] = []
    router = HookRouter(
        [
            binding(
                Script("bad", [], outcome=HookOutcome(HookAction.REPLACE, fragments=(fragment(),)))
            )
        ],
        on_failure=failures.append,
    )

    outcome = await router.dispatch(request_context())

    assert outcome.action is HookAction.CONTINUE
    assert [error.code for error in failures] == [ErrorCode.PLUGIN_HOOK_FAILED]


async def test_returning_none_is_a_continue() -> None:
    router = HookRouter([binding(Script("quiet", [], outcome=None))])
    assert (await router.dispatch(request_context())).action is HookAction.CONTINUE


# ------------------------------------------------------------------ F 注册


def test_bindings_from_reads_registered_hooks() -> None:
    registry = CapabilityRegistry()
    handler = Script("h", [])
    with registry.batch(Builtin()) as batch:
        batch.add(
            CapabilityKind.HOOK,
            "audit",
            RegisteredHook(hook=HookName.TURN_END, handler=handler),  # type: ignore[arg-type]
        )
    registry.freeze(registry.registrations)

    bindings = bindings_from(registry)

    assert [(item.hook, item.name, item.priority) for item in bindings] == [
        (HookName.TURN_END, "audit", 0)
    ]


def test_bindings_from_rejects_a_foreign_payload() -> None:
    registry = CapabilityRegistry()
    with registry.batch(Builtin()) as batch:
        batch.add(CapabilityKind.HOOK, "audit", object())
    registry.freeze(registry.registrations)

    try:
        bindings_from(registry)
    except KaryviaError as error:
        assert error.code is ErrorCode.KERNEL_INVARIANT_VIOLATED
    else:  # pragma: no cover - 失败路径
        raise AssertionError("载荷形状不对必须当场报错")
