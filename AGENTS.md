# NucleaMind 仓库开发指南

本文件只保留当前仍然有效、会影响代码判断的规则。项目现状、架构地图、常见改动路径和历史
分别见：

- [`docs/project/README.md`](./docs/project/README.md)：当前状态与接手入口
- [`docs/project/architecture-map.md`](./docs/project/architecture-map.md)：层次、运行链路与所有权
- [`docs/project/change-guide.md`](./docs/project/change-guide.md)：常见改动需要触碰哪些位置
- [`docs/project/evolution-boundaries.md`](./docs/project/evolution-boundaries.md)：哪些已冻结、哪些可扩展、哪些以后才设计
- [`docs/project/history.md`](./docs/project/history.md)：D00–D53 里程碑摘要

更细的正式约束仍以 [`docs/project/technical-design.md`](./docs/project/technical-design.md)、
[`docs/project/requirements-analysis.md`](./docs/project/requirements-analysis.md) 和测试守卫为准。

## 1. 目标与边界

NucleaMind 是一个轻量、模块化、可扩展的 Agent Kernel。核心只保留运行一个 Agent 所需的
机制：消息与公开契约、Turn 执行、Session 并发、Context 组装、能力注册、插件加载、基础
配置和可观测性。

模型供应商、Channel、Memory、Web、Image、MCP、Cron 等具体能力应放在 `builtins/` 或
独立插件中。判断一项代码是否属于 Kernel 时，问两个问题：

1. 去掉所有可选能力后，宿主还能否正确装载能力并完成一个 turn？
2. 这段代码定义的是所有实现都必须遵守的机制，还是某一种实现的策略？

只有第一个答案为“不能”且第二个答案为“机制”时，才应进入 Kernel。不要为了尚未实现的
功能预埋空接口；先确认现有接缝无法表达，再按演进流程修改公开表面。

当前产品范围刻意收窄：不恢复旧 `legacy/`、WebUI 或 nanobot 兼容层；不在 Kernel 内堆新的
供应商、Channel 和工具；OpenClaw 等更高层产品属于独立包，不反向污染 Kernel。

## 2. 仓库层次与依赖方向

```text
src/nucleamind/
├── contracts/   # 公开数据契约；纯类型与纯函数
├── kernel/      # 通用机制；只依赖 contracts 与 kernel 内部
├── sdk/         # 插件作者的唯一宿主 API；只依赖 contracts/sdk
├── builtins/    # 默认能力；身份与外部插件相同
├── runtime/     # 唯一组装根、CLI、生产 PluginContext
└── embed/       # 嵌入式薄门面
plugins/         # 七个官方独立插件发行包
examples/plugins/# 最小教学插件
tests/           # 按层镜像；integration/e2e 验证组装后的骨架
```

依赖规则由 AST 测试强制执行：

- `contracts/` 不依赖任何 NucleaMind 内部层。
- `kernel/` 不导入 `sdk/`、`builtins/`、`runtime/` 或 `embed/`。
- `sdk/` 不导入 `kernel/`、`builtins/`、`runtime/` 或 `embed/`。
- `builtins/` 与插件只依赖 `sdk` 和 `contracts`，不享有私有特权。
- `runtime/` 是唯一允许把各层组装起来的位置。
- `embed/` 保持薄，只依赖公开契约与 `runtime` 门面。

新增 import 前先判断依赖箭头，而不是等测试报错。详细图见
[`architecture-map.md`](./docs/project/architecture-map.md)。

## 3. 已发布且不得顺手改动的表面

以下内容不是普通内部实现：

- `SessionKey.storage_id()` 是已发布的持久化编码，必须可逆且无碰撞。
- `ErrorCode` 与 `CODE_CATEGORIES` 是错误码唯一来源；禁止散落错误码字面量。
- `NucleaError.category` 由错误码推导，调用方不能另传一份分类。
- `contracts.errors.redact()` / `scrub()` 在数据构造时脱敏；不要把责任推给日志 sink。
- `contracts.SecretStr` 是唯一密钥包装类型；明文只通过 `reveal()` 短暂取得。
- SDK 当前为 `3.1.0`。`sdk.__all__`、`sdk.testing.__all__`、
  `CapabilityKind`、`NucleaAPI` 和 manifest schema 都受兼容承诺约束。
