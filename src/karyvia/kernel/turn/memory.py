"""长期记忆的召回路径：把一条 `MEMORY` 能力接到上下文组装上。

职责：从 registry 里挑出生效的 `MemoryProvider`、每轮 turn 用本次输入去召回、把结果变成
可以进 `extra_fragments` 的 `ContextFragment` 序列，并按配置在故障时降级或失败。
不负责：存储（`MemoryProvider` 的实现方）、决定片段怎么进模型消息（`context_builder.py`）、
选哪个 Provider（那是配置，装配根读它）——本模块不做任何 IO，只 await 注入进来的 Provider。

**它存在的理由**： 交了 `MEMORY` 能力与一个实现它的插件，但 kernel 里没有消费者
（`memory_providers_from()` 除测试外无调用方），因此**只注册一条 `MEMORY` 能力、记忆永远
进不了模型**。本模块把该能力接入 Turn：第三方可以只写一条
`MEMORY` 能力，不必再自带一个 Context Provider。

**四条会影响正确性的判定：**

1. **只用 `FragmentScope.AGENT` 召回。** 契约的 `MemoryProvider` 三个方法**一个
   `SessionKey` 都不带**，因此经这条接口根本表达不出「哪个会话的记忆」——`scope=SESSION`
   传下去，实现方只能猜。这不再是「默认这么理解」而是**决定**：`MemoryProvider` 是
   **实例级**长期记忆的接口，会话级与工作区级的记忆归 `ContextProvider`
   （`plugins/karyvia-plugin-memory/` 的四条通路就是那么分的）。要改这个决定得先给三个
   方法加一个 key 参数，而那是 SDK 1.0 之后的破坏性变更（§7.6）。
2. **priority 有下界，这是本模块唯一会改写的字段。** 它保留记忆片段的相对重要性元数据，
   供 `context_assemble` 拦截器与诊断读取，但不参与 Kernel 的 token 预算判断。
   **其余字段一个都不动**，包括 `trust`：
   声明 `SYSTEM` 的记忆会进系统指令位置，那与一个 Context Provider 声明 `SYSTEM` 是同一件
   事、同一份 manifest 担保。（`plugins/karyvia-plugin-memory/` 在**写入**侧就把 trust
   钉死成 `UNTRUSTED`，那是它的判断，不是这里替它做的。）
3. **失败按配置分叉，默认降级**。`degrade` = 记一条错误、这一轮没有记忆、
   turn 照常跑；`fail` = 原样上抛，turn `FAILED`。**降级不等于静默**：错误一定经
   `on_failure` 报出去，否则「记忆一直召不回来」查不出原因。
4. **空查询直接返回空**，不去打扰 Provider：命令类 turn 与刚开的会话都会走到这里，
   拿空串去检索只会拿回一批「碰巧得分最高」的记忆。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Final

from karyvia.contracts import (
    CancelSignal,
    CapabilityKind,
    CapabilityRef,
    ContextFragment,
    Correlation,
    ErrorCategory,
    ErrorCode,
    FragmentScope,
    KaryviaError,
    MemoryProvider,
    ProviderId,
)

__all__ = [
    "DEFAULT_MEMORY_FRAGMENT_PRIORITY",
    "DEFAULT_MEMORY_ON_FAILURE",
    "DEFAULT_MEMORY_RECALL_LIMIT",
    "DEFAULT_MEMORY_RECALL_TIMEOUT_MS",
    "MEMORY_ON_FAILURE_CHOICES",
    "MEMORY_RECALL_SCOPE",
    "MemoryRecall",
    "select_memory",
]

#: 经这条接口召回的唯一范围。见模块 docstring 第 1 条——这是决定，不是默认。
MEMORY_RECALL_SCOPE: Final = FragmentScope.AGENT

#: 每轮最多召回几条。给一个小数：记忆与会话历史抢同一份预算，而默认配置下用户没有表态。
DEFAULT_MEMORY_RECALL_LIMIT: Final = 5

#: 一次召回的预算。与 `DEFAULT_CONTEXT_PROVIDER_TIMEOUT_MS` 取同一个值——它们是同一类
#: 东西（一次可能要走外部服务的上下文贡献），两个不同的数只会让人猜哪个先到。
DEFAULT_MEMORY_RECALL_TIMEOUT_MS: Final = 3_000

#: 召回片段的 priority 下界。取 100 = 插件基准（`sdk/manifest.py` 的默认值）。
DEFAULT_MEMORY_FRAGMENT_PRIORITY: Final = 100

#: 的两种处置。`degrade` 是默认：一个记忆后端挂了不该让实例不能对话。
MEMORY_ON_FAILURE_CHOICES: Final[tuple[str, ...]] = ("degrade", "fail")
DEFAULT_MEMORY_ON_FAILURE: Final = "degrade"


@dataclass(frozen=True, slots=True)
class MemoryRecall:
    """一条已选定的 `MemoryProvider`，加上召回它所需的全部策略。

    做成一个对象而不是给 `OrchestratorDeps` 加五个槽：这五项是同一个决定的五个面，
    分开放会让「没配 provider 却配了 limit」这种半截状态可表达。
    """

    provider: MemoryProvider
    name: str
    owner: ProviderId
    limit: int = DEFAULT_MEMORY_RECALL_LIMIT
    timeout_ms: int = DEFAULT_MEMORY_RECALL_TIMEOUT_MS
    priority_floor: int = DEFAULT_MEMORY_FRAGMENT_PRIORITY
    #: `True` = 故障时上抛。默认降级。
    critical: bool = False

    @property
    def ref(self) -> CapabilityRef:
        return CapabilityRef(kind=CapabilityKind.MEMORY, name=self.name, provider=self.owner)

    async def recall(
        self,
        query: str,
        correlation: Correlation,
        cancel: CancelSignal,
        *,
        on_failure: Callable[[KaryviaError], None] | None = None,
    ) -> tuple[ContextFragment, ...]:
        """召回这一轮的记忆片段。**约定：`critical=False` 时不抛。**

        **异常约定**：`critical=True` 时把故障原样上抛（turn `FAILED`）；否则交给
        `on_failure` 后返回空元组。**不吞**——见模块 docstring 第 3 条。分叉只在
        `_degrade()` 一处判：三条 except 各判一次的话，改策略要记得改三个地方。
        **取消语义**：`cancel` 透传给 Provider；`ErrorCategory.CANCELLED` 类错误
        **不走降级**——它不是记忆后端的故障而是这条 turn 该停了，把它折成「这轮没有记忆」
        会让 turn 带着半份上下文继续跑。判据用**类别**而不是逐个列举错误码：
        `CODE_CATEGORIES` 已经是那份归类的唯一来源（`contracts/errors.py`）。
        """
        del correlation  # 片段不带关联标识：整个 turn 只有一个，复制一份就有两个真相来源。
        if not query.strip():
            return ()
        try:
            recalled = await asyncio.wait_for(
                self.provider.recall(
                    query,
                    scope=MEMORY_RECALL_SCOPE,
                    limit=self.limit,
                    cancel=cancel,
                ),
                timeout=self.timeout_ms / 1000,
            )
        except KaryviaError as error:
            if error.category is ErrorCategory.CANCELLED:
                raise
            return self._degrade(error, on_failure)
        except TimeoutError as error:
            return self._degrade(
                KaryviaError(
                    ErrorCode.TIMEOUT_HOOK,
                    _RECALL_TIMED_OUT,
                    detail={"provider": str(self.owner), "timeout_ms": self.timeout_ms},
                    capability=self.ref,
                ),
                on_failure,
                cause=error,
            )
        except Exception as error:  # noqa: BLE001 - 见 docstring
            return self._degrade(
                KaryviaError(
                    ErrorCode.PLUGIN_HOOK_FAILED,
                    _RECALL_RAISED,
                    # **只放类型名不放异常消息**：第三方后端的异常文本可能带着连接串。
                    detail={"provider": str(self.owner), "exception": type(error).__name__},
                    capability=self.ref,
                ),
                on_failure,
                cause=error,
            )
        return tuple(self._floored(fragment) for fragment in recalled.values())

    def _degrade(
        self,
        error: KaryviaError,
        on_failure: Callable[[KaryviaError], None] | None,
        *,
        cause: BaseException | None = None,
    ) -> tuple[ContextFragment, ...]:
        """ 的两种处置，唯一的判定点：报出去并当作没有记忆，或者上抛。"""
        if self.critical:
            raise error from cause
        if on_failure is not None:
            on_failure(error)
        return ()

    def _floored(self, fragment: ContextFragment) -> ContextFragment:
        """把 priority 抬到下界之上。已经够高的原样返回（`replace` 会造一个新对象）。"""
        if fragment.priority >= self.priority_floor:
            return fragment
        return replace(fragment, priority=self.priority_floor)


def select_memory(
    bindings: Sequence[tuple[str, ProviderId, MemoryProvider]], name: str
) -> tuple[str, ProviderId, MemoryProvider]:
    """按名字挑一条 `MEMORY` 能力。**挑不到是 `CAPABILITY_MISSING`，不是静默不启用。**

    运维在配置里写下一个名字就是要那一个：`memory.provider = "sqlite"` 而实例里只有
    `jsonl` 时，静默退回「没有记忆」会让用户以为记忆在工作。错误里列出实际有哪几条——
    这类配置错误九成是拼错或插件没启用。
    """
    for candidate in bindings:
        if candidate[0] == name:
            return candidate
    raise KaryviaError(
        ErrorCode.CAPABILITY_MISSING,
        _NO_SUCH_MEMORY,
        detail={
            "field": "/memory/provider",
            "requested": name,
            "available": sorted(item[0] for item in bindings),
        },
    )


#: 错误消息定义成模块级常量：`ruff` 的 `TRY003` 不允许在 `raise` 处写多词消息。
_RECALL_TIMED_OUT: Final = "长期记忆召回超时。"
_RECALL_RAISED: Final = "长期记忆后端抛出了异常。"
_NO_SUCH_MEMORY: Final = "配置里指定的长期记忆后端不存在。"
