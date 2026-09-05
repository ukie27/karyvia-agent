# Design Constraints

以下规则约束 Karyvia 的架构决策。添加功能或修复 bug 时，优先选择尊重这些边界的路径。
执行规则在 [`../AGENTS.md`](../AGENTS.md)，层次与代码所有权在
[`../docs/project/architecture-map.md`](../docs/project/architecture-map.md)，常见改动路线在
[`../docs/project/change-guide.md`](../docs/project/change-guide.md)；本文件只放判断标准。

## 核心保持小，能力在边缘扩展

新能力进 `plugins/`（外部发行包）或 `builtins/`（默认能力），机制才进 `kernel/`。
判断标准：

> 这是 Agent Runtime 的**必须**能力，还是**用户需求**能力？

必须能力才进内核：turn 执行循环、模型抽象、消息契约、Session 管理、Context 接口、
Tool 注册、Plugin Runtime、基础配置。用户需求能力一律做成插件——Memory、Channels、
Browser、MCP、Automation、Multi-Agent 都在这一侧。

`kernel/turn/engine.py` 是核心路径，有 ≤400 行的硬上限和 import 白名单，各有测试盯着。
**内建与插件同等身份**（`BAS-005`）：它们共用一个 `KaryviaAPI` 实现、同一条加载路径、
同一套资源与生命周期边界。写内建时不要另开注册通道——`tests/architecture/test_builtin_no_privilege.py`
的符号扫描就是为此存在的。

## 少结构，多智能

优先简单可读的代码，而非新的框架层和间接层。只有当下述情况才增加结构：
移除真实复杂度、保护重要边界、或匹配已有的本地模式。最好的修复往往是一个更紧的
tool 契约、一个 Channel 插件局部的改动，或一个聚焦的回归测试。

## 优先重复而非过早抽象

Channel 与 Model Provider 插件之间允许重复相似逻辑（发送重试、媒体处理、消息拆分、
零网络测试闸门）。**不要为了消除跨插件的重复而引入共享基类或公共包**——`R4` 让插件
够不着彼此，而每个插件是可独立禁用、可被第三方覆盖的提供方。

判据是「改一边要不要同时改另一边」：要，就写一条逐条对照测试把两份钉在一起
（`tools_shell/paths.py::CwdGuard` 与 `tools_fs.WorkspaceGuard` 就是这么处理的）；
不要，就让它们各自演化。

## 在边界类型化动态数据

Wire payload、持久化记录、第三方 SDK 对象是不可信的动态边界。在拥有它们的**边缘**
解析或归一化，用 frozen dataclass / `TypedDict` 固定形状，让校验只发生一次。

**`Any` 必须带 `# boundary: 理由` 注释**，否则 `tests/architecture/test_any_usage.py`
会拦下来。它对 `plugins/` 全目录生效，包括测试树。平台 SDK 对象在归一化之后就不该
再存在——`plugins/karyvia-plugin-feishu/` 里 `Any` 只出现在两个 SDK 出口模块上。

`typing.cast` 不做运行时校验。每个新 cast 必须有同路径的运行时检查支撑，或有从构造与
控制流即可明确的不变式（不明显时在本地注释说明）。不要仅为让 basedpyright 闭嘴而 cast。

## 显式优于魔法

配置字段只在 `kernel/config/schema.py` 的 `SECTION_SPECS` 一处声明，那张表同时是
默认值、类型与 `extra="forbid"` 的唯一依据。插件的配置块由自己 manifest 的
`config_schema` 声明。错误处理抛清晰异常，不静默修正坏输入。

能力的覆盖必须在 manifest 里**显式声明**（`EDG-102`）：覆盖关系永不由加载顺序决定，
判定只在 `kernel/registry/resolution.py` 一处。
