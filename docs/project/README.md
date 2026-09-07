# Karyvia 当前状态与接手入口

> 这里描述当前事实，不记录逐 PR 流水账；精确变化以 Git 为准。

## 结论

Karyvia 已经具备一套可运行、受架构守卫约束的 Agent Kernel 骨架。它不再是“等待以后
填充的目录结构”：从配置、插件发现和事务加载，到 Registry、Session 并发、Context、Turn
执行、工具调用、事件、持久化和 CLI，主链路已经贯通。

当前骨架基本符合最初的极简方向：Kernel 负责机制，具体能力由 Builtin 或插件负责；内建
能力没有私有注册通道，官方插件与第三方插件走同一套 SDK、Manifest、Host、Registry 和
生命周期。仓库不包含第二套旧实现、WebUI 或旧命名兼容层。

现在最重要的工作不是继续补齐所有可能的能力，而是维护这些清晰的边界，在真正出现需求时
沿现有接缝演进。三个可能需要未来专项设计的方向已经明确记录，但当前不应为它们放入半成品
接口：插件发起子 Turn/能力组合、跨 Channel 的主体与 Memory 作用域、多模态消息块。

## 当前能力基线

### Kernel 与 Runtime

- `contracts/`：公开消息、执行、能力、错误、事件、Session、附件和 opaque 块契约。
- `kernel/registry/`：能力注册、覆盖和冲突解析的唯一实现。
- `kernel/turn/`：取消、预算、Context、压缩、Memory 召回、模型重试、自动续写、工具调用、
  Transcript 和 Orchestrator。
- `kernel/config/`：实例布局、四层配置、schema、Secret 引用和实例锁；自身不写文件。
- `kernel/routing/`：去重、Session 单写者调度和命令分流。
- `kernel/plugins/`：发现、两阶段加载、事务注册、依赖排序和生命周期。
- `kernel/observability/`：同步事件总线、脱敏、健康状态和 sinks。
- `runtime/`：唯一组装根、插件装配策略、启动资源事务、生产 `PluginContext`、资源门面、
  配置写入、诊断与 CLI。
- `embed/`：嵌入式 Python 薄门面。

### 默认与可选能力

九个内建能力包：JSONL Session、基础 Context、Turn 内上下文压缩、OpenAI-compatible Model、
文件读写工具、文件投递工具、Shell 工具、核心命令和 CLI 入口。`file.send` 只把 workspace 文件附加到当前回复，
实际上传仍由 Channel 完成。

七个官方独立插件：OpenAI API、Anthropic、Feishu、Web、MCP、Memory、Cron。
插件安装方式与当前清单见 [`../getting-started.md`](../getting-started.md)。

### 对外表面

- 包版本：`0.3.0`（alpha）。
- SDK 版本：`5.0.0`；5.0 删除独立 `COMPACTOR`，统一由请求级 `TURN_COMPACTOR` 压缩，并在
  Turn 收口时持久化可精确映射的 Session 前缀。外部插件故障统一隔离并进入
  诊断；宿主发布的内建基线装配错误仍直接拒绝启动。
- `KaryviaAPI` 与 `CapabilityKind` 当前一一覆盖十类能力。
- `karyvia init`、`karyvia run`、`karyvia serve`、`karyvia config show`、`karyvia session`、
  `karyvia plugins`、`karyvia capabilities` 已可用。

## 架构是否仍然极简

“极简”在这里指所有权最小，而不是源文件数量最少。当前 Kernel 中的 Registry、Plugin
Loader、Turn、Routing、Config 和 Observability 都是宿主机制；删除其中任何一项，插件就
无法以统一方式安全装载、执行或诊断。因此它们属于骨架，不属于多余功能。

下列能力已经保持在 Kernel 外：具体模型厂商、Channel 协议、长期 Memory 策略、Web/Search、
MCP Server 适配、Cron 调度、OpenAI API 兼容服务。内建能力也只是随主发行包交付
的默认插件，不享有架构特权。

需要持续警惕的不是“Kernel 目录有多少文件”，而是：

- 某个具体产品策略是否被写入通用 Turn 或配置机制；
- Runtime 组装根是否开始承担可复用业务机制；
- Builtin 是否绕过公开 SDK/Registry 获得特权；
- 为假想需求增加的接口是否冻结了错误抽象；
- 中央文件是否因多个职责而接近行数上限。

这些判断与代码所有权见 [`architecture-map.md`](./architecture-map.md)。

## 当前稳定边界

下面这些“锁死”是有意的兼容承诺，不是架构缺陷：

- `SessionKey.storage_id` 与 Session JSONL 持久化格式；
- 集中式错误码、错误分类和构造时脱敏；
- 能力冲突/覆盖的确定性语义与事务注册；
- 配置优先级和 Secret 引用不落明文；
- Turn 的取消检查点、预算、单终态和工具副作用边界；
- 同 Session 单写者与去重优先的准入顺序；
- SDK 5.x 已发布的名字、签名和 Manifest 语义；
- 插件依赖方向与 Runtime 作为唯一组装根。

