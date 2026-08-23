"""静态清单 bootstrap：把一批 `LoadRequest` 跑成注册（技术方案 §7.3 阶段 B）。

职责：按顺序对每个请求开批次、调用方给的 Host 跑 `setup`、核对声明、提交或回滚；
依赖在 setup 阶段失败时跳过其依赖者，并如实报告每个提供方的结果。
不负责：决定加载哪些提供方、依赖拓扑排序、解析覆盖、构造 Host 与 `PluginContext`；
这些由发现/规划机制和 Runtime 组装根负责。

**本模块读不到 `BUILTIN_MANIFESTS`，这是刻意的**：`R2` 只允许 `kernel/` import
`contracts` 与 `kernel`，
而 `PluginManifest` 在 `sdk/`、`BUILTIN_MANIFESTS` 在 `builtins/`，两个都够不着。于是本
模块做成一个**对 `LoadRequest` 泛化的 setup 运行器**，manifest 的翻译留给
`runtime/wiring.py`。因此内建和外部插件共用同一个 setup 运行器，而不是各维护一份注册实现。

**零请求是一等路径**（`PLG-007`、`EDG-101`）：未启用可选插件时仍应正常装配。因此
`load_into(registry, ())` 返回空元组
并让 registry 保持可写可空，它有自己的测试，不是一条退化分支。

**`critical` 决定失败的后果**（`PLG-004`、`EDG-106`）：关键提供方失败直接抛，非关键的
记进 `LoadOutcome.error` 由调用方折进 `ResolutionReport.failures`，实例继续启动。依赖失败
同样是一种加载失败：只跳过它的传递依赖者，不妨碍无关插件启动。
两种情况下批次都已回滚，registry 不留半注册状态（`EDG-103`）。
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, TypeAlias

from nucleamind.contracts import CapabilityRef, ErrorCode, NucleaError, ProviderId
from nucleamind.kernel.registry import CapabilityRegistry, RegistrationBatch

from .declarations import LoadRequest

__all__ = ["LoadOutcome", "RegistrationHost", "SetupFn", "import_setup", "load_into"]

#: `setup(api)` 的形状。同步与异步都接受——插件作者不该为了一个纯注册函数被迫写 `async`。
#: 形参标成 `object` 而不是 `NucleaAPI`：`R2` 禁止 kernel import `sdk`，而这里只需要
#: 「能被调用」这一个事实，真正的类型由插件侧的签名负责。
SetupFn: TypeAlias = Callable[[object], object | Awaitable[object]]

#: `"pkg.module:function"` 的分隔符，与 `sdk/manifest.py` 的 `setup` 字段格式一致。
_SETUP_SEPARATOR = ":"


class RegistrationHost(Protocol):
    """loader 对 Host 的**全部**要求：能收尾。

    只声明 `finish()` 而不是引用 `CapabilityHost`：Host 由调用方构造并传进来
    （见 `load_into` 的 `host_for`），loader 只负责在 `setup` 返回后调一次收尾、再提交。
    """

    def finish(self) -> None:
        """核对声明与实际注册一致。**异常约定**：不一致抛 `PLUGIN_LOAD_FAILED`。"""
        ...


@dataclass(frozen=True, slots=True)
class LoadOutcome:
    """一个提供方的加载结果。

    `error` 非空即表示这一批被整体丢弃了——`registered` 此时必为空。两个字段都留着而不是
    塞进一个 union，是因为诊断要同时回答「谁失败了」和「它本来打算提供什么」。
    """

    provider: ProviderId
    registered: tuple[CapabilityRef, ...] = ()
    error: NucleaError | None = None

    @property
    def ok(self) -> bool:
        """是否加载成功。"""
        return self.error is None


def import_setup(target: str) -> SetupFn:
    """按 `"pkg.module:function"` 解析出 `setup` 函数。

    **异常约定**：格式非法、模块导不进、属性不存在或不可调用，一律抛
    `PLUGIN_LOAD_FAILED` 并在 `detail["setup"]` 里带上原串。不让 `ImportError` 直接冒泡：
    调用方要能把「这个插件坏了」与「Kernel 自己坏了」分开。
    """
    module_name, separator, attribute = target.partition(_SETUP_SEPARATOR)
    if not separator or not module_name or not attribute:
        raise NucleaError(
            ErrorCode.PLUGIN_LOAD_FAILED,
            "setup 入口必须写成 pkg.module:function。",
            detail={"setup": target},
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise NucleaError(
            ErrorCode.PLUGIN_LOAD_FAILED,
            "无法导入 setup 所在模块。",
            detail={"setup": target, "module": module_name},
        ) from exc
    found = getattr(module, attribute, None)
    if not callable(found):
        raise NucleaError(
            ErrorCode.PLUGIN_LOAD_FAILED,
            "setup 入口不存在或不可调用。",
            detail={"setup": target, "attribute": attribute},
        )
    # `getattr` 交回的东西没有静态类型，`callable()` 是这里唯一能做的运行时检查
    # （`AGENTS.md` 原则 6：cast 必须有运行时检查支撑）。
    return found  # pyright: ignore[reportUnknownVariableType, reportReturnType]


async def _run_one(
    batch: RegistrationBatch,
    request: LoadRequest,
    host: RegistrationHost,
    resolve_setup: Callable[[str], SetupFn],
) -> LoadOutcome:
    """跑一个提供方：setup → 核对 → 提交。失败即回滚并把错误折进结果。"""
    try:
        setup = resolve_setup(request.setup)
        outcome = setup(host)
        if inspect.isawaitable(outcome):
            await outcome
        host.finish()
    except NucleaError as error:
        batch.rollback()
        return LoadOutcome(provider=request.provider, error=error)
    except Exception as exc:
        # 第三方 `setup` 抛什么都可能。折成 `PLUGIN_LOAD_FAILED` 时**只放类型名不放消息**：
        # 异常文本可能带着凭据。
        # 只捕 `Exception`：`BaseException`（取消、Ctrl-C）要放行。
        batch.rollback()
        return LoadOutcome(
            provider=request.provider,
            error=NucleaError(
                ErrorCode.PLUGIN_LOAD_FAILED,
                "setup 执行失败。",
                detail={"provider": str(request.provider), "exception": type(exc).__name__},
            ),
        )
    registered = tuple(item.ref for item in batch.staged)
    batch.commit()
    return LoadOutcome(provider=request.provider, registered=registered)


async def load_into(
    registry: CapabilityRegistry,
    requests: Sequence[LoadRequest],
    *,
    host_for: Callable[[RegistrationBatch, LoadRequest], RegistrationHost],
    resolve_setup: Callable[[str], SetupFn] = import_setup,
) -> tuple[LoadOutcome, ...]:
    """按给定顺序加载全部请求，返回逐个提供方的结果。

    `resolve_setup` 是可注入的（默认 `import_setup`）：测试可以提供假解析器，不触碰真实
    导入系统或插件模块。

    **`host_for` 由调用方提供而不是在这里 `CapabilityHost(...)`**：Host 要拿一个
    `PluginContext`，而 `R2` 禁止 `kernel/` 认识那个类型。把构造交给
    `runtime/wiring.py`，「Host 满足 `NucleaAPI`」那句类型标注就落在真实装配路径上，
    而不是一个只为证明而存在的函数里。

    **异常约定**：`critical=True` 的提供方失败时原样抛出（启动失败）；非关键提供方的失败
    记进对应的 `LoadOutcome.error`。依赖已失败的请求不构造 Host、不执行 setup，并以
    `PLUGIN_LOAD_FAILED` 记录依赖链；无关请求继续。实际执行失败的批次均已回滚。
    """
    outcomes: list[LoadOutcome] = []
    failed: set[str] = set()
    for request in requests:
        blocked_by = tuple(
            dependency for dependency in request.dependencies if dependency in failed
        )
        if blocked_by:
            outcome = LoadOutcome(
                provider=request.provider,
                error=NucleaError(
                    ErrorCode.PLUGIN_LOAD_FAILED,
                    "插件依赖未能完成加载，因此未执行 setup。",
                    detail={"plugin_id": request.plugin_id, "failed_dependencies": blocked_by},
                ),
            )
        else:
            batch = registry.batch(request.provider)
            outcome = await _run_one(batch, request, host_for(batch, request), resolve_setup)
        if outcome.error is not None and request.critical:
            raise outcome.error
        if outcome.error is not None:
            failed.add(request.plugin_id)
        outcomes.append(outcome)
    return tuple(outcomes)
