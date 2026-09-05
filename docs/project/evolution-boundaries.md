# 演进边界

这份文档把架构分为三类：已经承诺稳定的边界、可以直接做兼容新增的接缝、必须等真实需求
出现后再专项设计的闸门。目标不是预测所有未来，而是避免现在把错误抽象冻结进 SDK。

## 1. 判断原则

一个好骨架不需要提前拥有每个接口。它需要：

- 已存在的核心语义只有一个真相来源；
- 新能力可以在不修改主循环的情况下接入；
- 无法兼容扩展的地方被明确识别，修改时有版本/迁移路径；
- 未知需求不通过空 Protocol、万能上下文对象或配置开关提前固化。

因此“还没有子 Turn API”不等于骨架失败；如果当前公开对象没有泄漏内部 Orchestrator，未来
仍能增加一个受约束的门面，这反而保留了设计空间。

## 2. 已冻结的稳定边界

### 持久化与标识

- `SessionKey.storage_id()` 的编码必须保持可逆、无碰撞和向后兼容。
- Session JSONL 与插件 `state_version` 是外部可观察的存储契约。
- 改字段含义、键格式或版本判定必须先写迁移设计和旧数据失败策略。

### 错误、事件与安全数据

- 错误码集中登记，错误分类由码推导。
- Secret 使用唯一 `SecretStr`，事件和错误在构造/发布路径统一脱敏。
- RuntimeEvent sequence 由 EventBus 分配；turn 事件只有一个发布点。
- 这些可以新增枚举项，不能让不同子系统各自产生第二套语义。

### Turn 语义

- 业务取消使用 `CancelToken` 检查点。
- 六项预算和终态映射集中定义；工具副作用之前必须记账。
- Engine 事件流只有一个终态。
- 同 Session 单写者、去重优先、started 必有终态。
- 模型重试不能重放用户已看见的输出，续写不能重做工具副作用。

### 插件与 SDK

- SDK 5.x 的公开名称、Protocol 签名、Manifest 字段和注册语义承担兼容承诺。
- 内建与外部插件使用同一个 Host、Registry、冲突解析和生命周期。
- 覆盖结果由声明和确定性 resolution 决定，不依赖 import/setup 顺序。
- `PluginContext` 暴露窄资源门面，不暴露 Runtime/Kernel 内部对象。
- 安装、更新和卸载是 NucleaMind home 级操作；实例只拥有启用、配置和业务状态。
- 命名实例统一收在 NucleaMind home 的 `instances/<name>/`；显式实例目录可以位于外部。
- 当前安装后端与执行宿主是 Python。增加 npm 或进程外插件时扩展 Runtime 的安装后端与
  执行桥，不改变实例级启用语义，也不把包管理策略放进 Kernel。

### 配置与依赖方向

- 配置优先级为 default < file < env < CLI，未知字段拒绝。
- 配置文档保留 `${VAR}`，解析后的密钥独立存放且不写回。
- 六层依赖方向由架构测试固定，Runtime 是唯一组装根。

这些稳定边界并非永远不能改变；它们要求显式版本、迁移和评审，不能被普通功能 PR 顺手
改变。

## 3. 可以直接扩展的接缝

下列变化通常是兼容新增：

- 增加一个现有 kind 的能力实现或官方插件；
- 增加一个新的 Tool/Command/Hook/Context Provider；
- 增加 EventName、ErrorCode 或可选 Context 片段类型，并补齐集中映射；
- 在现有请求/结果对象末端增加有默认值、调用方可忽略的字段；
- 给 `PluginContext` 增加一个窄、可替换、由 Runtime 实现的资源 Protocol；
- 在 `SECTION_SPECS` 增加有默认值的配置字段并贯通 Config 对象与文档；
- 在 Model、Tool 或事件订阅者外增加透明包装层；
- 新增一个 `CapabilityKind`，前提是走完整的 SDK minor 版本流程，而非局部修改。

“兼容新增”仍需检查调用方是否穷举 Enum、dataclass 构造是否使用关键字、JSON schema 是否
允许新字段，以及第三方实现是否会因 Protocol 变化失配。

## 4. 三个未来设计闸门

### 4.1 插件组合、子 Turn 与多 Agent

触发条件：出现一个真实插件，需要在自己的能力执行过程中让宿主启动另一个受管理的 Agent
Turn，而使用普通 Tool/Command 无法表达。

届时应先回答：

- 子 Turn 继承还是新建 Session/Conversation/Correlation？
- 父子取消如何传播，父预算如何分配，最大递归深度是多少？
- 子 Turn 的 PluginContext 和模型选择从哪里继承？
- 输出是流式转发、结构化结果，还是写回父 Turn 的工具结果？
- 子 Turn 失败时，父 Turn 的终态和已发生副作用如何表示？

可能的公开形状是窄 `TurnGateway` 或 `AgentExecutor` Protocol，由 Runtime/Orchestrator 提供
实现，并通过 `PluginContext` 授予。名称和字段必须在上述语义确定后再冻结。

当前禁止的捷径：

- 把 `TurnOrchestrator`、Registry 或 Runtime Instance 直接交给插件；
- 让插件自己 import Kernel 并调用 `run_turn()`；
- 用全局函数或 service locator 绕过取消、预算、事件和 Session 调度；
- 为 Multi-Agent 给 `EngineDeps` 增加第五个宿主对象槽。