它们可以演进，但需要显式版本或迁移，而不能在普通重构里顺手改变。完整分类见
[`evolution-boundaries.md`](./evolution-boundaries.md)。

## 已留好的扩展接缝

大多数后续需求不需要重做骨架：

- 新模型、Channel、Tool、Context、Memory、Command、Hook、Compactor 可直接作为能力注册。
- 新资源访问通过 `PluginContext` 的窄门面增加，并由 Runtime 提供生产实现。
- 新横切机制可包装现有 `Model`、Tool 或事件订阅者，不必给 Engine 增加依赖槽。
- 新配置字段从 `SECTION_SPECS` 派生默认值、校验和 JSON Schema。
- 新事件通过集中 `EventName` 与唯一发布路径增加。
- 新能力种类可以做 SDK 的兼容新增，但需要同步十余处映射和契约测试，不能只加一个 Enum。

实际改动清单见 [`change-guide.md`](./change-guide.md)。

## 尚未设计、但没有被封死的方向

### 插件组合与子 Turn

插件目前可以注册能力、观察实例和取消 Turn，但没有公开的宿主执行门面来安全发起一个新的
Turn。未来若 Multi-Agent、Workflow 或 Automation 真的需要它，应设计受取消、预算、
递归深度和关联 ID 约束的 `TurnGateway`/`AgentExecutor` 类门面，而不是把
`TurnOrchestrator` 暴露给插件。

### 主体身份与 Memory 作用域

当前 SessionKey 足以隔离现有会话，但不能自然表达“同一人在多个 Channel/Conversation
之间共享哪些记忆”。未来应先定义稳定的主体身份映射和 `MemoryScopeKey`/请求对象，再扩展
Memory 能力；不能改写已经持久化的 `SessionKey.storage_id`。

### 多模态输入与输出

附件引用现在会贯穿 InboundMessage、Turn、Session 和历史重放；Agent 也可用 `file.send`
把 workspace 文件交给统一出站链。模型当前看到的是附件元数据投影，消息主体仍以文本为中心。
未来若需要原生图像、音频或文档输入，应新增可演进的内容块联合类型，并明确模型降级、
Transcript 和存储版本，而不是扩张 `AttachmentRef` 的含义。

以上三项只是“设计闸门”，不是待立刻实现的 TODO。触发条件和禁止的捷径见
[`evolution-boundaries.md`](./evolution-boundaries.md)。

## 维护性重点

现阶段优先级应是：

1. 保持层次、唯一真相来源和防漂移测试有效。
2. 在功能需求出现时沿公开接缝实现最小插件，而不是提前搭大框架。
3. 中央文件接近阈值时按职责拆分，尤其关注 SDK manifest/API、Orchestrator、
   Context Builder、配置 schema 和插件 loader；`runtime/bootstrap.py` 已把插件装配策略拆到
   `plugin_bootstrap.py`，后续不要重新塞回去。
4. 保持模块 docstring 说明“负责/不负责”，让代码所有权可快速判断。
5. 公开契约、配置、事件或持久化格式发生变化时，同步文档和迁移说明。

仓库门禁规定 Kernel 单文件不超过 500 行、其他层和插件不超过 800 行，Engine 另有 400 行
上限；行数是拆分信号，不应通过压缩代码或提高阈值解决。

## 验证基线

日常先运行与改动相邻的测试，再按风险扩大：

```bash
mkdir -p .pytest-tmp
.venv/bin/python -m pytest tests/architecture -q --basetemp=.pytest-tmp/architecture
.venv/bin/python -m pytest tests/contracts tests/kernel tests/sdk -q \
  --basetemp=.pytest-tmp/core
.venv/bin/python -m ruff check src plugins examples tests
.venv/bin/python -m basedpyright
```

完整 E2E 需要七个官方插件登记到隔离的 Karyvia 全局插件目录，确保安装目录发现链路可用。
架构测试、类型检查和插件清单守卫不能因为开发环境缺依赖而跳过。

## 阅读顺序

新接手项目建议按以下顺序：

1. [`architecture-map.md`](./architecture-map.md)：代码在哪里、主链路怎么走。
2. [`evolution-boundaries.md`](./evolution-boundaries.md)：哪些能加、哪些必须迁移。
3. [`change-guide.md`](./change-guide.md)：动手时的检查清单。
4. [`../../AGENTS.md`](../../AGENTS.md)：仓库执行规则。
5. 目标模块的 docstring、相邻测试和相关架构约束。

详细阶段历史不作为理解当前架构的前置条件，需要追溯时查看 Git。
