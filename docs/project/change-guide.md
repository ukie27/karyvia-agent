# 常见改动路线

这是一份“改什么就检查什么”的导航，不是要求每个 PR 跑全部测试。先判断变化类别，再沿相应
路线修改唯一真相来源、派生映射、文档和守卫。

## 1. 先判断变化属于哪一类

```text
需求是否只描述一种能力实现？
  ├─ 是 → Builtin 或独立插件
  └─ 否
      是否是所有能力共同需要的宿主机制？
        ├─ 否 → Runtime 产品策略或插件内组合
        └─ 是
            现有 Capability / Hook / Event / 包装层能否表达？
              ├─ 能 → 沿现有接缝新增
              └─ 不能 → 进入骨架设计评审，先判断兼容/迁移/major
```

以下信号通常说明位置放错了：

- Kernel 中出现厂商名、平台名、产品 prompt 或插件 id。
- Builtin/Plugin import `karyvia.kernel` 或 `karyvia.runtime`。
- Runtime 出现可独立复用的算法、状态机或协议适配。
- 为一个实现增加新的 `CapabilityKind`，而现有 kind 已能表达。
- 为未知未来增加空 Protocol、`dict[str, object]` 万能接口或整个宿主对象。

## 2. 新增一个普通插件

1. 在 `plugins/karyvia-plugin-<id>/` 或 `examples/plugins/` 建独立发行包。
2. Manifest 声明 id、版本、SDK 范围、setup、依赖和能力全集。
3. 实现只导入 `karyvia.sdk` 与 `karyvia.contracts`。
4. 在 `setup(ctx, api)` 中通过对应 `register_*` 注册；声明与实际注册必须完全一致。
5. 后台任务只通过 `ctx.spawn_task` 创建，停止行为由生命周期管理器接管。
   `setup` 失败时 Registry 与任务/订阅分别由 RegistrationBatch 和 StartupResources 回滚，
   插件不要另建宿主无法追踪的任务。
6. 继承匹配的 `sdk.testing` 契约基类，并复制官方插件的 `inspect.signature` 守卫。
7. 加入根 `pyproject.toml` 的 basedpyright include、CI editable 安装清单及清单防漂移测试所需
   的规范位置。
8. 写插件 README：配置、信任边界、能力名、失败边界和安装命令。
9. 跑插件单测、SDK/架构守卫、basedpyright 和至少一个发现/加载 E2E。

不要在根依赖中加入仅该插件需要的包；依赖由插件自己的 `pyproject.toml` 声明。

## 3. 新增现有种类的能力

| 能力 | 注册 API | 主要消费者 |
|---|---|---|
| Tool | `register_tool` | `kernel/turn/invoker.py` |
| Command | `register_command` | `kernel/routing/dispatcher.py` |
| Context | `register_context_provider` | `kernel/turn/context_builder.py` |
| Turn Compactor | `register_turn_compactor` | `kernel/turn/turn_compaction.py`；可持久化水位映射在 `compaction.py` |
| Hook | `api.on` | `kernel/turn/hooks.py` |
| Model | `register_model_provider` | Runtime selection → Engine deps |
| Channel | `register_channel` | Runtime instance/input pump |
| Memory | `register_memory_provider` | `kernel/turn/memory.py` |
| Session Store | `register_session_store` | Orchestrator/Runtime instance |
| CLI Entry | `register_cli_entry` | Runtime CLI bootstrap |

每个能力至少检查：名字/namespace、arity、override 目标、加载失败隔离、回滚和停机行为。
能力对象应通过 Registry 取回，不能由列表直接塞进 Orchestrator。

## 4. 新增一种 CapabilityKind

这是 SDK 兼容新增，不是普通 Enum 修改。用 `rg` 搜索现有十种 kind，至少贯通以下位置：

1. `contracts/capability.py`：Enum、arity 表、引用/解析约束。
2. `contracts`：该能力的公开 Protocol、请求与结果契约。
3. `sdk/api.py`：注册方法与公开类型；`sdk.__all__`。
4. `sdk/manifest.py`：声明是否需要新的验证或 schema 表达。
5. `sdk/testing/`：契约测试基类和 `sdk.testing.__all__`。
6. `kernel/plugins/capabilities.py`：唯一注册载荷形状与 Registry 取回函数。
7. `kernel/plugins/host.py`：声明核对后的注册分派。
8. `kernel/plugins/declarations.py` / `host.py`：namespace、名称和覆盖规则。
9. `runtime/wiring.py` 与 `runtime/selection.py`：从有效能力到生产消费者的连接。
10. `tests/sdk/test_public_surface.py`：CapabilityKind ↔ KaryviaAPI 的一一映射及快照。
11. `tests/contracts/test_protocols.py`：Protocol 形状快照。
12. 插件加载、冲突、事务回滚、诊断输出和 integration 测试。
13. Manifest JSON Schema、插件开发文档、SDK 版本和变更说明。

