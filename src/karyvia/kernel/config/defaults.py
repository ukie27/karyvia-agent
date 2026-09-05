"""配置默认值里那些**镜像自别处**的字面量（技术方案 §6.7）。

职责：把 turn 六项预算、routing 六项、hooks/context 三项超时、插件停止预算与模型请求
重试四项的默认值集中成一处常量，供 `schema.SECTION_SPECS` 引用。
不负责：定义有哪些字段（`schema.py` 的那张表）、校验（`fields.py`）、读取任何来源
（`sources.py`）；本模块只有字面量，没有逻辑。

**这些常量是各自真实归属地的副本，不是第二个真相来源**：真正的定义在
`kernel/turn/limits.py`、`kernel/routing/`、`kernel/turn/{hooks,context_builder,retry}.py` 与
`kernel/plugins/lifecycle.py`、`runtime/instance.py`，每一组都有一条逐项对照的测试盯着。

**为什么不 import 那些模块**：`kernel.turn` / `kernel.routing` / `kernel.plugins` 的
`__init__` 会把 engine、调度器、registry 与 asyncio 一起拖上配置路径，而 `karyvia config show`
与诊断只需要十几个整数（`NFR-405` 给整个冷启动的预算是 300 ms）。抄一份字面量 + 一条
对照测试，是这条约束下唯一诚实的做法——两处不一致时测试会响，而不是用户的实例会。

默认值单独成模块，使字段表保持聚焦，也避免 `schema.py` 因纯常量接近 Kernel 的 500 行
上限。新增字段仍应先在 `SECTION_SPECS` 声明，再按需要引用这里的机制默认值。
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "DEFAULT_CHANNEL_CONCURRENCY",
    "DEFAULT_COMMAND_PREFIX",
    "DEFAULT_CONTEXT_PROVIDER_TIMEOUT_MS",
    "DEFAULT_TURN_COMPACTOR",
    "DEFAULT_TURN_COMPACTOR_TIMEOUT_MS",
    "DEFAULT_DEDUP_CAPACITY",
    "DEFAULT_DEDUP_TTL_MS",
    "DEFAULT_INTERCEPTOR_TIMEOUT_MS",
    "DEFAULT_MAX_ITERATIONS",
    "DEFAULT_MAX_TOOL_CALLS_PER_TURN",
    "DEFAULT_MEMORY_FRAGMENT_PRIORITY",
    "DEFAULT_MEMORY_ON_FAILURE",
    "DEFAULT_MEMORY_RECALL_LIMIT",
    "DEFAULT_MEMORY_RECALL_TIMEOUT_MS",
    "DEFAULT_OBSERVER_TIMEOUT_MS",
    "DEFAULT_PLUGIN_STOP_TIMEOUT_MS",
    "DEFAULT_QUEUE_MAX_SIZE",
    "DEFAULT_RETRY_BASE_DELAY_MS",
    "DEFAULT_RETRY_EMPTY_RESPONSE",
    "DEFAULT_RETRY_MAX_ATTEMPTS",
    "DEFAULT_RETRY_MAX_DELAY_MS",
    "DEFAULT_SESSION_CONCURRENCY",
    "DEFAULT_TOOL_RESULT_MAX_BYTES",
    "DEFAULT_TOOL_TIMEOUT_MS",
    "DEFAULT_TURN_TIMEOUT_MS",
    "MEMORY_ON_FAILURE_CHOICES",
    "SESSION_CONCURRENCY_CHOICES",
]

#: turn 六项预算（第六项 `context_max_tokens` 无默认值）。**与 `kernel/turn/limits.py` 的
#: `DEFAULT_*` 必须逐一相等**，由 `test_turn_defaults_match_the_limits_module` 盯着。
DEFAULT_MAX_ITERATIONS: Final = 16
DEFAULT_MAX_TOOL_CALLS_PER_TURN: Final = 48
DEFAULT_TOOL_TIMEOUT_MS: Final = 120_000
DEFAULT_TOOL_RESULT_MAX_BYTES: Final = 65_536
DEFAULT_TURN_TIMEOUT_MS: Final = 900_000

#: 路由六项。前五项与 `kernel/routing/`、Channel 总量上限与 `runtime/instance.py` 的
#: 同名 `DEFAULT_*` 相等，由配置测试逐一对照。
DEFAULT_COMMAND_PREFIX: Final = "/"
DEFAULT_SESSION_CONCURRENCY: Final = "queue"
DEFAULT_QUEUE_MAX_SIZE: Final = 32
DEFAULT_DEDUP_CAPACITY: Final = 4096
DEFAULT_DEDUP_TTL_MS: Final = 600_000
#: 单条 Channel 的总在途消息上限。它只防止 Runtime 产生无界任务，不参与 Session 调度。
DEFAULT_CHANNEL_CONCURRENCY: Final = 64

#: `session_concurrency` 的合法取值，与 `routing.ConcurrencyPolicy` 的三个取值同名。
SESSION_CONCURRENCY_CHOICES: Final = ("queue", "merge", "reject")

#: Hook 与 Context Provider 的三项超时（技术方案 §6.6、§10.2 第 7 步 b）。**与
#: `kernel/turn/hooks.py` 与 `context_builder.py` 的同名 `DEFAULT_*` 必须逐一相等**，
#: 由 `test_orchestration_defaults_match_the_turn_package` 盯着。
DEFAULT_OBSERVER_TIMEOUT_MS: Final = 2_000
DEFAULT_INTERCEPTOR_TIMEOUT_MS: Final = 5_000
DEFAULT_CONTEXT_PROVIDER_TIMEOUT_MS: Final = 3_000
DEFAULT_TURN_COMPACTOR: Final = "basic"
DEFAULT_TURN_COMPACTOR_TIMEOUT_MS: Final = 120_000

#: 单个插件的停止预算（`EDG-104`）。**与 `kernel/plugins/lifecycle.py` 的
#: `DEFAULT_STOP_TIMEOUT_MS` 必须相等**，由
#: `test_the_stop_budget_default_matches_the_config_schema` 盯着。
DEFAULT_PLUGIN_STOP_TIMEOUT_MS: Final = 5_000

#: 长期记忆的召回四项（`MEM-003`）。**与 `kernel/turn/memory.py` 的同名 `DEFAULT_*`
#: 必须逐一相等**，由 `test_memory_defaults_match_the_turn_package` 盯着。理由与上面那三个
#: 超时完全相同：`kernel/config/` 不得 module-level import `kernel.turn`（那会把 engine 与
#: asyncio 拖上配置路径，`NFR-405` 的冷启动预算 300 ms）。
DEFAULT_MEMORY_RECALL_LIMIT: Final = 5
DEFAULT_MEMORY_RECALL_TIMEOUT_MS: Final = 3_000
DEFAULT_MEMORY_FRAGMENT_PRIORITY: Final = 100
DEFAULT_MEMORY_ON_FAILURE: Final = "degrade"

#: `memory.on_failure` 的合法取值，与 `kernel/turn/memory.py::MEMORY_ON_FAILURE_CHOICES`
#: 同源同序。
MEMORY_ON_FAILURE_CHOICES: Final = ("degrade", "fail")

#: 模型请求重试四项（`MOD-003`）。**与 `kernel/turn/retry.py` 的同名 `DEFAULT_*`
#: 必须逐一相等**，由 `test_retry_defaults_match_the_turn_package` 盯着。
#:
#: `DEFAULT_RETRY_MAX_ATTEMPTS` 是**总尝试次数含第一次**，因此 `1` 就是「不重试」——
#: 没有第二个 `enabled` 开关，两个旋钮表达同一件事只会让它们有机会互相矛盾。
DEFAULT_RETRY_MAX_ATTEMPTS: Final = 3
DEFAULT_RETRY_BASE_DELAY_MS: Final = 500
DEFAULT_RETRY_MAX_DELAY_MS: Final = 8_000
DEFAULT_RETRY_EMPTY_RESPONSE: Final = True