- Session JSONL 格式是持久化契约，修改必须先设计迁移。

公开表面优先做纯新增。需要破坏性修改时，不要写长期双读兼容垫片；明确版本、迁移边界、
失败方式和移除时间，再集中实施。流程见 [`change-guide.md`](./docs/project/change-guide.md)。

## 4. 能力注册与插件运行时

- `kernel/registry/resolution.py` 是能力冲突、覆盖和遮蔽语义的唯一来源。覆盖不由加载顺序
  决定。
- 注册必须经过 `RegistrationBatch`，保证插件 `setup()` 失败时整批回滚。
- `CapabilityHost` 是唯一 `NucleaAPI` 实现；内建和外部插件走同一条注册路径。
- manifest 的 `capabilities` 是有约束力的全集：未声明却注册、声明却未注册都必须失败。
- `overrides` 以原始字符串跨层传递，统一由 `contracts.parse_capability_target()` 解析。
- manifest 通常不要显式写默认 `priority=100`；内建基准为 0，只有确有排序意图时才写。
- 生产发现只读取 `~/.nucleamind/plugins.json` 中由 `nm` 管理的全局候选；不扫描环境，
  不从实例配置读取代码路径。manifest 解析和项目规则在 `runtime/inventory.py`。
- 安装、更新、卸载是全局操作，要求所有实例停止；实例只管理启用、配置和业务状态。
  全局数据直接位于 `~/.nucleamind/`，不要增加 `global/` 中间目录。
- 未启用插件不得被导入。entry point 名、候选名和 manifest `id` 必须一致。
- `kernel/plugins/loader.py` 只负责依赖、排序和 schema 等机制；项目级判定在
  `runtime/plugin_plan.py`。
- `LoadPlan.order` 只定义加载顺序。生命周期停止使用其逆序，不要另算一套顺序。
- `state_version` 不匹配当前直接拒绝加载；尚未设计热迁移，不要静默兼容。
- 插件在 `setup()` 中通过 `ctx.on_start()`、`ctx.add_cleanup()` 和 `ctx.spawn_task()` 登记
  激活、资源释放和后台任务；后台任务只在激活后启动，并由生命周期管理器停止。
- `setup()` 的 Registry 写入由 `RegistrationBatch` 回滚，任务与事件订阅由 Runtime 的
  `StartupResources` 回滚；启动失败与二次装配都必须同时覆盖这两类副作用。

插件是受信任的同进程 Python 代码，安装并启用即完全信任。`PluginContext` 资源门面用于提供
统一的工作区、网络、进程与密钥服务，不是权限系统或安全沙箱。不要声称它能阻止插件直接
调用 Python/OS API。

插件应继承 `sdk.testing` 的契约测试基类，并同时加入 `inspect.signature` 守卫；Protocol 的
运行时检查只验证属性存在，不能证明签名一致。官方插件还必须进入 basedpyright 和 CI 的
全局安装清单。

## 5. Turn 与编排不变量

### Engine

- `kernel/turn/engine.py` 是纯事件循环，`EngineDeps` 只有 `model/tools/hooks/limits` 四个槽。
- Engine 只分发 `ENGINE_HOOKS` 的四个 hook，并且事件流恰好以一个终态结束。
- 取消统一使用 `CancelToken` 和 `Checkpoint`，不要用 `asyncio.CancelledError` 代替业务取消。
- 限额只有 `TurnLimits` 的六项；越界结果由 `LIMIT_OUTCOMES` 决定。
- 每个 turn 使用一个 `BudgetLedger`，工具预算必须在执行副作用之前扣除。
- 模型重试通过 `RetryingModel` 包装 `EngineDeps.model`，装配点在
  `orchestration.engine_deps()`。只有首个实质输出交给用户前才可重试，判据是
  `ErrorCategory`，不是错误对象的 `retryable` 标志。
- `MAX_TOKENS` 续写复用同一消息序列和 ledger，最多三次；不得重新执行已完成的工具。

### Orchestrator

- turn 事件只有 `orchestrator.py` 一个发布点，事件翻译只有 `translation.py` 一张表。
- `before_model_request` 由 Engine 每次模型迭代分发；编排层不得再发一次。
- 准入顺序固定为：去重 → Session 并发 → 分流。被拒消息只发 `turn.rejected`，不能先发
  `turn.started`。