只有真实消费者随同能力种类一起落地时才增加 kind；“先声明以后再接线”会产生看似可用、实际
无人消费的冻结表面。

## 5. 新增配置字段

配置字段有一个声明真相，但有多个类型化投影和用户视图。逐项完成：

1. 若默认值镜像 Kernel 机制常量，在 `kernel/config/defaults.py` 加常量和逐项对照测试。
2. 在 `kernel/config/schema.py::SECTION_SPECS` 声明字段、类型、默认值和约束。
3. 在 `kernel/config/sections.py` 的对应 frozen dataclass 加类型化属性；需要转成运行策略时更新
   其 `to_*`，保持相关 Kernel import 在函数内部。
4. 在 `schema.validate_config` 的具名构造中读取字段，不能只声明后让值被静默丢弃。
5. 在 `kernel/config/document.py::config_to_json` 渲染字段。
6. 若 Runtime 需要它，在唯一组装/选择位置消费；能力实现不要自己重新读 env 或文件。
7. 在 `docs/configuration.md` 字段表和说明中记录。

`json_schema.py`、默认层、env/CLI 深层覆盖和多数防漂移测试由 `SECTION_SPECS` 派生，通常不应
再手写第二张字段表。至少运行：

```bash
.venv/bin/python -m pytest tests/kernel/test_config.py tests/e2e/test_user_docs.py -q \
  --basetemp=.pytest-tmp/config
```

新增整个小节还要更新 `KaryviaConfig`、`config_to_json` 顶层、Runtime 消费点和文档表。

## 6. 扩展 PluginContext 资源门面

只有插件确实需要宿主资源且现有门面无法表达时才扩展：

1. 在 `contracts` 定义最窄 Protocol 和跨层值对象，避免返回 Runtime/Kernel 类型。
2. 在 `sdk.PluginContext` 增加只读属性，并明确它提供的宿主约束与失败语义。
3. 在 `runtime/access/` 实现生产门面，在 `runtime/plugin_context.py` 连接。
4. 为 denied、timeout、size limit、路径边界和脱敏写测试。
5. 更新 `tests/contracts/test_protocols.py` 的 Protocol 快照。
6. 更新 `tests/sdk/test_public_surface.py` 的 API Protocol/只读属性快照。
7. 更新插件开发文档，并给 `sdk.testing` 或测试 Fake 提供可用替身。

观察实例状态与取消 Turn 目前分别通过 `InstanceView` 和 `TurnControl`；不要把两者重新合成
万能宿主对象。

## 7. 新增事件或错误码

### Event

1. 在 `contracts/events.py::EventName` 增加名字并选择 category。
2. 找到唯一生产者；turn 事件必须经 `translation.py` 和 Orchestrator 发布。
3. 载荷只放必要、可脱敏、数量有界的数据；不要在调用点自行构造 RuntimeEvent。
4. 更新事件序列快照、翻译覆盖测试、诊断文档或插件订阅文档。

### Error

1. 在 `contracts/errors.py::ErrorCode` 与 `CODE_CATEGORIES` 同时登记。
2. 只在实现处引用 Enum 成员，不写错误码字符串。
3. 判断错误消息/detail 是否可能携带插件异常文本、路径、token 或 Secret。
4. 为类别、脱敏和外部可观察终态补测试。

## 8. 修改 Turn 执行

先确定变化属于哪一层：

- 模型调用前后透明行为：优先包装 `Model`，装配在 `orchestration.engine_deps`。
- 工具调用的超时/校验/副作用边界：`ToolInvoker`。
- 单次模型工具循环：Engine。
- Session、Context、Transcript、事件终态：Orchestrator。
- 入站去重/并发/命令：Routing。
- 具体模型/工具策略：插件。

任何 Engine 改动都要检查：四槽 `EngineDeps`、四个 engine hook、预算扣除点、取消检查点、
单终态、工具不重放、首个实质分片后的不可重试、三次续写上限和 400 行限制。

