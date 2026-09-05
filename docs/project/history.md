# 里程碑摘要

本文只用于回答“现在的骨架是怎样形成的”。它不是当前开发清单，也不应被复制回
`AGENTS.md`。精确提交、当时的测试和完整讨论以 Git 历史为准。

## D00–D06：最终目录与公开骨架

- **D00**：迁到 `src/` 布局，建立 contracts/kernel/sdk/builtins/runtime/embed 六层目标结构。
- **D01**：建立 import、文件规模、复杂度和 CI 架构守卫。
- **D02–D04**：完成公开 contracts，集中消息、Session、错误、事件和能力协议。
- **D05**：建立插件 SDK 与 `sdk.testing` 表面。
- **D06**：完成 Capability Registry、冲突与覆盖解析，阶段 1 收口。

这一阶段决定了最重要的依赖方向：Kernel 不认识 SDK，插件只依赖 SDK/Contracts，Runtime
负责把两者组装起来。

## D07–D15：Turn Kernel 与支撑设施

- **D07**：为旧实现建立迁移行为基线（在 D31/D35 完成迁移后删除）。
- **D08**：取消令牌与六类预算。
- **D09**：纯 Turn Engine、事件流、调度与 folding，阶段 2 收口。
- **D10–D12**：配置/实例布局、Secret 与凭据、可观测性，阶段 3 收口。
- **D13**：入站分流、去重和 Session 单写者并发。
- **D14**：Turn Orchestrator、Context、Hook、Invoker、Transcript 与事件翻译。
- **D15**：生产机制贯通的骨架集成测试，阶段 4 收口。

这一阶段形成了今天仍稳定的主链路：去重 → Session 调度 → 分流 → Context → Engine →
Transcript/事件/出站消息。

## D16–D24：内建能力与可运行产品

- **D16**：内建能力也通过 Plugin Host/Registry 加载，并建立能力契约测试。
- **D17–D21**：依次交付 JSONL Session、基础 Context、OpenAI-compatible Model、文件工具和
  Shell 工具。
- **D22**：核心命令和 Runtime introspection，`PluginContext` 增加实例观察与 Turn 控制。
- **D23**：CLI、组装根、实例生命周期、embed 门面和唯一 `karyvia` 入口，阶段 5 收口。
- **D24**：首次运行 scaffold、JSON Schema、`karyvia init` 与 E2E，阶段 6 收口。

至此项目不再只是库结构，而是能够初始化实例、执行 turn 并诊断有效能力的独立程序。

## D25–D30：完整 Plugin Runtime

- **D25**：entry point/目录插件发现和 `plugins.enabled`。
- **D26**：权限声明、账本和生产 `PluginContext` 资源门面。
- **D27**：两阶段加载、依赖计划和事务注册；Builtin/Plugin 合并到同一次 wiring。
- **D28**：六阶段插件生命周期、反向停止和每插件停止预算。
- **D29**：`karyvia plugins`、`karyvia capabilities`、配置原子编辑与诊断输出。
- **D30**：两个独立示例插件、插件开发文档、runtime E2E 和真正的能力 disable，阶段 7 收口。

## D31–D40：移除旧实现，能力全面插件化

- **D31**：删除旧 Agent/CLI/WebUI/Gateway/API 等路径，以 OpenAI API 插件和通用 `karyvia serve`
  替代，阶段 8 收口。
- **D32**：Anthropic 原生 Model 插件，移除宿主 anthropic 依赖。
- **D33**：Channel fanout 放开跨 conversation 并发，曾交付 Discord 插件并删除旧实现；
  当前产品不需要该平台后已整包删除，通用 Channel 并发骨架保留。
- **D34**：Feishu Channel 插件。
- **D35**：删除全部 `legacy/`、`tests/legacy/` 和 `webui/`，收窄产品范围。
- **D36–D38**：曾交付 Web、Image、MCP Tool 插件；Image 后因当前产品不需要而整包删除，
  通用 Tool、Artifact 与附件接缝保留；同期增加 `CapabilityDecl.namespace`。
- **D39**：Memory 插件，一份 manifest 组合 Memory/Context/Tool/Command 能力；SDK 增加 Memory
  契约测试基类。
- **D40**：Cron/Automation 插件，仅使用现有 Channel/Tool/Command 接缝，Kernel 零修改。

D40 是插件骨架成熟的重要证明：一个有调度器、工具和命令的复合能力无需修改 Kernel。

## D41–D52：冻结 SDK 后的缺口收口

- **D41**：官方插件纳入 basedpyright 和 CI 清单防漂移守卫，并修复 EventHandler 类型缺陷。
- **D42**：SDK `1.0.0`：`ToolResult.trust`、二进制 FileAccess、受限 HttpAccess、Manifest JSON
  Schema。
- **D43**：`channel.delivery_failed`，统一 Channel 投递失败语义。
- **D44**：Kernel 正式消费 Memory 能力，并拆出配置 sections 与 Runtime selection。
- **D45**：Opaque 块保真回放，SDK `1.1.0`。
- **D46**：补齐 getting started、配置、CLI、部署用户文档和防漂移测试。
- **D47**：ToolResult 附件贯通到出站消息与 Channel，SDK `1.2.0`。
- **D48**：模型瞬时失败重试首次接线，通过模型包装层实现。
- **D49**：修正 `MAX_TOKENS` 的不完整终态。
- **D50**：同一消息序列、同一预算上的有界自动续写，不重复执行工具。
- **D51**：第十类能力 `COMPACTOR`、插件化 Context 压缩，SDK `1.3.0`。
- **D52**：明确插件权限是意图审计和资源门面，不是同进程 Python 安全沙箱。

## D53：三项骨架一致性修复

- Hook 处理后的工具集合成为模型可见性与实际执行的同一来源，工具调用替换只能修改参数。
- SDK `3.1.0` 增加插件激活与资源清理登记，后台任务从 setup 延迟到激活阶段。
- 实例停止先请求 `SHUTDOWN` 业务取消并等待 Turn 收口，宽限耗尽后才强制取消任务。

## D54：统一请求级压缩与持久化

- SDK `5.0.0` 删除独立 `COMPACTOR`、`ContextCompactor`、对应注册方法和配置字段。
- `TURN_COMPACTOR` 成为唯一压缩策略，在每次最终模型请求边界触发并立即应用摘要。
- Kernel 只登记能按消息来源精确覆盖初始 Session 连续前缀的结果；Turn 收口时先追加
  Transcript，再持久化摘要和水位。临时 Context、Memory 或本 Turn 消息不会写回 Session。
- 4.x 插件迁移到 5.x 时需把 `ContextCompactor` 实现改为 `TurnContextCompactor`，删除
  manifest 中的 `COMPACTOR` 声明及 `context.compactor` / `compactor_timeout_ms` 配置。

## 当前历史结论

D00–D54 的主线不是不断把功能塞进宿主，而是反复证明以下骨架：

- 具体能力能从旧实现移到独立插件，宿主依赖随之减少；
- Builtin 与 Plugin 可以共享一套注册、冲突、权限和生命周期；
- Turn 横切能力可以通过包装层、Hook、Context、Memory、Turn Compactor 或事件接入；
- 已发布 SDK 的兼容新增保持纯新增，必要删除集中在明确 major 并由契约测试保护；
- 无消费者的“已交付能力”会被继续追查并真正接线，而不是只停留在 manifest。

下一阶段不以新增 D 编号或功能数量为目标。当前维护重点见 [`README.md`](./README.md)，未来
设计闸门见 [`evolution-boundaries.md`](./evolution-boundaries.md)。