### 4.2 主体身份与 Memory 作用域

触发条件：真实产品需要同一主体跨 Channel/Conversation 共享某类 Memory，且简单 Session
隔离不再满足隐私和召回语义。

届时应先区分：

- 传输身份：某个平台的用户/组织/空间标识；
- 会话身份：当前 `SessionKey`，服务于并发和 Transcript；
- 主体身份：经显式映射或认证后确认的跨平台主体；
- Memory 作用域：user/session/workspace/agent/organization 等可审计范围。

应新增独立 `MemoryScopeKey` 或 `MemoryRequest` 一类契约，使作用域选择显式且可授权。不要
改变 `SessionKey.storage_id()`，也不要默认把同名用户跨平台合并。

当前禁止的捷径：

- 在 Memory 插件里解析 SessionKey 字符串猜用户；
- 把 Channel 用户名当作全局主体 ID；
- 为共享记忆绕过审计和删除边界；
- 把主体字段先塞入所有消息但不给出来源与可信度语义。

### 4.3 原生多模态内容

触发条件：至少一个入站 Channel 和一个 Model 都需要保真处理图片、音频或文档，而且附件
路径/URL 与文本描述无法满足能力协商、持久化或回放。

届时应先设计：

- `ContentBlock` 的可扩展联合类型及未知块保真策略；
- 模型能力协商和不支持媒体时的确定性降级；
- inline bytes、外部引用和 workspace 文件的生命周期/大小边界；
- Transcript、Session JSONL、事件与日志如何避免泄密和膨胀；
- ToolResult/OutboundMessage 与 ModelRequest 是否共享块类型或只做显式转换。

当前 `AttachmentRef` 负责跨 Channel、Turn 和 Session 保存文件引用；Context Builder 只把
这些元数据投影成 user 文本，`file.send` 只产生出站附件意图。`OpaqueBlock` 负责供应商块的
同 turn 保真回放。它们已经覆盖“收文件引用、记住引用、发送 workspace 文件”，但不是应当
无限扩张的原生多模态容器。

当前禁止的捷径：

- 将 base64 或厂商 JSON 塞进普通文本；
- 用 `OpaqueBlock` 绕过公共多模态语义；
- 悄悄改变 `Attachment` 为模型内容块；
- 只改模型插件，不设计 Session/Transcript 的保存与降级。

## 5. 其他刻意延期的边界

### 插件热加载与状态迁移

当前生命周期服务于实例启动/停止，`state_version` 不一致直接拒绝。只有出现不停机升级的真实
部署需求时，才设计隔离、任务排空、能力原子切换、回滚和状态迁移。不要把重新 import Python
模块称为“热加载”。

### 不可信插件隔离

当前插件是受信任的同进程代码。未来更严格的隔离应由独立宿主/进程、RPC 协议或可选安全
插件承担，并明确序列化、性能、崩溃恢复和能力代理边界。继续往应用级权限表增加权限名不能
把同进程 Python 变成沙箱。同进程也意味着 Python 依赖不能真正按插件隔离：当前安装管理器
要求默认安装的同名发行包版本一致；真正允许同名包多版本必须连同进程宿主与 RPC 一起设计。

### Builtin 的发行拆分

Builtin 与主包同 wheel 交付是安装体验决定，不是架构特权。只有主包体积、依赖或发布节奏
真正成为问题时，再把某个 Builtin 迁成独立发行包；Registry 语义无需因此变化。

### OpenClaw / 更高层 Agent 产品

更高层的技能市场、多 Agent 编排或产品 UI 应依赖 NucleaMind 的公开 SDK/embed 表面，以独立
包演进。不能为了上层产品方便，将产品状态和交互策略下沉到 Kernel。

## 6. 何时允许修改骨架

只有同时满足下列条件，才启动骨架级设计：

1. 有至少一个可描述、可测试的真实调用方，现有接缝确实无法表达。
2. 问题属于多个能力共用的宿主机制，而不是单个插件策略。
3. 已列出对 SDK、配置、持久化、事件、取消和预算的影响。
4. 已决定是兼容新增、带迁移的变更，还是 SDK major 变更。
5. 新接口比“把内部对象直接暴露出去”更窄，并能由 Fake 做契约测试。
6. 不需要长期维护两套语义或双读兼容层。

不满足时，先把需求实现为插件内策略或写成设计问题，不新增占位接口。

## 7. 破坏性演进流程

若确实必须突破稳定边界：

1. 写一页决策记录：现有表面为何无法兼容扩展、受影响的真实调用方和备选方案。
2. 明确版本：SDK minor/additive、SDK major、配置 schema 或持久化 schema。
3. 写旧输入/旧状态的可执行兼容测试与预期失败信息。
4. 先增加迁移读取/转换工具，再切换写入或默认行为。
5. 更新公开文档、Manifest JSON Schema、快照和官方插件。
6. 给兼容期设定明确结束条件；若项目策略是不保留兼容层，则在同一发布中完成迁移并清理。

具体文件路线见 [`change-guide.md`](./change-guide.md)。
