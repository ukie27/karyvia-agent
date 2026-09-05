"""插件生命周期：状态机、停止顺序与停止超时（技术方案 §7.4；`NFR-201`、`PLG-005`、`EDG-104`）。

职责：定义插件的六个生命周期阶段与**唯一**一张合法转换表、把阶段投影成诊断用的
`PluginState`、把一批停止动作按给定顺序逐个跑完并为每个动作施加独立超时。
不负责：决定加载顺序（`loader.plan_load_order()` 的 `LoadPlan.order`，停止顺序取它的
逆序）、决定「停一个插件」具体要做哪些事（取消任务、退订事件在
`runtime/plugin_context.py`）、发布事件（bus 在装配根手里）、决定谁被禁用（配置，
且首版不热更新）。

**停止顺序不在这里算**（`PLG-005`）：`stop_order()` 只是把 `LoadPlan.order` 翻过来。
再写一遍拓扑排序会让「被依赖者后停」与「被依赖者先起」各有一份实现，而它们必须是同一
个序的两面——一份坏了，另一份的测试不会响。

**超时后是放弃等待而不是等到它结束**（`EDG-104`）：一个吞掉 `CancelledError` 的插件任务
可以永远不返回，而实例退出不能被它扣住。放弃意味着那个协程可能仍在跑——`StopOutcome`
如实标着 `timed_out`，`TIMEOUT_PLUGIN_STOP` 因此是一条独立的错误码而不是复用加载失败。

**`PluginState` 是显示口径，`PluginPhase` 是判定口径**：阶段只有这一份，诊断所需的
粗粒度状态由 `PHASE_STATES` 投影出来，
不是另一套并行的枚举。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from karyvia.contracts import ErrorCode, KaryviaError
from karyvia.kernel.observability import PluginState

__all__ = [
    "DEFAULT_STOP_TIMEOUT_MS",
    "PHASE_STATES",
    "PHASE_TRANSITIONS",
    "PluginLifecycle",
    "PluginPhase",
    "StopAction",
    "StopOutcome",
    "StopUnit",
    "stop_order",
    "stop_plugins",
    "units_for",
]

#: 单个插件的停止预算（技术方案 §7.4 的 `plugin_stop_timeout_ms`）。
#: **与 `kernel/config/schema.py` 的 `DEFAULT_PLUGIN_STOP_TIMEOUT_MS` 必须相等**，
#: 由 `test_plugin_stop_timeout_default_matches_the_config_schema` 盯着。两处各写一份的
#: 理由与 turn 的六项预算相同：`kernel.config` 不能 import 本包（会把 registry、routing、
#: turn 与 asyncio 一起拖上 `karyvia config show` 的路径，`NFR-405` 的冷启动预算）。
DEFAULT_STOP_TIMEOUT_MS: Final = 5_000


class PluginPhase(StrEnum):
    """插件生命周期的阶段（技术方案 §7.4 的状态机）。

    与 `PluginState` 的分工见模块 docstring：这里是判定口径，那里是显示口径。
    `DISABLED` 不在这里——它是配置结论而不是阶段，一个被禁用的插件根本不会进入生命周期。
    """

    #: 候选被发现、manifest 已读。
    DISCOVERED = "discovered"
    #: 阶段 A 全部通过：SDK 兼容、依赖可解、配置合 schema、状态版本一致。
    VALIDATED = "validated"
    #: `setup()` 跑完且整批注册已提交。
    LOADED = "loaded"
    #: 实例已就绪，插件的后台任务与事件订阅在跑。
    STARTED = "started"
    #: 停止动作已开始、尚未确认结束。
    STOPPING = "stopping"
    #: 停止动作干净结束。终态。
    STOPPED = "stopped"
    #: 任意阶段失败。**失败发生在哪个阶段记在 `PluginLifecycle.failed_phase`**，
    #: 因为「发现时就坏了」与「停的时候超时了」的补救动作毫无共同之处。
    FAILED = "failed"


#: 合法转换的**唯一**一张表。非法转换是 `KERNEL_INVARIANT_VIOLATED` 而不是被静默接受：
#: 一个从 `STOPPED` 回到 `STARTED` 的插件意味着有人在停止流程之后又启动了它，那种错误
#: 只会在很远的地方以「事件订阅明明退订了却还在收」的形式暴露出来。
#:
#: 三条不那么显然的边：
#:
#: - `LOADED -> STOPPING`：装配失败时已经 `setup()` 过的插件仍然要被清理，它没经过
#:   `STARTED`（实例从未就绪）。
#: - `FAILED -> STOPPING`：`setup()` 中途失败的插件**注册被回滚了、副作用没有**——它可能
#:   已经 `spawn_task()` 或订阅过事件（`setup` 跑在事件循环里）。不给它一条清理路径，
#:   那些任务就会活过实例本身。
#: - `STOPPING -> FAILED`：停止超时（`EDG-104`）。它不是 `STOPPED`——那会声称清理干净了。
PHASE_TRANSITIONS: Final[Mapping[PluginPhase, frozenset[PluginPhase]]] = {
    PluginPhase.DISCOVERED: frozenset({PluginPhase.VALIDATED, PluginPhase.FAILED}),
    PluginPhase.VALIDATED: frozenset({PluginPhase.LOADED, PluginPhase.FAILED}),
    PluginPhase.LOADED: frozenset(
        {PluginPhase.STARTED, PluginPhase.STOPPING, PluginPhase.FAILED}
    ),
    PluginPhase.STARTED: frozenset({PluginPhase.STOPPING, PluginPhase.FAILED}),
    PluginPhase.STOPPING: frozenset({PluginPhase.STOPPED, PluginPhase.FAILED}),
    PluginPhase.STOPPED: frozenset(),
    PluginPhase.FAILED: frozenset({PluginPhase.STOPPING}),
}

#: 阶段 → 诊断状态的投影。`VALIDATED` 与 `DISCOVERED` 同投影成 `discovered`：
#: `PluginState` 刻意比阶段粗，「校验过了但还没 setup」对用户不构成一个可行动的区别
#: （两者的失败都在启动报告里带着阶段名）。`STOPPING` 投影成 `activated` 而不是
#: `deactivated`——停到一半的插件还没停下，说它已停用就是在报一个尚未成立的结论。
PHASE_STATES: Final[Mapping[PluginPhase, PluginState]] = {
    PluginPhase.DISCOVERED: PluginState.DISCOVERED,
    PluginPhase.VALIDATED: PluginState.DISCOVERED,
    PluginPhase.LOADED: PluginState.LOADED,
    PluginPhase.STARTED: PluginState.ACTIVATED,
    PluginPhase.STOPPING: PluginState.ACTIVATED,
    PluginPhase.STOPPED: PluginState.DEACTIVATED,
    PluginPhase.FAILED: PluginState.FAILED,
}


@dataclass(slots=True)
class PluginLifecycle:
    """一个插件的阶段与失败记录。**全项目唯一可变的插件状态**。

    刻意做成可变对象而不是每次转换产出一个新值：调用方（装配根、`stop_plugins()`）分布
    在几个模块里，让它们各自持有并替换一份不可变快照，等于把「当前阶段是哪个」变成一个
    要靠约定同步的问题。
    """

    plugin_id: str
    dependencies: tuple[str, ...] = ()
    phase: PluginPhase = PluginPhase.DISCOVERED
    #: 失败发生在哪个阶段（`phase is FAILED` 时非空）。
    failed_phase: PluginPhase | None = None
    error: KaryviaError | None = None

    def advance(self, phase: PluginPhase) -> None:
        """推进到下一个阶段。

        **异常约定**：非法转换抛 `KERNEL_INVARIANT_VIOLATED`（见 `PHASE_TRANSITIONS`）。
        推进到当前阶段同样非法——那通常意味着有人把一段停止流程跑了两遍。
        """
        if phase not in PHASE_TRANSITIONS[self.phase]:
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "插件生命周期出现非法状态转换。",
                detail={
                    "plugin_id": self.plugin_id,
                    "from": self.phase.value,
                    "to": phase.value,
                    "allowed": sorted(item.value for item in PHASE_TRANSITIONS[self.phase]),
                },
            )
        self.phase = phase

    def fail(self, error: KaryviaError) -> None:
        """记一次失败：阶段变 `FAILED`，**失败发生的阶段与原因一并留下**。

        **异常约定**：从终态（`STOPPED`）失败抛 `KERNEL_INVARIANT_VIOLATED`，与
        `advance()` 同一张表。已经 `FAILED` 的插件再失败一次也是非法的——第二条错误会
        盖掉第一条，而第一条才是根因。
        """
        failed_at = self.phase
        self.advance(PluginPhase.FAILED)
        self.failed_phase = failed_at
        self.error = error

    @property
    def state(self) -> PluginState:
        """诊断口径的状态，供 `karyvia plugins` 与会话内 `/plugins` 使用。"""
        return PHASE_STATES[self.phase]

    @property
    def terminal(self) -> bool:
        """是否再也不会转换。`FAILED` **不是**终态：它还欠一次清理。"""
        return not PHASE_TRANSITIONS[self.phase]


#: 「停这一个插件」要跑的那件事。约定：它自己 `cancel()` 该插件的任务与订阅，然后等它们
#: 回收；超时的判定由 `stop_plugins()` 做，因此这个协程**不需要**自带超时。
StopAction = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class StopUnit:
    """一个插件的停止单元。

    `lifecycle` 可选：`embed/` 与测试里停一个孤立的 ctx 时没有阶段可推进，而为它编一个
    只为满足签名的生命周期对象只会让「阶段现在是什么」多一个假答案。
    """

    plugin_id: str
    stop: StopAction
    lifecycle: PluginLifecycle | None = None


@dataclass(frozen=True, slots=True)
class StopOutcome:
    """一个插件停止的结果。

    `timed_out` 与 `error` 分开：超时之外还有「停止动作自己抛了异常」，两者都记 `error`，
    但只有前者意味着**那段协程可能还在跑**——诊断要能只扫一遍就回答「这次退出干净吗」。
    """

    plugin_id: str
    timed_out: bool = False
    error: KaryviaError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def stop_order(order: Sequence[str]) -> tuple[str, ...]:
    """停止顺序 = 启动拓扑序的逆序（`PLG-005`）。

    参数就是 `LoadPlan.order`。这个函数只把“逆序”写在一处；`plan_load_order()` 是加载
    顺序的唯一来源，停止侧不重算拓扑（见模块 docstring）。
    """
    return tuple(reversed(tuple(order)))


async def stop_plugins(
    units: Sequence[StopUnit],
    *,
    timeout_ms: int = DEFAULT_STOP_TIMEOUT_MS,
) -> tuple[StopOutcome, ...]:
    """**按给定顺序**逐个停止，每个各有独立超时（`EDG-104`）。

    调用方交进来的顺序就是执行顺序——用 `stop_order(plan.order)` 得到它。逐个而不是并发：
    依赖方必须先于被依赖方停下，而并发停止会让「B 还在用 A 的能力时 A 已经停了」重新
    变成可能。

    超时的处置是**放弃等待**：取消那个任务、记一条 `TIMEOUT_PLUGIN_STOP`、继续停下一个。
    被放弃的任务不再被 await（否则「不阻塞进程退出」就是句空话），但挂一个吞掉结果的
    回调——否则一个失败的孤儿任务会在事件循环关闭时刷一条 "never retrieved" 警告，
    把真正的诊断淹掉。

    **异常约定**：不抛。停止是收尾路径，一个插件的失败不该盖住其余插件的清理
    （`NFR-204`）；一切都折进 `StopOutcome`。`BaseException` 除外——取消整个停止流程
    是调用方的权利。
    """
    outcomes: list[StopOutcome] = []
    for unit in units:
        outcomes.append(await _stop_one(unit, timeout_ms=timeout_ms))
    return tuple(outcomes)


async def _stop_one(unit: StopUnit, *, timeout_ms: int) -> StopOutcome:
    """停一个插件并推进它的阶段。"""
    lifecycle = unit.lifecycle
    if lifecycle is not None and lifecycle.phase is not PluginPhase.STOPPING:
        # 已经是终态（`STOPPED`）的插件不再停第二次；其余阶段一律先进 `STOPPING`。
        if lifecycle.terminal:
            return StopOutcome(plugin_id=unit.plugin_id)
        lifecycle.advance(PluginPhase.STOPPING)

    task = asyncio.ensure_future(_as_coroutine(unit.stop()))
    done, _ = await asyncio.wait({task}, timeout=timeout_ms / 1000)
    if not done:
        task.cancel()
        # 不 await：等下去正是 `EDG-104` 要避免的那件事。
        task.add_done_callback(_swallow)
        return _failed(unit, _timeout_error(unit.plugin_id, timeout_ms), timed_out=True)

    error = _error_of(task, plugin_id=unit.plugin_id)
    if error is not None:
        return _failed(unit, error)
    if lifecycle is not None:
        lifecycle.advance(PluginPhase.STOPPED)
    return StopOutcome(plugin_id=unit.plugin_id)


def _error_of(task: "asyncio.Future[None]", *, plugin_id: str) -> KaryviaError | None:
    """取出停止动作的异常。**只放类型名不放异常消息**，第三方异常文本可能带凭据。

    `CancelledError` 走 `BaseException` 通道原样冒泡：整个停止流程被
    取消时，把它记成「这个插件停不下来」是在报一个假结论。"""
    if task.cancelled():
        raise asyncio.CancelledError
    exc = task.exception()
    if exc is None:
        return None
    if isinstance(exc, KaryviaError):
        return exc
    if not isinstance(exc, Exception):
        raise exc
    return KaryviaError(
        ErrorCode.PLUGIN_LOAD_FAILED,
        "插件的停止动作抛出了异常；它的清理可能不完整。",
        detail={"plugin_id": plugin_id, "exception": type(exc).__name__},
    )


def _timeout_error(plugin_id: str, timeout_ms: int) -> KaryviaError:
    return KaryviaError(
        ErrorCode.TIMEOUT_PLUGIN_STOP,
        "插件未在停止预算内结束；已放弃等待，它的后台任务可能仍在运行。",
        detail={
            "plugin_id": plugin_id,
            "timeout_ms": timeout_ms,
            "suggestion": "检查该插件 spawn_task 的协程是否吞掉了 CancelledError；"
            "预算见配置 plugins.stop_timeout_ms。",
        },
    )


def _failed(unit: StopUnit, error: KaryviaError, *, timed_out: bool = False) -> StopOutcome:
    if unit.lifecycle is not None:
        unit.lifecycle.fail(error)
    return StopOutcome(plugin_id=unit.plugin_id, timed_out=timed_out, error=error)


def _swallow(task: "asyncio.Future[None]") -> None:
    """读掉被放弃任务的结果，免得事件循环在关闭时刷 "exception was never retrieved"。"""
    if not task.cancelled():
        task.exception()


async def _as_coroutine(awaitable: Awaitable[None]) -> None:
    """`StopAction` 返回的是 `Awaitable`，而 `ensure_future` 对非协程 awaitable 的处理
    因类型而异——包一层，`asyncio.wait()` 因此永远拿到一个真正的 Task。"""
    await awaitable


#: 留给装配根的便利构造：按 id 取 ctx 的停止动作，缺席的 id 直接跳过。
#: 放在这里而不是 `runtime/`，是因为「顺序由 id 决定、动作由调用方给」这条组合规则属于
#: 停止机制本身；`runtime/instance.py` 只交出一张 `{id: 动作}` 表。
def units_for(
    order: Sequence[str],
    actions: Mapping[str, StopAction],
    lifecycles: Mapping[str, PluginLifecycle] | None = None,
) -> tuple[StopUnit, ...]:
    """把 `LoadPlan.order` + 一张动作表拼成**已经逆序好**的停止单元序列。"""
    known = lifecycles or {}
    return tuple(
        StopUnit(plugin_id=plugin_id, stop=actions[plugin_id], lifecycle=known.get(plugin_id))
        for plugin_id in stop_order(order)
        if plugin_id in actions
    )
