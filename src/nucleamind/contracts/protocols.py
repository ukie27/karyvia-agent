"""能力接口：Kernel 与能力实现之间唯一的行为契约（技术方案 §5.1、需求 §9.3 `SDK-001`）。

职责：声明 10 个能力 Protocol（`ModelProvider` / `ToolHandler` / `ContextProvider` /
`SessionStore` / `MemoryProvider` / `Channel` / `CommandHandler` / `HookHandler` /
`CliEntry`）与三个支撑用的只读/动作面（`CancelSignal` / `InstanceView` / `TurnControl`），
并在每个方法的 docstring 上固定写明**异常约定**与**取消语义**。
不负责：提供任何实现、决定调用顺序与重试、执行超时——实现在 `builtins/` 与 `plugins/`，
调度在 `kernel/`；本模块只有签名，函数体一律是 docstring + `...`。

三条本模块自身的规则：

- **结构化子类型，插件不继承宿主基类**。这是 `PLG-002`（插件不得通过导入 Kernel 私有
  模块获得隐式能力）在类型层的前提：能力实现只要形状对得上就能注册。
- **`runtime_checkable` 只用于诊断输出，不用于控制流**。它只查属性存在性，不查签名；
  拿它做分支等于把类型错误推迟到调用点。
- **取消一律通过 `CancelSignal` 观测，而不是捕获 `asyncio.CancelledError`**
  （技术方案 §6.4）。`CancelledError` 会在任意 await 点抛出，「保存已产生内容并标记为
  取消」这个语义在它下面无法实现。能力实现方拿到的只有观测面——`request()` 与
  `child()` 只在 Kernel 侧的 `CancelToken` 上，能力实现无权取消整个 turn。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .capability import HookContext, HookOutcome
from .command import CommandInvocation, CommandResult, CommandSpec
from .compaction import CompactionRequest, CompactionResult
from .context import ContextFragment, FragmentScope
from .ids import Correlation, SessionKey, TurnId
from .message import InboundMessage, OutboundMessage
from .model import ModelChunk, ModelInfo, ModelRequest, ModelResponse
from .session import CancelReason, SessionMessage, SessionSnapshot
from .tool import ToolInvocation, ToolResult

if TYPE_CHECKING:  # pragma: no cover - 仅为注解，运行时不导入，避免与包根成环。
    from . import JsonValue

__all__ = [
    "CancelSignal",
    "Channel",
    "CliEntry",
    "CommandHandler",
    "ContextProvider",
    "ContextCompactor",
    "HookHandler",
    "InstanceView",
    "MemoryProvider",
    "ModelProvider",
    "SessionStore",
    "ToolHandler",
    "TurnControl",
]


@runtime_checkable
class CancelSignal(Protocol):
    """取消信号的只读观测面（技术方案 §6.4）。

    `kernel/turn/cancel.py::CancelToken` 结构化满足本 Protocol，并额外提供
    `request()` 与 `child()`。分成两个面是刻意的：能力实现需要知道「该停了」，
    但不该有能力替 Kernel 决定整个 turn 的终态。
    """

    @property
    def requested(self) -> bool:
        """是否已被请求取消。幂等查询，不抛异常。"""
        ...

    def raise_if_requested(self) -> None:
        """已请求取消则抛 `NucleaError(CANCELLED_BY_USER | CANCELLED_BY_BUDGET)`。

        **异常约定**：只抛 `CANCELLED` 类的 `NucleaError`，不抛其他类型。
        """
        ...


@runtime_checkable
class InstanceView(Protocol):
    """实例的只读视图（需求 `PLG-006`、`NFR-502`、`CMD-001`）。

    存在的理由：`/help`、`/capabilities`、`/plugins`、`/config`、`/session` 这五个命令
    要回答的都是「这个实例现在是什么样」，而 `R4` 禁止 `builtins/` 与 `plugins/` import
    `kernel/`——数据在 `kernel/registry` 与 `kernel/observability` 里，命令够不着。
    没有这个门面，`commands_core` 就只能由 `runtime/` 特权注册，`BAS-005`「内建能力不
    享受特权」当场破例，第三方也永远写不了 `/status` 这类命令。

    它落在 `contracts/` 而不是 `sdk/`：kernel 侧的实现要结构化满足它，而 `R2` 禁止
    `kernel/` import `sdk/`；放在共享契约层可让生产实现与插件依赖同一个 Protocol。

    **只读，且只读一份快照**。这里没有任何改状态的方法：改配置是 `nm config`、
    启停插件是 `nm plugins`，两者都在进程外。命令能做的只有把现状讲给用户听。

    `capabilities()` 与 `plugins()` 返回 JSON 而不是 kernel 类型，是因为
    `ResolutionReport` 与 `PluginStatus` 都在 `kernel/` 里、契约层够不着；而这两样
    **本来就以 JSON 为发布形态**（`NFR-502` 要求报告可序列化，两者各有 `to_json()`）。
    在契约层复刻一遍它们的字段只会多出一份必然漂移的定义。其余三个成员用的类型全在
    `contracts/` 里，因此是强类型的。
    """

    def commands(self) -> tuple[CommandSpec, ...]:
        """全部已注册命令的声明，按命令名排序去重（`CMD-001`）。

        排序是契约的一部分：`/help` 的输出不该随注册顺序漂移。

        **异常约定**：不抛。命令索引在启动期就已建好（`CMD-002` 的冲突也在那时报出），
        这里只是读它。
        **取消语义**：同步纯查询，不接受取消信号。
        """
        ...

    def capabilities(self) -> Mapping[str, JsonValue]:
        """覆盖解析报告的 JSON 形态：`active` / `shadowed` / `disabled` / `failures`。

        每一项都标明由内建还是哪个插件提供（`PLG-006`）——这正是 `/capabilities` 要显示
        的东西，也是 `nm capabilities` 的同一份数据源。

        **异常约定**：不抛；报告本身可能含 `failures`，那是数据不是异常。
        **取消语义**：同步纯查询，不接受取消信号。
        """
        ...

    def plugins(self) -> tuple[Mapping[str, JsonValue], ...]:
        """每个插件的诊断视图（状态、版本、已注册能力、失败原因与阶段）的 JSON 形态。

        没有任何外部插件时返回空元组——**「没有插件」与「查不到」是两个结论**，
        前者是 `EDG-101` 明确要求可用的形状。

        **异常约定**：不抛。
        **取消语义**：同步纯查询，不接受取消信号。
        """
        ...

    def config_document(self) -> Mapping[str, JsonValue]:
        """**完整**的生效配置文档，供 `/config` 渲染。

        这是本门面唯一越过 `CFG-002`（`ctx.config` 只给自己那一块）的地方，因为
        `/config` 的职责就是显示整份配置。**明文不会在这里**：配置树自始至终
        持有 `${VAR}` 字面量、解析出的明文只在 `SecretMap` 里，因此这份文档
        「没有别的东西可泄漏」——脱敏是结构性成立的，不是靠调用方记得过滤。

        **异常约定**：不抛。
        **取消语义**：同步纯查询，不接受取消信号。
        """
        ...

    async def session_snapshot(self, key: SessionKey) -> SessionSnapshot:
        """读一个会话的当前快照，供 `/session` 显示条数与压缩水位。

        刻意只给快照而不是整个 `SessionStore`：后者带着 `delete()` 与 `compact()`，
        而 `/session` 一个都不需要。**门面的宽度应当等于用途的宽度。**

        **异常约定**：会话不存在时返回空快照而不是抛错（与 `SessionStore.load()` 一致，
        「没有历史」是正常状态）；存储损坏抛 `PERSISTENCE_RECORD_CORRUPT`。
        **取消语义**：不接受取消——一次快照读取是原子的，中途放弃只会让调用方拿到
        一个既不完整也不失败的结果。
        """
        ...


@runtime_checkable
class TurnControl(Protocol):
    """在跑 turn 的观测与取消（技术方案 §10.3）。

    与 `InstanceView` 分开而不是合成一个七成员门面：一个是只读可观测性、一个是**控制
    动作**。「能看」与「能取消别人的 turn」应当可以分别授予，
    合并成一个门面就没有这个分界线了。
    """

    def live_turns(self) -> tuple[TurnId, ...]:
        """当前在跑的 turn。

        **异常约定**：不抛。
        **取消语义**：同步纯查询，不接受取消信号。
        """
        ...

    def cancel_turn(self, turn_id: TurnId, reason: CancelReason = CancelReason.USER) -> bool:
        """请求取消一个在跑的 turn，返回它当时是否还在跑。

        `False` 意味着「已经结束了」而不是「失败了」——这两者对用户是同一个好结果，
        但对诊断不是。幂等：重复请求时第一次的 `reason` 胜出（`EDG-206`）。

        **注意这是请求而不是保证**：取消经 `CancelToken` 在检查点生效，好让 turn 有机会
        保存已产生的内容再退出（技术方案 §6.4）。方法返回时目标 turn 通常还没停下。

        **异常约定**：不抛；未知 `turn_id` 返回 `False`。
        **取消语义**：这**是**取消入口本身，不接受取消信号。
        """
        ...


@runtime_checkable
class ModelProvider(Protocol):
    """模型供应商（需求 §9.5 `MOD-001`–`MOD-005`）。

    Provider 不得决定 Session、Memory 或 Channel 行为（`MOD-004`）：它拿到的是一份
    已经组装好的 `ModelRequest`，交回的是文本、Tool Call、终止原因与用量。
    """

    def describe(self, model_id: str) -> ModelInfo:
        """声明该模型的能力与窗口大小（`MOD-001`）。

        不具备的能力必须缺席 `ModelInfo.capabilities`，不得静默降级后假装支持
        （`MOD-005`）。

        **异常约定**：模型不存在抛 `CAPABILITY_MISSING`；配置缺失抛 `CONFIG_INVALID`。
        本方法是纯查询，不得发起网络请求——它在 turn 的预算推导路径上（`CTX-003`）。
        **取消语义**：同步方法，不涉及取消。
        """
        ...

    async def complete(self, request: ModelRequest, cancel: CancelSignal) -> ModelResponse:
        """一次非流式请求。

        **异常约定**：限流、超时、认证失败与内容过滤必须转换为可分类的 `NucleaError`
        （`MOD-003`）——分别是 `EXTERNAL_MODEL_PROVIDER`（`retryable` 如实标注）、
        `TIMEOUT_MODEL_REQUEST`、`CONFIG_SECRET_MISSING` / `PERMISSION_DENIED`，
        以及 `StopReason.CONTENT_FILTER` 的正常响应（内容过滤是结果不是异常）。
        供应商 SDK 的原生异常不得逸出本方法（`MSG-004` 的同类要求）。
        **取消语义**：进入网络调用前检查 `cancel`；调用期间被取消时抛
        `CANCELLED` 类错误，此时不得返回半份响应。
        """
        ...

    def stream(self, request: ModelRequest, cancel: CancelSignal) -> AsyncIterator[ModelChunk]:
        """一次流式请求，逐片产出增量。

        声明为普通 `def` 返回 `AsyncIterator` 而不是 `async def`，实现方因此可以直接
        写成异步生成器（`async def` + `yield`），不必额外包一层。

        **异常约定**：同 `complete()`；已经 yield 过分片后再失败，必须先 yield 一个
        `ChunkKind.DONE`（`stop_reason=ERROR`）再抛，让消费方能把已收到的文本按
        `interrupted=True` 持久化（技术方案 §6.4 检查点 3）。
        模型不支持流式时抛 `CAPABILITY_MISSING`，不得自行降级为一次性返回（`MOD-005`）。
        **取消语义**：每产出一片前检查 `cancel`；被取消时结束迭代并抛 `CANCELLED` 类
        错误，已 yield 的分片由调用方保留。
        """
        ...


@runtime_checkable
class ToolHandler(Protocol):
    """一个工具的执行体（需求 §9.9 `TOL-001`–`TOL-007`）。"""

    async def execute(self, invocation: ToolInvocation, cancel: CancelSignal) -> ToolResult:
        """执行一次调用并返回结果。

        **异常约定**：**约定不抛**。失败是一等结果，应返回
        `ToolResult(ok=False, error=...)`——`ok=False` 必须带 `error` 已由契约强制。
        确实逸出的异常会被 Kernel 记为 `ToolResult(ok=False, side_effect=UNKNOWN)`：
        Kernel 无从判断外部世界变了没有，只能如实说不知道（`EDG-401`、`EDG-407`）。
        堆栈不得进入 `content`——它是模型可见文本（§10.5 末段）。
        **取消语义**：在每个可能长时间阻塞的步骤前检查 `cancel`。取消后仍必须返回
        `ToolResult`，并如实标注 `side_effect`：确定没做返回 `NONE`，做了一半或不确定
        返回 `UNKNOWN`。Kernel 只等待 `tool_cancel_grace_ms`（默认 2000），超时后会
        自行写入 `tool.cancel_timeout` 结果并把本协程登记为孤儿任务（`EDG-407`）。
        """
        ...


@runtime_checkable
class ContextProvider(Protocol):
    """上下文片段的贡献者（需求 §9.6 `CTX-001`–`CTX-006`）。"""

    async def provide(
        self,
        snapshot: SessionSnapshot,
        correlation: Correlation,
        cancel: CancelSignal,
    ) -> tuple[ContextFragment, ...]:
        """为本次 turn 贡献片段。返回空元组表示无贡献，这不是错误。

        Provider 交出的是**片段**而不是最终文本：`trust`、`priority`、`sensitivity`
        与预算约束因此无法被绕过（`CMD-004`、`CMD-005`）。裁剪与排序由组装器负责，
        Provider 只需保证单个片段不超长并如实填写 `estimated_tokens`。

        **异常约定**：可以抛 `NucleaError`。宿主记录原因并跳过本 Provider（`CTX-005`）。
        **取消语义**：在每个外部查询前检查 `cancel`；被取消时抛 `CANCELLED` 类错误，
        本次贡献整体作废——半份上下文比没有上下文更危险。
        """
        ...


@runtime_checkable
class ContextCompactor(Protocol):
    """会话历史的压缩策略。"""

    async def compact(
        self,
        request: CompactionRequest,
        cancel: CancelSignal,
    ) -> CompactionResult | None:
        """返回压缩建议，或以 `None` 表示本轮不压缩。

        Kernel 负责触发、校验、持久化与回退；实现只决定摘要内容和压缩水位。

        **异常约定**：可以抛 `NucleaError`；Kernel 将其记录为插件失败并沿用本轮已经完成的
        确定性裁剪结果，不得让压缩策略故障阻断模型请求。返回的空摘要、倒退或越界水位同样
        视为插件失败。
        **取消语义**：在模型调用等外部操作前检查 `cancel`；收到取消后尽快停止并抛
        `CANCELLED` 类错误。Kernel 不会取消已经开始的 Session 持久化。
        """
        ...


@runtime_checkable
class SessionStore(Protocol):
    """会话历史的持久化实现（需求 §9.7 `SES-001`–`SES-006`）。

    同一 Session 的写入由 Kernel 的 session 锁保证单一写者（`KER-008`），因此实现方
    不需要自己做跨进程以外的并发控制，但**必须**保证单次写入的顺序与原子性
    （`SES-002`）。
    """

    async def load(self, key: SessionKey) -> SessionSnapshot:
        """读取会话全量视图。

        **异常约定**：会话不存在时返回空快照（`SessionSnapshot(session_key=key)`），
        **不抛**——「第一次说话」不是错误。读取失败或记录损坏抛 `PERSISTENCE_READ_FAILED`
        / `PERSISTENCE_RECORD_CORRUPT`，不得返回空快照冒充「没有历史」，那会让一次读盘
        故障静默清空用户的上下文。
        **取消语义**：读操作短暂，不接受取消。
        """
        ...

    async def append(self, key: SessionKey, messages: Sequence[SessionMessage]) -> None:
        """顺序追加记录，整批原子生效。

        **异常约定**：失败抛 `PERSISTENCE_WRITE_FAILED`，turn 随之标记 `FAILED`；
        不得吞掉异常伪装成功（`SES-003`）。部分写入必须回滚或以整体失败呈现。
        **取消语义**：写操作不接受取消——中途放弃会留下半份历史。
        """
        ...

    async def compact(self, key: SessionKey, through: int, summary: SessionMessage) -> None:
        """把前 `through` 条记录替换为摘要，推进压缩水位（`SES-005`）。

        原始记录是否物理删除由实现决定，但 `load()` 之后
        `SessionSnapshot.compacted_through` 必须等于 `through`。

        **异常约定**：`through` 越界抛 `INPUT_MALFORMED`；写失败同 `append()`。
        **取消语义**：不接受取消。
        """
        ...

    async def delete(self, key: SessionKey) -> bool:
        """删除会话，返回是否真的存在过（`SES-005`）。

        **异常约定**：不存在返回 `False`，不抛；删除失败抛 `PERSISTENCE_WRITE_FAILED`。
        **取消语义**：不接受取消。
        """
        ...

    async def list_keys(self) -> tuple[SessionKey, ...]:
        """列出全部会话标识，供 `/session` 命令与迁移工具使用（`SES-004`）。

        **异常约定**：读取失败抛 `PERSISTENCE_READ_FAILED`；单条记录损坏应跳过并记录，
        不得让一条坏记录使整个列表不可用。
        **取消语义**：不接受取消。
        """
        ...


@runtime_checkable
class MemoryProvider(Protocol):
    """跨 Session 的长期记忆（需求 §9.8 `MEM-001`–`MEM-005`）。

    Kernel 只依赖本接口，不假设文件、向量或图数据库（`MEM-001`）。写入与检索的载荷
    都是 `ContextFragment`：它自带 `source`、`trust`、`scope`、`sensitivity` 与
    `expires_at`，恰好覆盖 `MEM-002`（范围）与 `MEM-004`（来源与可信度标记），
    而且召回结果可以直接进上下文，不需要中间形态。

    **它是实例级（`FragmentScope.AGENT`）长期记忆的接口。这是决定，不是默认。**
    下面三个方法**一个 `SessionKey` 都不带**，因此经这条接口根本表达不出「哪个会话的
    记忆」——`scope=SESSION` 传下去，实现方只能猜。会话级与工作区级的记忆归
    `ContextProvider`：它的 `provide()` 拿得到 `SessionSnapshot.session_key`
    （`plugins/nucleamind-plugin-memory/` 的四条通路就是这么分的）。
    Kernel 侧的召回因此**只用 `AGENT`**（`kernel/turn/memory.py::MEMORY_RECALL_SCOPE`），
    实现方可以对其余三个范围直接抛 `INPUT_UNSUPPORTED` 而不算违约。

    要改这个决定得给三个方法各加一个 key 参数，而 SDK 已发 `1.0.0`——那是 §7.6 意义上的
    破坏性变更，得走 major。在此之前**不要**用「约定在 `query` 里编码会话 id」这类办法
    绕过它：那会让两个后端对同一个字符串有两种理解。
    """

    async def remember(self, fragment: ContextFragment, cancel: CancelSignal) -> str:
        """写入一条记忆，返回稳定的记录标识。

        **异常约定**：写失败抛 `PERSISTENCE_WRITE_FAILED`。模型生成内容应以
        `trust=UNTRUSTED` 写入——召回时会被包裹为数据块，不获得指令优先级（`EDG-306`）。
        **取消语义**：写操作不接受取消。
        """
        ...

    async def recall(
        self,
        query: str,
        *,
        scope: FragmentScope,
        limit: int,
        cancel: CancelSignal,
    ) -> Mapping[str, ContextFragment]:
        """按相关性召回记忆，返回 `记录标识 -> 片段` 的有序映射。

        用映射而不是元组，是为了让调用方在不引入第三个类型的前提下拿到记录标识
        （`MEM-005` 的「查询、修正、删除」都需要它）。**顺序即相关性排序**，
        实现方必须按相关性从高到低插入。

        **异常约定**：后端不可用抛 `EXTERNAL_SERVICE` 类错误；Kernel 按配置降级为
        无长期记忆模式继续对话（`MEM-003`），因此本方法不得为了「保证可用」而返回空结果
        掩盖故障。**降级的判定在 `memory.on_failure`**（默认 `degrade`），实现方不需要、
        也不应该自己做那个选择。
        **取消语义**：检索前与每次外部往返后检查 `cancel`，被取消时抛 `CANCELLED` 类错误。
        Kernel 侧的召回**不把取消当成后端故障**（因此它不走降级，见类 docstring）。
        """
        ...

    async def forget(self, record_id: str) -> bool:
        """删除一条记忆，返回是否真的存在过（`MEM-005`）。

        「修正」不单列方法：`forget()` + `remember()` 的组合语义明确，而原地修改会让
        「这条记忆是什么时候、由谁写的」变得不可追溯。

        **异常约定**：不存在返回 `False`，不抛；删除失败抛 `PERSISTENCE_WRITE_FAILED`。
        **取消语义**：不接受取消。
        """
        ...


@runtime_checkable
class Channel(Protocol):
    """外部平台接入（需求 §9.4 `MSG-001`–`MSG-007`）。

    归一化在 Channel 边界完成：原始 SDK 对象不得进入 Kernel（`MSG-004`）。
    入站用**拉模型**（`receive()` 产出异步迭代器）而不是回调注册，因为拉模型让
    「谁在消费、什么时候停」直接由 Kernel 的任务结构表达，不需要再定义一个提交回调
    Protocol 和它的错误语义。
    """

    @property
    def channel_id(self) -> str:
        """本 Channel 的稳定标识，构成 `SessionKey.channel_id`（`SES-001`）。"""
        ...

    async def start(self) -> None:
        """建立与平台的连接。

        **异常约定**：失败抛 `EXTERNAL_CHANNEL`；宿主记录失败并继续启动其余能力。
        **取消语义**：不接受取消；连接失败应快速返回而不是无限重试。
        """
        ...

    async def stop(self) -> None:
        """断开连接并释放资源。

        **异常约定**：**约定不抛**。停止阶段的异常只会拖住整个实例退出；实现应内部
        记录并返回。Kernel 对每个能力的停止有独立超时，超时即放弃等待（`EDG-104`）。
        **取消语义**：不接受取消。
        """
        ...

    def receive(self) -> AsyncIterator[InboundMessage]:
        """产出归一化后的入站消息，直到 `stop()` 被调用。

        **异常约定**：平台侧的可恢复错误应内部重试并继续迭代；不可恢复错误抛
        `EXTERNAL_CHANNEL` 并结束迭代。畸形消息应丢弃并记录，不得让一条坏消息终止整个
        Channel（`MSG-004`）。
        **取消语义**：迭代由 `stop()` 或消费方取消结束，不接受 `CancelSignal`——
        它是实例级长生命周期服务，不属于任何单个 turn。
        """
        ...

    async def deliver(self, message: OutboundMessage) -> None:
        """投递一条出站消息。

        消息自带 `channel_id + conversation_id + turn_id`，实现方不需要维护自己的
        Session 映射（`MSG-006`）。平台长度上限、附件限制与流式能力差异由实现方处理，
        必要时降级为分段或只发最终结果（`MSG-003`、`MSG-005`）。
        `message.is_complete_answer` 为假时必须附加明确标记，不得渲染成完整回答
        （`EDG-304`）。

        **它可能被并发调用**：不同 Session 的 turn 会同时跑到这一步。**同一 Session 内
        不会并发**——`SessionScheduler` 是唯一单写者边界，因此按 conversation 分片的缓冲
        （流式 edit-in-place 那类）不需要加锁。

        **异常约定**：投递失败抛 `EXTERNAL_CHANNEL` 并如实标注 `retryable`。
        **抛出是安全的**：出站路由点捕获它、发一条 `channel.delivery_failed`，turn 照样
        走到自己的终态并完整持久化（`EDG-204`）。因此实现方**不需要**为了保住 turn 而把
        投递故障吞成成功——那样「消息一直发不出去」就只能靠用户来发现。

        调用方会把异常转换成 `channel.delivery_failed`，而不会改写已经完成并持久化的 turn
        终态。实现方仍必须抛错，因为只有它知道投递是否真正成功。
        **取消语义**：不接受取消——投递是 turn 的最后一步，此时取消只会让用户什么也收不到。
        """
        ...


@runtime_checkable
class CommandHandler(Protocol):
    """斜杠命令的执行体（需求 §9.13 `CMD-001`–`CMD-005`）。"""

    async def handle(self, invocation: CommandInvocation, cancel: CancelSignal) -> CommandResult:
        """执行命令。

        **异常约定**：**约定不抛**。失败应返回
        `CommandResult(disposition=REJECTED, error=...)`——命令处理失败不得导致实例退出，
        必须返回可诊断错误并保持会话可用（`CMD-003`）。逸出的异常由 Kernel 捕获为同形状的
        结果，但那样就丢掉了实现方本可以给出的具体说明。
        **取消语义**：命令通常短暂；长耗时命令应在每步前检查 `cancel`，被取消时返回
        `REJECTED` 并在 `error` 中说明，而不是抛出。
        """
        ...


@runtime_checkable
class HookHandler(Protocol):
    """生命周期与流水线扩展点（技术方案 §6.6）。"""

    async def handle(self, context: HookContext) -> HookOutcome | None:
        """处理一次 Hook 分发。返回 `None` 等价于 `HookAction.CONTINUE`。

        Observer 的返回值一律被忽略——它按定义不影响 turn，所以最自然的实现就是返回
        `None`。Interceptor 的返回值按 `HookAction` 采纳，具体取哪一份载荷见
        `HookAction.REPLACE` 的说明。

        **异常约定**：**handler 不应抛出异常，抛出被视为插件故障并被隔离。**
        Observer 与 Interceptor 的异常、超时只记 `PLUGIN_FAILURE` 事件，不影响 turn
        结果；跳过该 handler 后继续（`NFR-204`、`CTX-005`）。
        **取消语义**：不接受 `CancelSignal`。Hook 有自己的独立超时
        （observer 2000ms / interceptor 5000ms），超时即按上面的隔离规则处理；
        把 turn 的取消信号交给 Hook 只会诱导它在 `turn_end` 这类清理点提前退出。
        """
        ...


@runtime_checkable
class CliEntry(Protocol):
    """本地命令行入口（需求 §9.2 `BAS-009`、`BAS-010`、`EDG-108`、技术方案 §8.1）。

    这是 `CapabilityKind.CLI_ENTRY` 的载荷类型。它落在 `contracts/` 而不是 `sdk/`：
    `kernel/` 与 `runtime/`
    都要调用 CLI 能力，而 `R2` 禁止它们 import `sdk/`。

    CLI 入口不可禁用——不装任何 Channel 插件也必须存在本地交互入口。插件可以覆盖它
    （`CLI_ENTRY` 是 SINGLETON），但覆盖实现加载失败时 Runtime 强制回落到内建实现
    （`EDG-108`），因此本接口不需要「没有实现」这个状态。

    与 `Channel` 分开而不是复用：Channel 是可有可无的外部平台接入，生命周期由实例
    托管；CLI 入口拥有进程本身——它决定 `nm` 什么时候返回、返回什么退出码。
    """

    async def run(self, argv: Sequence[str], cancel: CancelSignal) -> int:
        """接管本次 `nm` 调用，返回进程退出码（0 表示成功）。

        交互式会话与单次执行（`nm -p "..."`）是同一个方法的两种参数形态：把它们拆成
        两个方法，覆盖实现就得同时替换两处，而 `EDG-108` 的回落只能整体进行。

        **异常约定**：**约定不抛**。参数错误、能力缺失与执行失败都应打印可诊断信息并
        返回非零退出码——异常逸出会让用户看到 traceback 而不是说明。逸出的异常由
        Runtime 捕获为 `KERNEL_UNEXPECTED` 并以非零码退出。
        **取消语义**：首个 `Ctrl-C` 由 Runtime 转成 `cancel.request()`，实现方应观测
        `cancel.requested` 结束当前 turn 并**继续接受输入**，而不是退出进程；
        第二个 `Ctrl-C` 由 Runtime 直接终止进程，实现方不需要处理。
        """
        ...
