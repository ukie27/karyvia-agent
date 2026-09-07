"""配置的类型化视图：十个小节 dataclass 与 `KaryviaConfig`。

职责：把 `schema.SECTION_SPECS` 那张字段表表达成不可变、带默认值、可静态检查的类型，
并提供 `TurnSection.to_limits()` / `RetrySection.to_policy()` / `MemorySection.critical`
这类「配置 → 机制参数」的换算。
不负责：定义有哪些字段（`schema.SECTION_SPECS` 是唯一依据）、校验（`schema.validate_config`）、
默认值常量（`defaults.py`）、渲染成 JSON（`document.py`）。

**它是 `SECTION_SPECS` 的类型化投影，不是第二份真相。** 加字段仍然只改 `schema.py` 那张表，
然后在这里加一个同名同默认值的属性——两侧不一致会被
`test_every_section_spec_has_a_dataclass_field` 当场抓住。

类型化视图独立成模块，让字段声明、校验和运行时类型各自保持单一职责。`schema.py` 原样
再导出这些名字，调用方仍可从配置包的既有入口导入。

**不要在这里 module-level import `kernel.turn`**：`to_limits()` / `to_policy()` 用函数内
import，理由见 `defaults.py`——那会把 engine/scheduling/folding 与 asyncio 拖上配置路径
（冷启动预算 300 ms）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Mapping

from . import plugin_blocks as blocks
from .defaults import (
    DEFAULT_CHANNEL_CONCURRENCY,
    DEFAULT_COMMAND_PREFIX,
    DEFAULT_CONTEXT_PROVIDER_TIMEOUT_MS,
    DEFAULT_DEDUP_CAPACITY,
    DEFAULT_DEDUP_TTL_MS,
    DEFAULT_INTERCEPTOR_TIMEOUT_MS,
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_MAX_TOOL_CALLS_PER_TURN,
    DEFAULT_MEMORY_FRAGMENT_PRIORITY,
    DEFAULT_MEMORY_ON_FAILURE,
    DEFAULT_MEMORY_RECALL_LIMIT,
    DEFAULT_MEMORY_RECALL_TIMEOUT_MS,
    DEFAULT_OBSERVER_TIMEOUT_MS,
    DEFAULT_PLUGIN_STOP_TIMEOUT_MS,
    DEFAULT_QUEUE_MAX_SIZE,
    DEFAULT_RETRY_BASE_DELAY_MS,
    DEFAULT_RETRY_EMPTY_RESPONSE,
    DEFAULT_RETRY_MAX_ATTEMPTS,
    DEFAULT_RETRY_MAX_DELAY_MS,
    DEFAULT_SESSION_CONCURRENCY,
    DEFAULT_TOOL_RESULT_MAX_BYTES,
    DEFAULT_TOOL_TIMEOUT_MS,
    DEFAULT_TURN_COMPACTOR,
    DEFAULT_TURN_COMPACTOR_TIMEOUT_MS,
    DEFAULT_TURN_TIMEOUT_MS,
)
from .plugin_blocks import PluginEntry

if TYPE_CHECKING:
    from ...contracts import JsonValue
    from ..turn.limits import TurnLimits
    from ..turn.retry import RetryPolicy

__all__ = [
    "ContextSection",
    "HooksSection",
    "LoggingSection",
    "MemorySection",
    "ModelSection",
    "KaryviaConfig",
    "PluginsSection",
    "RetrySection",
    "RoutingSection",
    "TurnSection",
    "WorkspaceSection",
]


@dataclass(frozen=True, slots=True)
class TurnSection:
    """一次 turn 的预算。字段名与 `LimitKind` 的取值逐一对应（`limits.py`当前约定）。"""

    max_iterations: int = DEFAULT_MAX_ITERATIONS
    max_tool_calls_per_turn: int = DEFAULT_MAX_TOOL_CALLS_PER_TURN
    tool_timeout_ms: int = DEFAULT_TOOL_TIMEOUT_MS
    tool_result_max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES
    turn_timeout_ms: int = DEFAULT_TURN_TIMEOUT_MS
    #: `None` = 由模型能力推导，不是「无限制」。见 `TurnLimits.resolve_context_max_tokens`。
    context_max_tokens: int | None = None

    def to_limits(self) -> TurnLimits:
        """转成 `TurnLimits`。

        **函数内 import 是刻意的**：见 `DEFAULT_MAX_ITERATIONS` 的注释——只有真的要构造
        `TurnLimits` 的调用方（编排层）才付得起把 turn 包导入进来的代价。
        """
        from ..turn.limits import TurnLimits as _TurnLimits

        return _TurnLimits(
            max_iterations=self.max_iterations,
            max_tool_calls_per_turn=self.max_tool_calls_per_turn,
            tool_timeout_ms=self.tool_timeout_ms,
            tool_result_max_bytes=self.tool_result_max_bytes,
            turn_timeout_ms=self.turn_timeout_ms,
            context_max_tokens=self.context_max_tokens,
        )


@dataclass(frozen=True, slots=True)
class RoutingSection:
    """输入分流与 Session 并发。字段与 `kernel/routing/` 的构造参数一一对应。

    `command_prefix` 是路由的配置项而不是命令身份的一部分（见 `contracts/command.py`）：
    改前缀不该等于改全部命令声明。
    """

    command_prefix: str = DEFAULT_COMMAND_PREFIX
    #: `queue` / `merge` / `reject`，取值由 `SESSION_CONCURRENCY_CHOICES` 限定。
    session_concurrency: str = DEFAULT_SESSION_CONCURRENCY
    #: 单个 session 的等待上限；超出即降级为拒绝，不静默丢弃。
    queue_max_size: int = DEFAULT_QUEUE_MAX_SIZE
    dedup_capacity: int = DEFAULT_DEDUP_CAPACITY
    dedup_ttl_ms: int = DEFAULT_DEDUP_TTL_MS
    #: 单条 Channel 的总在途消息上限；不参与 Session 的三种并发策略。
    channel_concurrency: int = DEFAULT_CHANNEL_CONCURRENCY


@dataclass(frozen=True, slots=True)
class HooksSection:
    """Hook 分发的两项超时。

    观察者是**整批**超时、拦截器是**每个 handler** 超时：前者并发执行、返回值被忽略，
    整体拖不动 turn 就行；后者串行且能改流水线，一个慢 handler 会连累后面全部。
    """

    observer_timeout_ms: int = DEFAULT_OBSERVER_TIMEOUT_MS
    interceptor_timeout_ms: int = DEFAULT_INTERCEPTOR_TIMEOUT_MS


@dataclass(frozen=True, slots=True)
class ContextSection:
    """Context 组装。

    `context_max_tokens` **不在这里**：它是 turn 的六项预算之一，字段名与 `LimitKind`
    的取值一一对应，搬过来会破坏那条已被测试钉死的对应关系。
    """

    #: 单个 Context Provider 的独立超时。超时后记录故障并跳过。
    provider_timeout_ms: int = DEFAULT_CONTEXT_PROVIDER_TIMEOUT_MS
    #: 每轮模型请求超限时使用的统一压缩能力；生产实例必须能解析到该名字。
    turn_compactor: str = DEFAULT_TURN_COMPACTOR
    #: 单次 Turn Compactor 调用的总预算，也约束它通过窄门面发出的模型调用。
    turn_compactor_timeout_ms: int = DEFAULT_TURN_COMPACTOR_TIMEOUT_MS


@dataclass(frozen=True, slots=True)
class MemorySection:
    """长期记忆的召回。

    **`provider = None` 是默认，含义是「不启用 kernel 侧召回」而不是「自动挑一个」。**
    自动挑会让装上一个记忆插件就悄悄改变每一轮请求的内容；而这一节唯一的作用是让运维
    显式说出「用哪一个后端」。

    **它与插件自带的 Context Provider 会叠加。** 例如
    `plugins/karyvia-plugin-memory/` 同时注册了 `MEMORY:jsonl` 与 `CONTEXT:memory`，
    后者默认已经召回 `agent` 范围——两边都开着会让同一条记忆在一轮里出现两次。要用 kernel
    侧召回就把那个插件的 `enabled_scopes` 去掉 `agent`，或者干脆别写这一节。这条如实记在
    这里而不是留给用户发现，因为两条路径**都是对的**，只是不该同时开。
    """

    #: `MEMORY` 能力的名字（例如 `"jsonl"`）。`None` = 不启用。写了却不存在是
    #: `CAPABILITY_MISSING`，不是静默不启用——见 `kernel/turn/memory.py::select_memory`。
    provider: str | None = None
    #: 每轮最多召回几条。记忆与会话历史抢同一份预算。
    recall_limit: int = DEFAULT_MEMORY_RECALL_LIMIT
    #: 一次召回的预算。超时按 `on_failure` 处置。
    recall_timeout_ms: int = DEFAULT_MEMORY_RECALL_TIMEOUT_MS
    #: 召回片段的 priority 下界；这是拦截器与诊断可见的来源元数据，不参与 token 预算。
    fragment_priority: int = DEFAULT_MEMORY_FRAGMENT_PRIORITY
    #: ：`degrade` = 后端故障时这一轮没有记忆、turn 照常跑（默认）；
    #: `fail` = turn `FAILED`。降级**不等于静默**，错误一定会被报出去。
    on_failure: str = DEFAULT_MEMORY_ON_FAILURE

    @property
    def critical(self) -> bool:
        """`on_failure == "fail"`。`MemoryRecall.critical` 取这个值。"""
        return self.on_failure == "fail"


@dataclass(frozen=True, slots=True)
class WorkspaceSection:
    """workspace 位置。`None` = 用实例目录下的 `workspace/`（`InstanceLayout.workspace_dir`）。"""

    root: str | None = None


@dataclass(frozen=True, slots=True)
class PluginsSection:
    """实例级插件启用状态、停止预算与逐插件配置块。

    `enabled` / `disable` / `stop_timeout_ms` 是**保留键**，`plugins` 小节里
    其余的键都是插件 id，形状校验与那条
    「保留键为什么撞不上插件 id」的理由都在 `plugin_blocks.py`。
    """

    #: 显式启用的插件 id。**不在这张表里的
    #: 候选连 manifest 都不会被读**，「安装 ≠ 启用」因此没有绕行路径。
    enabled: tuple[str, ...] = ()
    #: 显式禁用的提供方 id。它压过 `enabled`，也对内建生效（`resolve(disabled=...)`）。
    disable: tuple[str, ...] = ()
    #: 单个插件的停止预算：超时即放弃等待、记事件、继续停其余插件。
    stop_timeout_ms: int = DEFAULT_PLUGIN_STOP_TIMEOUT_MS
    #: 插件 id -> 它的 `{config, secrets}`。装配根按 id 取，取不到就给空块。
    entries: Mapping[str, PluginEntry] = blocks.NO_PLUGIN_ENTRIES

    def entry(self, plugin_id: str) -> PluginEntry:
        """取一个插件的条目。**没配过不是错误**——每个字段都有默认值的插件应当免配置可用。"""
        return self.entries.get(plugin_id, PluginEntry())


@dataclass(frozen=True, slots=True)
class ModelSection:
    """默认模型选择。provider 凭据不在配置文件里（只走环境变量，§6.7）。"""

    provider: str | None = None
    name: str | None = None


@dataclass(frozen=True, slots=True)
class RetrySection:
    """模型请求的重试策略。

    **它不是 turn 的第七项预算**：那六项说的是「一次 turn 能用掉多少」，重试说的是
    「一次失败之后怎么办」。`TurnLimits` 的 docstring 明写着不要往里加第七项。

    判定依据只有一个 `KaryviaError.retryable`——Provider 已经如实标过了
    （`model_openai/faults.py` 连 429 里的「限速」与「欠费」都分开标），kernel 不再按
    状态码猜第二遍。取消类错误**一律不重试**，那条在 `kernel/turn/retry.py` 里。
    """

    #: **总尝试次数含第一次**，因此 `1` 就是「不重试」。刻意没有第二个 `enabled` 开关：
    #: 两个旋钮表达同一件事只会让它们有机会互相矛盾。
    max_attempts: int = DEFAULT_RETRY_MAX_ATTEMPTS
    #: 指数退避的基数：第 n 次重试等 `base * 2**(n-1)`，再乘一个 [0.5, 1.0] 的抖动系数。
    #: 供应商发了 `Retry-After` 时**用它的值且不加抖动**——抖动只会把它往小了调，
    #: 而那意味着再吃一次 429。
    base_delay_ms: int = DEFAULT_RETRY_BASE_DELAY_MS
    max_delay_ms: int = DEFAULT_RETRY_MAX_DELAY_MS
    #: 空回复（既无正文也无工具调用）算不算故障。`False` 表示原样放行：终帧空正文被
    #: `emit_outbound` 丢掉，用户什么都收不到而 turn 记 `COMPLETED`。
    retry_empty_response: bool = DEFAULT_RETRY_EMPTY_RESPONSE

    def to_policy(self) -> RetryPolicy:
        """转成 `RetryPolicy`。**函数内 import**，理由同 `to_limits()`。"""
        from ..turn.retry import RetryPolicy as _RetryPolicy

        return _RetryPolicy(
            max_attempts=self.max_attempts,
            base_delay_ms=self.base_delay_ms,
            max_delay_ms=self.max_delay_ms,
            retry_empty_response=self.retry_empty_response,
        )


@dataclass(frozen=True, slots=True)
class LoggingSection:
    """事件 sink 的开关。sink 实现在 。"""

    level: str = "info"
    #: 写 `logs/events-<date>.jsonl`。
    file_enabled: bool = True


@dataclass(frozen=True, slots=True)
class KaryviaConfig:
    """整份配置。所有小节都有默认值——缺失的 `config.json` 因此是完全合法的状态。"""

    turn: TurnSection = field(default_factory=TurnSection)
    routing: RoutingSection = field(default_factory=RoutingSection)
    hooks: HooksSection = field(default_factory=HooksSection)
    context: ContextSection = field(default_factory=ContextSection)
    memory: MemorySection = field(default_factory=MemorySection)
    workspace: WorkspaceSection = field(default_factory=WorkspaceSection)
    plugins: PluginsSection = field(default_factory=PluginsSection)
    model: ModelSection = field(default_factory=ModelSection)
    retry: RetrySection = field(default_factory=RetrySection)
    logging: LoggingSection = field(default_factory=LoggingSection)

    def to_json(self) -> dict[str, JsonValue]:
        """诊断视图。实现在 `document.py`——它是这张字段表的**派生物**而不是第二份真相。

        **函数内 import** 是为了绕开 `document` → `schema` 的模块级环（`to_limits()` 的
        同一个做法）：渲染器需要小节的类型，而小节住在这里。
        """
        from .document import config_to_json

        return config_to_json(self)
