"""启动期资源所有权：在实例构造成功前负责回滚插件副作用与收尾函数。

职责：记录装配过程中已经创建的 ``RuntimePluginContext`` 和 closer；一次装配尝试失败时
按逆序、有预算地停止插件上下文，再关闭 sink 等普通资源；成功时把所有权一次性交给
``AgentInstance``。
不负责：选择插件、推进正式生命周期、释放实例锁或决定原始启动错误；这些仍由装配根负责。

能力注册的 ``RegistrationBatch`` 只能回滚 Registry。插件的 ``setup()`` 还可以订阅事件或
通过 ``ctx.spawn_task()`` 创建任务，因此启动本身也需要一层资源事务。两层事务处理的是不同
对象，不能互相替代。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from karyvia.contracts import ErrorCode, KaryviaError
from karyvia.kernel.plugins import StopOutcome, StopUnit, stop_plugins

from .plugin_context import RuntimePluginContext

__all__ = ["Closer", "StartupResources"]


Closer = Callable[[], Awaitable[None]]


@dataclass(slots=True)
class StartupResources:
    """尚未转交给实例的启动资源。

    ``plugin_checkpoint()`` 允许 CLI 回落只撤销本轮创建的 PluginContext，而保留 EventBus
    和 sink 供下一轮装配继续使用。最终的 ``transfer()`` 是所有权边界：调用后本对象变空，
    之后的停止责任完全属于 ``AgentInstance``。
    """

    contexts: list[RuntimePluginContext] = field(default_factory=list)
    closers: list[Closer] = field(default_factory=list)
    _transferred: bool = field(default=False, init=False)

    def add_context(self, context: RuntimePluginContext) -> None:
        self._ensure_owned()
        self.contexts.append(context)

    def add_closer(self, closer: Closer) -> None:
        self._ensure_owned()
        self.closers.append(closer)

    def plugin_checkpoint(self) -> int:
        """返回当前上下文数量，供一次可回滚的插件装配记住起点。"""
        self._ensure_owned()
        return len(self.contexts)

    async def rollback_plugins(
        self, checkpoint: int, *, timeout_ms: int
    ) -> tuple[StopOutcome, ...]:
        """逆序停止 checkpoint 之后创建的插件上下文，并从本事务摘除。"""
        self._ensure_owned()
        if checkpoint < 0 or checkpoint > len(self.contexts):
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "启动资源的插件回滚点无效。",
                detail={"checkpoint": checkpoint, "contexts": len(self.contexts)},
            )
        discarded = self.contexts[checkpoint:]
        del self.contexts[checkpoint:]
        units = tuple(
            StopUnit(plugin_id=context.plugin_id, stop=context.shutdown)
            for context in reversed(discarded)
        )
        return await stop_plugins(units, timeout_ms=timeout_ms)

    async def rollback(self, *, timeout_ms: int) -> tuple[StopOutcome, ...]:
        """撤销仍归启动期所有的一切；普通 closer 的失败不得覆盖启动根因。"""
        outcomes = await self.rollback_plugins(0, timeout_ms=timeout_ms)
        closers = tuple(reversed(self.closers))
        self.closers.clear()
        for closer in closers:
            try:
                await closer()
            except Exception:
                # 启动路径已经有一个应向调用方报告的根因。closer 没有统一错误协议，
                # 在这里替换原异常只会让真正的失败位置丢失。
                continue
        return outcomes

    def transfer(self) -> tuple[tuple[RuntimePluginContext, ...], tuple[Closer, ...]]:
        """把资源所有权交给实例；每个启动事务只能成功转交一次。"""
        self._ensure_owned()
        contexts = tuple(self.contexts)
        closers = tuple(self.closers)
        self.contexts.clear()
        self.closers.clear()
        self._transferred = True
        return contexts, closers

    def _ensure_owned(self) -> None:
        if self._transferred:
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "启动资源已经转交给实例，不能再次修改或回滚。",
            )
