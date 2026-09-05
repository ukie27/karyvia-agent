# nanobot 参考导航

## 定位

`references/nanobot` 是 Karyvia fork 前的原始项目代码。它的主要用途是确认现有行为、配置格式、运行时边界和迁移前后的差异，不作为新功能设计的唯一依据。

## 优先入口

- Agent 执行循环：`references/nanobot/nanobot/agent/loop.py`
- LLM 工具调用循环：`references/nanobot/nanobot/agent/runner.py`
- Provider 抽象：`references/nanobot/nanobot/providers/base.py`
- Provider 创建与发现：`references/nanobot/nanobot/providers/factory.py`、`registry.py`
- Memory：`references/nanobot/nanobot/agent/memory.py`
- Session：`references/nanobot/nanobot/session/`
- 配置：`references/nanobot/nanobot/config/schema.py`、`loader.py`
- Channel：`references/nanobot/nanobot/channels/`
- Tools：`references/nanobot/nanobot/agent/tools/`

## 适合查询的问题

- 当前 Karyvia 是否保持 nanobot 的既有行为？
- 某个能力在原始项目中由哪个模块负责？
- 某个配置字段、事件或工具结果的原始格式是什么？
- 抽离某个能力为插件时，原始实现与核心路径的边界在哪里？

## 读取提示

涉及核心行为时，至少同时查看定义、一个调用方和对应测试。不要因为某个目录位于 `nanobot/` 下就认定它应继续属于 Karyvia Kernel；此项目的目标是逐步重新划分这些边界。