任何 Orchestrator 改动都要检查：事件单发布点、hook 次数、started/terminal 配对、Session
锁持有范围、Transcript 保存失败、附件收集、Memory/Compactor 失败策略。

Turn 内请求增长不通过增加 Engine 槽位处理：在 Orchestrator 的 model 装配边界包装
`TurnCompactingModel`，策略由 `TURN_COMPACTOR` 插件提供。修改时必须同时验证完整
`ModelRequest` 估算、工具往返不可拆分，以及消息来源映射：只有覆盖初始 Session 连续前缀
的摘要可在 Transcript 追加后持久化；混入临时 Context 或本 Turn 消息时不得推进水位。

## 9. 修改持久化契约

Session JSONL 或未来插件状态变更时：

1. 先写旧格式 fixture 和当前读写 round-trip 测试。
2. 定义 schema/state version 与支持区间。
3. 区分“自动无损迁移”“需要显式命令”“拒绝启动”，不要静默丢字段。
4. 设计崩溃中断、重复迁移、只读文件和回滚行为。
5. 更新格式文档和部署升级步骤。
6. 完成迁移后再切换默认写格式；不要同时长期双写两套格式。

`SessionKey.storage_id` 不随 Session schema 迁移一起改变。

## 10. 修改 SDK 公开表面

### 兼容新增（minor）

- 新字段必须有语义明确的默认值，并确保旧实现仍可被调用。
- 新 Protocol 成员通常会让第三方结构化实现失配；优先新增独立 Protocol/能力，而不是给现有
  Protocol 强塞必需方法。
- 更新 `SDK_VERSION`、`__all__` 快照、Manifest schema、契约测试和官方插件类型检查。
- 用 `inspect.signature` 验证实现对象，而不只依赖 `isinstance(Protocol)`。

### 破坏性变化（major）

- 写明不能兼容新增的原因和受影响插件。
- 定义旧 manifest 的拒绝错误与版本范围，不做含糊的运行时猜测。
- 在同一工作序列更新全部官方插件、示例、文档和测试。
- 若需要迁移工具，先交付工具再移除旧表面。

## 11. 拆分接近上限的文件

拆分应按所有权，不按行号：

- 纯数据/常量与行为分开；
- 解析/验证与 I/O 分开；
- 状态机与组装分开；
- Registry 载荷形状与能力消费者分开；
- 派生视图与唯一真相来源分开。

保留原 import 路径时，可在原模块原样再导出，避免无意义的内部调用方 churn。拆分后用架构
测试证明没有引入反向依赖，并用原行为测试证明只是重构。

禁止通过删除 docstring、合并语句、缩短变量名或提高 500/800 行阈值来“解决”超限。

## 12. 注释与命名整理

阅读代码时按下面的顺序判断一段注释：

1. 它是否解释当前仍成立的契约、安全边界、性能选择或反直觉行为？是则保留并尽量写成正面
   陈述。
2. 它是否只记录阶段编号、提交顺序、旧实现或某次踩坑过程？把当前结论留下，历史交给 Git。
3. 它是否复述下一行代码？删除，优先让函数和变量名表达意图。
4. 它是否写着“以后补”“暂时”“已知缺口”，但功能已经实现？立即改成当前行为。
5. 它是否仍是有效缺口？放到可追踪的设计/issue 文档；局部只说明当前限制和失败方式。

等价可读性重构优先使用具名小函数、早返回、领域名词和按职责分段。不要为减少几行代码引入
动态分派、反射、万能字典或额外热路径分配。

## 13. 验证矩阵

| 改动 | 最小相关验证 |
|---|---|
| Contracts/SDK | contracts + sdk + public surface + basedpyright |
| Registry/插件加载 | kernel plugin/registry + architecture + plugin runtime E2E |
| Turn/Routing | kernel turn/routing + integration skeleton |
| Config | kernel config + user docs + first-run E2E |
| Builtin/官方插件 | 自身测试 + contract base + signature + basedpyright |
| Runtime/CLI | runtime + CLI 文档守卫 +相关 E2E |
| 启动/停止生命周期 | bootstrap + instance + plugin context + architecture |
| 持久化 | codec/round-trip +旧 fixture +部署升级说明 |
| 纯重构 | 原目标测试 + architecture + ruff + basedpyright |

完整测试要求官方插件 entry point 已安装。若环境导致某组无法运行，应报告具体缺少的插件或
受限资源，不能把环境失败解释为产品测试通过。