- `trust=SYSTEM` 是进入系统指令位置的唯一凭据；不可信文本的包装由契约层
  `as_model_text()` 完成。
- Context 确定性裁剪按既定优先级工作；只剩系统段和当前输入仍超预算时抛
  `INPUT_TOO_LARGE`，不要伪装成已压缩成功。
- `ToolInvoker.invoke()` 约定不抛，并必须在 `timeout_ms + grace` 内返回。超时先请求子令牌
  取消，宽限后仍不返回则登记孤儿和未知副作用；不要直接 `task.cancel()`。

不要给 Engine 增加事件总线、Session、配置或插件对象。新横切行为优先包在既有依赖外层，
或放在 Orchestrator/Runtime 的组装边界。

## 6. Routing、配置与可观测性

### Routing

- 入站顺序始终是去重 → 并发 → 分流。
- 同一 Session 同时最多一个写者；`queue/merge/reject` 只决定拿不到槽位时怎么办。
- FIFO 由显式票据保证，不依赖 `asyncio.Lock` 的实现细节。
- 命令名和别名在同一命名空间，冲突在启动期由 `build_command_index()` 判定。
- Dispatcher 不发布事件、不分配 `turn_id`。
- 命令 handler 的普通异常折为 `REJECTED`，只暴露异常类型名；`BaseException` 放行。

### Config

- 配置优先级只在 `sources.collect_layers()` 定义：default < `config.json` < env < CLI。
- 字段只在 `schema.SECTION_SPECS` 声明；字段形状积木只在 `fields.py`。
- 默认值常量放 `defaults.py`；`json_schema.py` 是派生物，不是第二份真相。
- 新字段还必须进入配置 dataclass、`validate_config()` 显式构造和
  `docs/configuration.md`。有守卫验证声明值真的到达最终对象。
- `kernel/config/` 只解析和渲染，不写文件。首次写入、原子修改分别由
  `runtime/first_run.py` 与 `runtime/config_edit.py` 负责。
- 顶层只额外放行具名 `$schema`，不能放行任意 `$*`。
- 配置冷路径不得 module-level import `kernel.turn`、`kernel.routing`、
  `kernel.plugins`，也不引入 pydantic。
- PID 探测只用 `process.process_is_alive()`；Windows 下不要用 `os.kill(pid, 0)`。
- `${VAR}` 解析后的明文只进入 `SecretMap`，配置树继续持有引用字面量。写回前调用
  `prepare_for_write()`。

### Observability

- 事件只走 `bus.publish(...)`，不要自行构造 `RuntimeEvent`。Bus 负责 sequence 与脱敏。
- `publish()` 同步、不抛、不可 `await`；异步订阅者自行放入有界队列。
- 脱敏只有 `prepare_payload()` 一条路径，调用点和 sink 不再各写一套。
- Sink 是普通订阅者，不反向依赖配置布局。
- 配置加载失败时 Bus 尚不存在，只允许专用 `write_config_error()` 写诊断。

## 7. Builtins、插件与 Runtime 的所有权

八个内建子包只是默认插件：`session_jsonl`、`context_basic`、`model_openai`、`tools_fs`、
`tools_file`、`tools_shell`、`commands_core`、`cli_entry`。它们与外部插件共用 manifest、Host、
Registry、冲突解析和生命周期，不得 import Kernel 私有实现获得特权。

冻结的基础工具名是：`fs.read`、`fs.write`、`fs.edit`、`fs.list`、`fs.grep`、`shell.exec`。
文件和 shell 能力必须经过 workspace 边界与资源服务；供应商选择、凭据解析和启用判定属于
Runtime 配置与组装，不进入模型实现。

`runtime/bootstrap.py` 是唯一组装根，只保留启动顺序与最终实例组装；Manifest 配置、
规划和统一注册策略在 `runtime/plugin_bootstrap.py`，启动期资源所有权在
`runtime/startup.py`。某项逻辑若同时需要认识 SDK manifest、Kernel 实现和具体能力，它通常
属于 Runtime；如果 Runtime 文件开始承担可复用机制，应先抽到所属 Kernel 模块。

## 8. 测试与质量门禁

使用仓库虚拟环境，不依赖系统 Python：

