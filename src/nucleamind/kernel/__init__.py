"""第 2 层：Kernel 机制。

职责：提供能力注册与覆盖解析、turn 执行、输入分流、插件运行时、配置加载与
可观测性等机制，只依赖 contracts。
不负责：实现具体能力（模型、存储、工具、渠道），也不认识任何具体 provider。

子包各自拥有一类机制：

- `registry/`：能力注册、冲突与覆盖解析；
- `turn/`：取消、预算、Context、Engine 与 Orchestrator；
- `config/`：实例布局、分层配置、Secret 引用与实例锁；
- `observability/`：事件总线、脱敏、sink 与诊断查询；
- `routing/`：输入去重、Session 并发与命令分流；
- `plugins/`：发现、加载、Host 与生命周期。

这些子包可以互相复用 Kernel 机制，但不得反向依赖 SDK、Builtin 或 Runtime。
"""
