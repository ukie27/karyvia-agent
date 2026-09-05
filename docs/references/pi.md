# Pi 参考导航

## 定位

`references/pi` 是 Karyvia 研究极简 Agent Runtime 和扩展设计的主要参考项目。Pi 更偏 coding agent，因此重点借鉴扩展机制、Session、Context 和 Runtime 边界，不直接照搬 coding-specific 功能。

## 优先入口

- 总体扩展理念：`references/pi/packages/coding-agent/README.md`
- Agent 基础包：`references/pi/packages/agent/`
- Coding Agent Runtime：`references/pi/packages/coding-agent/`
- 扩展示例：`references/pi/packages/coding-agent/examples/extensions/`
- Session 后端：`references/pi/packages/session-backends/`
- 协议：`references/pi/packages/protocol/`
- AI 抽象：`references/pi/packages/ai/`
- 项目级上下文和开发规则：`references/pi/AGENTS.md`

## 适合查询的问题

- 极简 Agent 核心保留哪些能力？
- 扩展如何注册工具、命令、事件和 UI？
- Context 文件、Skill、Prompt Template 和 Extension 如何组合？
- Session、分支和压缩如何与 Agent Runtime 解耦？
- 如何通过 SDK、RPC 或插件包嵌入和扩展 Agent？

## 读取提示

先看 `README.md` 和扩展示例，再进入 Runtime 实现。研究个人 AI 助手场景时，需要明确区分 Pi 的 coding-agent 假设与 Karyvia 的通用个人助手目标。