```bash
# Linux / macOS
.venv/bin/python -m pytest tests/architecture -q --basetemp=.pytest-tmp/architecture
.venv/bin/python -m pytest -q --basetemp=.pytest-tmp/full

# Windows
.venv\Scripts\python.exe -m pytest tests\architecture -q --basetemp=.pytest-tmp\architecture
.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp\full
```

沙箱内运行 pytest 前先创建仓库内 `.pytest-tmp/`。临时目录不得指向宽泛路径。

常用门禁：

```bash
.venv/bin/python -m ruff check src plugins examples tests
.venv/bin/python -m basedpyright
.venv/bin/python -m pytest tests/architecture tests/contracts tests/kernel tests/sdk -q \
  --basetemp=.pytest-tmp/core
```

完整插件测试要求相应官方插件以 editable 方式装入当前虚拟环境；不要因为本地缺少 entry
point 就删除或放宽 E2E 断言。

结构限制：

- `kernel/` 单个 Python 文件不超过 500 行，其他新层与插件不超过 800 行。
- `engine.py` 额外受 400 行上限约束。
- 圈复杂度不超过 12，函数语句数由 ruff `PLR0915` 守卫。
- 若文件接近上限，按职责抽出纯模块；不要压缩排版、合并无关函数或提高阈值。

注释与 docstring 也是长期代码的一部分：

- 模块 docstring 说明“负责什么、不负责什么、有哪些反直觉不变量”，不要记录交付阶段。
- 注释解释当前代码无法直接表达的原因、安全边界和兼容约束，不逐行复述显而易见的操作。
- `Dxx`、某个 PR、曾经如何实现、当时踩过什么等历史移到 `docs/project/history.md` 或 Git；
  运行代码只保留从历史中提炼出的当前结论。
- 需求编号（如 `EDG-304`）只在它能帮助定位稳定契约时保留，不能代替自然语言解释。
- 已经兑现的“以后补”“暂时缺口”和过期 TODO 必须删除或改写成当前行为；不要让读者判断
  注释说的是现在还是过去。
- 若代码需要大段历史才能解释，先尝试改善命名、拆分职责或把决策移到架构文档，再留下最短
  的局部理由。

`tests/integration/` 的 Fake 只能位于能力边界。Registry、HookRouter、ToolExecutor、
Dispatcher、SessionScheduler、DedupCache、EventBus 和 TurnOrchestrator 应使用生产实现；能力
必须经 `RegistrationBatch` 注册后从 Registry 取回。集成测试禁止真实网络。

## 9. 修改工作的最小闭环

开始前：

1. 读目标模块 docstring、相邻测试和 [`change-guide.md`](./docs/project/change-guide.md)。
2. 用 `rg` 找唯一真相来源和镜像清单，不凭文件名猜测。
3. 判断改动是内部重构、兼容新增、持久化迁移还是破坏性 SDK 变更。

实施时：

1. 直接写入最终层次，不建临时实现层，不恢复兼容垫片。
2. 复用集中契约、错误码、脱敏、注册、配置和事件路径。
3. 同步更新派生文档与字面量快照。
4. 对高风险不变量先写或保留失败测试，再改实现。

结束前：

1. 先跑目标测试，再跑相邻层，最后按风险跑架构/集成/E2E。
2. 运行 ruff 和 basedpyright；说明任何因环境缺失而未运行的范围。
3. 更新当前文档，不留下“以后补”的临时说明。
4. 保持用户已有改动；不使用破坏性 Git 命令。

## 10. 文档与历史

- 活跃规则只放在本文件和 `docs/project/` 的四个入口文档，避免把阶段流水账复制到每个文件。
- 用户文档描述实际可用行为；能力未实现前不写占位使用说明。
- 配置、CLI、插件安装列表和插件代码示例已有防漂移测试，改代码时必须同步更新。
- 上游 nanobot 只保留在 `references/nanobot/` 作为只读参考；不要从那里 import 或复制旧命名。
- 历史决策通过 Git 和 [`history.md`](./docs/project/history.md) 查询；不要把 D 编号历史重新塞回
  活跃开发规则。
- 安全边界详见 [`.agent/security.md`](./.agent/security.md)，常见陷阱见
  [`.agent/gotchas.md`](./.agent/gotchas.md)，依赖守卫摘要见 [`.agent/design.md`](./.agent/design.md)。
