"""取消：显式 `CancelToken` 与 6 个命名检查点。

职责：定义可传播、可等待、幂等的 `CancelToken`，以及 turn 执行路径上 6 个命名检查点
`Checkpoint`；把「取消原因」翻译成稳定错误码。
不负责：决定何时取消（那是 CLI、Channel 与 orchestrator）、保存已产生内容、
等待不可取消的工具——本模块不含 IO、不创建任务、不认识 session 与工具。

**为什么不是 `asyncio.CancelledError`**：`CancelledError` 会在任意 await 点
抛出，被取消的代码无从选择「先把已产生的文本存下来再退出」。而  要求中断后
已产生的输出、已发生的副作用和历史状态**可判定**。显式令牌把「该停了」变成一个可查询、
可等待的事实，退出点由 Kernel 在命名检查点上选定，因此每个检查点的善后语义可以写死、
可以逐个测试。

6 个检查点与中断后语义：

| # | `Checkpoint` | 归属 | 中断后语义 |
| --- | --- | --- | --- |
| 1 | `BEFORE_CONTEXT` | orchestrator | turn 未产生内容，除用户消息外不写入历史 |
| 2 | `BEFORE_MODEL_REQUEST` | engine | 同上 |
| 3 | `BETWEEN_STREAM_CHUNKS` | engine | 已产生文本持久化并标记 `interrupted=True` |
| 4 | `AFTER_MODEL_RESPONSE` | orchestrator | 保存 assistant 消息，未执行工具 `side_effect=NONE` |
| 5 | `BEFORE_TOOL_CALL` | engine | 未执行工具 `side_effect=NONE` |
| 6 | `AFTER_TOOL_RESULT` | engine | 已执行工具保留真实结果 |

本模块不是线程安全的：一个 turn 的令牌树只在单个事件循环内使用。跨线程取消应由持有
令牌的协程侧发起（`loop.call_soon_threadsafe`），而不是给令牌加锁——加锁会给每个检查点
带来无谓开销，而检查点在热路径上。
"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from karyvia.contracts import CancelReason, ErrorCode, KaryviaError

__all__ = [
    "CANCEL_REASON_CODES",
    "CHECKPOINT_OWNERS",
    "DEFAULT_TOOL_CANCEL_GRACE_MS",
    "CancelToken",
    "Checkpoint",
    "CheckpointOwner",
]

#: 工具收到取消信号后的宽限期。超时仍未返回则不再
#: 等待，工具结果写成 `ok=False, side_effect=UNKNOWN`，后台任务登记到实例级
#: 孤儿任务表。等待与登记本身属于的工具调用路径，本模块只定义默认值：
#: 它是取消语义的一部分，不是预算项，因此不进 `TurnLimits` 的六项。
DEFAULT_TOOL_CANCEL_GRACE_MS: Final = 2000


class CheckpointOwner(StrEnum):
    """检查点由谁执行。两层拆分决定了这张表不能是自由文本。"""

    ENGINE = "engine"
    ORCHESTRATOR = "orchestrator"


class Checkpoint(StrEnum):
    """turn 执行路径上的 6 个命名检查点。

    取值即诊断信息：检查点抛出的 `KaryviaError.detail["checkpoint"]` 就是这里的字符串，
    「turn 在哪一步被打断」因此不依赖读日志上下文来猜。数量固定为 6——新增检查点意味着
    新增一种善后语义，必须同时更新契约测试和所有终态处理方。
    """

    BEFORE_CONTEXT = "before_context"
    BEFORE_MODEL_REQUEST = "before_model_request"
    BETWEEN_STREAM_CHUNKS = "between_stream_chunks"
    AFTER_MODEL_RESPONSE = "after_model_response"
    BEFORE_TOOL_CALL = "before_tool_call"
    AFTER_TOOL_RESULT = "after_tool_result"


#: 检查点归属。 的 engine 只实现 2/3/5/6，1/4 在 orchestrator 边界上——
#: engine 不知道 context 从哪来，也不负责写 assistant 消息。
CHECKPOINT_OWNERS: Final[Mapping[Checkpoint, CheckpointOwner]] = MappingProxyType(
    {
        Checkpoint.BEFORE_CONTEXT: CheckpointOwner.ORCHESTRATOR,
        Checkpoint.BEFORE_MODEL_REQUEST: CheckpointOwner.ENGINE,
        Checkpoint.BETWEEN_STREAM_CHUNKS: CheckpointOwner.ENGINE,
        Checkpoint.AFTER_MODEL_RESPONSE: CheckpointOwner.ORCHESTRATOR,
        Checkpoint.BEFORE_TOOL_CALL: CheckpointOwner.ENGINE,
        Checkpoint.AFTER_TOOL_RESULT: CheckpointOwner.ENGINE,
    }
)

#: 取消原因到稳定错误码的唯一映射。四个原因各有落点：把 `SHUTDOWN` 并进
#: `CANCELLED_BY_USER` 会让「是用户按了 Ctrl-C 还是实例在关闭」在诊断里不可判定，
#: 而这两者的后续处理完全不同（前者会话继续可用，后者进程正在退出）。
#: `TIMEOUT` 归到 `CANCELLED_BY_BUDGET`：turn 总超时是 §6.4 的六项预算之一。
CANCEL_REASON_CODES: Final[Mapping[CancelReason, ErrorCode]] = MappingProxyType(
    {
        CancelReason.USER: ErrorCode.CANCELLED_BY_USER,
        CancelReason.TIMEOUT: ErrorCode.CANCELLED_BY_BUDGET,
        CancelReason.BUDGET: ErrorCode.CANCELLED_BY_BUDGET,
        CancelReason.SHUTDOWN: ErrorCode.CANCELLED_BY_SHUTDOWN,
    }
)

_CANCEL_MESSAGES: Final[Mapping[CancelReason, str]] = MappingProxyType(
    {
        CancelReason.USER: "turn 已被用户中断。",
        CancelReason.TIMEOUT: "turn 已超过总时长预算并被取消。",
        CancelReason.BUDGET: "turn 已超出预算并被取消。",
        CancelReason.SHUTDOWN: "实例正在关闭，turn 已被取消。",
    }
)


class CancelToken:
    """一次 turn（或其子任务）的取消令牌。

    结构化满足 `contracts.protocols.CancelSignal`：能力实现拿到的是同一个对象，
    但它们只看得见 `requested` 与 `raise_if_requested()` 这个只读面。`request()` 与
    `child()` 属于 Kernel——工具无权取消调用它的整个 turn。

    三条不变量：

    - **`request` 幂等**。第一次的 `reason` 保留，重复请求不改变状态、
      不重复传播、不产生新的观察事件。用户连按三次 Ctrl-C 与按一次的结果必须完全一致。
    - **取消只向下传播**。父令牌取消时全部后代立即取消；子令牌取消不影响父与兄弟。
      工具超时取消一个子 turn，不该顺手停掉外层对话。
    - **令牌只表达「该停了」**，不表达「已经停了」。谁在哪个检查点退出、退出前保存什么，
      由持有令牌的代码决定——这正是本项目不用 `CancelledError` 的原因。
    """

    __slots__ = ("__weakref__", "_children", "_event", "_reason")

    def __init__(self) -> None:
        self._reason: CancelReason | None = None
        self._children: weakref.WeakSet[CancelToken] = weakref.WeakSet()
        # 事件懒创建：一个 turn 会派生出每工具一个令牌，绝大多数从不被 await，
        # 为它们预先造 `asyncio.Event` 只是无谓开销。
        self._event: asyncio.Event | None = None

    @property
    def requested(self) -> bool:
        """是否已被请求取消。幂等查询，不抛异常。"""
        return self._reason is not None

    @property
    def reason(self) -> CancelReason | None:
        """取消原因；未取消时为 `None`。重复请求时保持第一次的值。"""
        return self._reason

    def request(self, reason: CancelReason = CancelReason.USER) -> None:
        """请求取消，并向所有后代传播。幂等：已取消时直接返回。"""
        if self._reason is not None:
            return
        self._reason = reason
        if self._event is not None:
            self._event.set()
        # 传播前先固定快照：子令牌的 `request()` 不会改动本集合，但弱引用集合在迭代中
        # 被 GC 修改是可能的，取消路径不接受这种偶发的 RuntimeError。
        for child in list(self._children):
            child.request(reason)

    def raise_if_requested(self) -> None:
        """已请求取消则抛 `KaryviaError`；否则返回。

        **异常约定**：只抛 `CANCELLED` 类的 `KaryviaError`（`CANCEL_REASON_CODES`）。
        """
        if self._reason is not None:
            raise self._to_error(self._reason, checkpoint=None)

    def checkpoint(self, where: Checkpoint) -> None:
        """命名检查点：已请求取消则抛出，异常里带上是哪个检查点。

        与 `raise_if_requested()` 的区别只在诊断信息。engine 与 orchestrator 一律用这个
        方法，`detail["checkpoint"]` 因此能直接回答「turn 停在哪一步、已经产生了什么」。
        """
        if self._reason is not None:
            raise self._to_error(self._reason, checkpoint=where)

    def child(self) -> CancelToken:
        """派生子令牌，用于工具调用与子 turn。

        父令牌已取消时，新子令牌**出生即取消**并继承父的 `reason`——否则「取消后又派生了
        一个工具调用」会得到一个看起来正常的令牌，取消语义就有了一个静默的漏洞。
        """
        child = CancelToken()
        if self._reason is not None:
            child.request(self._reason)
        else:
            self._children.add(child)
        return child

    async def wait(self) -> CancelReason:
        """等待取消发生并返回原因；已取消则立即返回。

        取消必须**可等待**而不只是可轮询：不可取消的工具需要 Kernel 在
        `DEFAULT_TOOL_CANCEL_GRACE_MS` 的宽限期内与取消赛跑，只有轮询的话
        Kernel 只能寄希望于能力实现自觉检查——而  要处理的恰恰是不自觉的那些。
        """
        if self._reason is not None:
            return self._reason
        if self._event is None:
            self._event = asyncio.Event()
        await self._event.wait()
        assert self._reason is not None  # 事件只在 request() 里被 set，不可能空。
        return self._reason

    def _to_error(self, reason: CancelReason, *, checkpoint: Checkpoint | None) -> KaryviaError:
        detail: dict[str, str] = {"reason": reason.value}
        if checkpoint is not None:
            detail["checkpoint"] = checkpoint.value
            detail["owner"] = CHECKPOINT_OWNERS[checkpoint].value
        return KaryviaError(
            CANCEL_REASON_CODES[reason],
            _CANCEL_MESSAGES[reason],
            detail=detail,
            retryable=reason is not CancelReason.SHUTDOWN,
        )

    def __repr__(self) -> str:
        state = "pending" if self._reason is None else self._reason.value
        return f"CancelToken({state})"
