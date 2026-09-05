# 架构地图

这份文档回答三个问题：一段逻辑应该放在哪里，一条请求如何穿过系统，未来变化应从哪个
接缝进入。它不重复模块 API；精确签名以代码和测试为准。

## 1. 静态层次

```text
                           ┌──────────────┐
                           │   runtime    │  唯一组装根、CLI、生产资源门面
                           └──────┬───────┘
                    ┌─────────────┼─────────────┐
                    ▼             ▼             ▼
               ┌─────────┐  ┌─────────┐   ┌─────────┐
               │builtins │  │ plugins │   │  embed  │
               └────┬────┘  └────┬────┘   └────┬────┘
                    │             │             │
                    └──────┬──────┘             │
                           ▼                    │
                      ┌─────────┐               │
                      │   sdk   │               │
                      └────┬────┘               │
                           ▼                    │
                     ┌───────────┐ ◀────────────┘
                     │ contracts │
                     └───────────┘

runtime ───────────────▶ kernel ───────────────▶ contracts
```

箭头表示允许的依赖方向，不表示运行时调用一定按图从上到下发生。

| 层 | 拥有什么 | 明确不拥有什么 |
|---|---|---|
| `contracts` | 跨层值对象、Protocol、错误/事件枚举、纯解析与脱敏函数 | I/O、注册、配置读取、实现策略 |
| `kernel` | Turn、Registry、Routing、Plugin Loader、Config、Observability 等通用机制 | SDK manifest 类型、具体供应商、CLI 和产品策略 |
| `sdk` | 插件 manifest、注册 API、`PluginContext`、契约测试基类 | Kernel 实现、发现与装配、具体能力 |
| `builtins` | 随主包交付的默认能力 | 私有注册通道、Kernel 特权 |
| `plugins` | 独立发行的可选能力 | 宿主内部实现依赖 |
| `runtime` | 配置落盘、能力选择、实例启动/停止、资源服务、CLI | 可复用执行机制和供应商业务逻辑 |
| `embed` | 稳定、薄的嵌入入口 | 第二套装配流程 |

测试中的 R1–R5 import 守卫是这张表的可执行版本。

## 2. 目录所有权

```text
contracts/
  capability.py     能力种类、arity、能力引用
  model.py          模型请求/响应、分片、结束原因
  messages.py       入站/出站消息与附件
  context.py        Context 片段、信任级别与来源
  session.py        SessionKey、记录与存储 Protocol
  errors.py         错误码、分类、SecretStr、脱敏
  events.py         RuntimeEvent 与 EventName

kernel/
  registry/         注册批次、冲突解析、有效能力视图
  turn/             单个 turn 的执行与编排机制
  routing/          入站去重、Session 排队与命令分流
  plugins/          发现、加载计划、Host、生命周期
  config/           纯配置加载、校验、布局、Secret 引用
  observability/    EventBus、脱敏载荷、健康状态与 sinks

runtime/
  bootstrap.py      唯一组装根：启动顺序与最终实例组装
  plugin_bootstrap.py Manifest 配置、规划与统一注册策略
  startup.py        启动成功前的任务、订阅与 sink 所有权事务
  instance.py       实例生命周期与输入泵
  wiring.py         Host/Registry 到运行依赖的转换
  selection.py      从有效能力中做显式选择
  plugin_context.py 生产 PluginContext
  access/            文件、HTTP、进程等受控资源门面
  cli/               nm 命令外壳
```

判断所有权时以“谁能解释这条规则”为准。例如模型重试是所有模型共用的 Turn 机制，所以是
包裹 `Model` 的 `kernel/turn/retry.py`；某个厂商如何映射 429 是供应商实现，所以留在对应
Builtin/Plugin。

## 3. 一条入站消息的主链路

```text
Channel / nm run
       │ InboundMessage
       ▼
Instance input pump
       │
       ▼
DedupCache ──重复──▶ turn.rejected
       │
       ▼
SessionScheduler ──queue / merge / reject
       │ 持有单写者槽位
       ▼
Dispatcher ──命令──▶ RegisteredCommand
       │ model turn
       ▼
TurnOrchestrator
  ├─ load Session
  ├─ project attachment refs + assemble Context (+ optional Memory / persistent Compactor)
  ├─ run Engine
  │    ├─ TurnCompactingModel → required TURN_COMPACTOR → RetryingModel → Model
  │    ├─ ToolInvoker   → Tool capability
  │    └─ HookRouter    → Hook capabilities
  ├─ persist Transcript
  └─ publish translated RuntimeEvents
       │
       ▼
OutboundMessage → Channel.deliver / CLI
```

附件在这条链上始终是 `AttachmentRef`，Session 只持久化引用与元数据。工具请求发送文件时返回
`ToolResult.attachments`，Orchestrator 汇总到最终 `OutboundMessage`；工具不直接调用 Channel。

关键边界：

- Dispatcher 不分配 `turn_id`、不发事件；Orchestrator 是 turn 事件唯一发布者。
- Session 槽位在 `turn.started` 之前取得，保证 started 一定有后续终态。
- Engine 不认识 Session、EventBus、Manifest、配置和具体插件。
- Model 重试在首个用户可见分片之前发生；工具执行和 `MAX_TOKENS` 续写共享同一 ledger。
- 每轮 Hook 产出最终 `ModelRequest` 后，`TurnCompactingModel` 估算完整请求；超限时调用
  当前 `TURN_COMPACTOR`。摘要立即用于当前请求；只有覆盖初始 Session 连续前缀的结果在
  Turn 收口时、Transcript 追加之后持久化，不改变 Engine 事件或工具执行顺序。
- Session 存储、Context、Memory、Turn Compactor、Model、Tool、Hook 和 Command 都来自 Registry，
  不是直接实例列表。

## 4. 插件启动链路

```text
~/.nucleamind/plugins.json
       │ 先得到不导入代码也可知的 candidate id
       ▼
plugins.enabled / plugins.disable 过滤
       │ 只有启用项才读取
       ▼
runtime.inventory: parse + validate manifest
       ▼
runtime.plugin_plan + kernel.plugins.loader
  ├─ SDK 范围
  ├─ 依赖存在性/环
  ├─ config schema
  └─ 确定性 LoadPlan.order
       ▼
CapabilityHost + RegistrationBatch
  ├─ setup(ctx, api)
  ├─ 声明与实际注册逐项核对
  └─ 失败则整批丢弃
       ▼
Registry resolution
  ├─ overrides
  ├─ priority
  └─ disable / shadow
       ▼
Lifecycle activate → ready → reverse-order stop
```

加载顺序只保证依赖先 setup，不决定覆盖胜负。覆盖语义只在 Registry resolution 中解释。
`RegistrationBatch` 回滚能力表；`StartupResources` 同时接管 `setup()` 已产生的任务与订阅。
某个插件在 setup 阶段失败时，其传递依赖者不再执行 setup；无关插件仍继续加载。
任一步失败时先逆序清理这些运行资源，再释放实例锁；成功后所有权一次性交给
`AgentInstance`。CLI 回落等二次装配也必须先撤销前一次尝试。

安装链路与启动链路分开：`nm plugins install/update/uninstall` 在 Runtime 管理全局代码目录，
实例只通过 `enabled/disable` 选择已安装候选。全局变更持有管理锁并确认所有实例已停止；默认
Python 安装还会拒绝同名发行包的多版本集合。卸载不允许破坏仍被依赖的插件，跨实例配置与
全局目录通过补偿事务一起提交或恢复。

## 5. 配置与 Secret 链路

```text
schema.defaults()
      + config.json
      + NUCLEAMIND_* env
      + CLI overrides
              │
              ▼
       collect_layers()  （唯一优先级来源）
              ▼
       validate_config() （SECTION_SPECS 唯一字段表）
              │
              ├─ Config 文档始终保留 ${VAR} 字面量
              └─ resolve_secrets() → 单独的 SecretMap
                                      │
                                      ▼
                              runtime 组装具体能力
```

Kernel Config 不写磁盘。`nm init` 和配置编辑只在 Runtime 中落盘，并在写回前经过
`prepare_for_write()`。

## 6. 事件与诊断链路

```text
producer ── bus.publish(name, correlation, payload, error)
                   │
                   ├─ redact/scrub + payload bound
                   ├─ allocate sequence
                   └─ synchronous subscribers
                         ├─ ordinary subscriber
                         ├─ JsonlFileSink
                         └─ unhealthy subscriber auto-unsubscribe
```

调用点不自行构造 `RuntimeEvent`，sink 不自行补脱敏。配置解析失败发生在 Bus 创建之前，只有
`write_config_error()` 是独立例外。

## 7. 后续变化从哪里接入

| 需求 | 首选接缝 | 不应采取的捷径 |
|---|---|---|
| 新模型厂商 | `MODEL` 插件 | 在 Kernel 加 provider 分支或猜测表 |
| 新 Channel | `CHANNEL` 插件 + `nm serve` | 在 Runtime 写平台专用泵 |
| 新工具 | `TOOL` 插件和 `PluginContext` 资源服务 | 把 Runtime/Kernel 私有对象交给插件 |
| 发送 workspace 文件 | `file.send` → `ToolResult.attachments` → Channel | 让工具直接调用平台 SDK |
| 新 Context 来源 | `CONTEXT` 能力 | 把产品 prompt 写死在 Context Builder |
| 新上下文压缩策略 | `TURN_COMPACTOR` 能力 | 把算法写进 Engine 或让插件读写 Session |
| 新 Memory 后端 | `MEMORY` 能力 | 把存储策略塞进 Session Store |
| 新命令 | `COMMAND` 能力 | 给 Runtime CLI/Dispatcher 写内建特例 |
| 新横切观测 | EventBus 普通订阅者或 `HOOK` | 给 Engine 增加 EventBus 槽 |
| 新受控资源 | 扩展窄 `PluginContext` Protocol + Runtime 实现 | 把 Runtime/Kernel 对象整个交给插件 |
| 新配置值 | `SECTION_SPECS` 驱动的配置链路 | 在能力内部另读 env/config 文件 |
| 新能力种类 | 正式 SDK 兼容新增流程 | 只加 Enum 或用字符串伪装现有 kind |

详细修改清单见 [`change-guide.md`](./change-guide.md)。

## 8. 中央模块的增长规则

中央模块不是禁止增长，但每次增长都必须属于它的单一职责：

| 模块 | 可接受的增长 | 应抽出的信号 |
|---|---|---|
| `runtime/bootstrap.py` | 启动顺序、新组件连接与最终所有权转交 | 插件策略、解析或独立状态机 |
| `runtime/plugin_bootstrap.py` | Manifest 到本次注册尝试的 Runtime 策略 | 实例锁、Channel 运行或通用 Kernel 机制 |
| `kernel/turn/orchestrator.py` | 固定编排阶段之间的连接 | 某阶段已有独立状态机或多种策略 |
| `kernel/turn/context_builder.py` | 通用 Context 收集、过滤、放置与消息渲染 | 请求预算或独立压缩算法 |
| `kernel/plugins/loader.py` | 通用依赖与加载机制 | Manifest 项目规则或产品策略 |
| `kernel/config/schema.py` | 字段声明到 Config 的映射 | 新字段形状、I/O 或能力选择逻辑 |
| `sdk/api.py` / `manifest.py` | 公开协议与纯校验 | Runtime 行为、发现、网络或文件 I/O |

Kernel 文件达到约 450 行、其他层达到约 700 行时，应在同次改动中评估拆分，而不是等守卫
在 500/800 行硬拦。按职责抽模块，不按“辅助函数”与“主要函数”机械拆分。
