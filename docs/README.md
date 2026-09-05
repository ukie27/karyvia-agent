# Karyvia Agent Technical Documentation

本目录只放**当前实现**的文档。`D35` 删掉 `legacy/` 的同一个 PR 里，21 篇描述被继承的
nanobot 实现的文档一并删除——它们教人跑的是 `nanobot onboard`、`nanobot webui` 这类
已经不存在的命令，留着比没有更糟。需要确认旧行为时读 `references/nanobot/`
（见 [`references/README.md`](./references/README.md)）。

新层的**用户**文档（安装、配置字段、CLI 参考、部署）在 `D46` 补齐，见下表。它们与代码
之间有守卫：`tests/e2e/test_user_docs.py` 把配置字段表与 `SECTION_SPECS`、CLI 子命令清单
与 `runtime/cli/main.py` 的派发分支、插件安装清单与磁盘上的发行包各比对一次。

## 现有文档

| 目标 | 文档 |
|---|---|
| 第一次把它跑起来 | [`getting-started.md`](./getting-started.md) |
| 查配置字段与优先级 | [`configuration.md`](./configuration.md) |
| 查 `karyvia` 的参数与退出码 | [`cli.md`](./cli.md) |
| 部署成常驻服务 | [`deployment.md`](./deployment.md) |
| 了解项目方向 | [`project/开发背景.md`](./project/开发背景.md) |
| 遵循仓库开发规则 | [`../AGENTS.md`](../AGENTS.md) |
| 接手当前开发工作 | [`project/README.md`](./project/README.md) |
| 查层次、主链路与代码所有权 | [`project/architecture-map.md`](./project/architecture-map.md) |
| 查某类改动需要修改哪些位置 | [`project/change-guide.md`](./project/change-guide.md) |
| 判断稳定边界与未来设计闸门 | [`project/evolution-boundaries.md`](./project/evolution-boundaries.md) |
| 回顾 D00–D52 里程碑 | [`project/history.md`](./project/history.md) |
| 参考项目的阅读规范 | [`references/README.md`](./references/README.md) |
| 写一个插件 | [`plugin-development.md`](./plugin-development.md) |
| 理解插件信任边界 | [`plugin-development.md`](./plugin-development.md#6-资源服务与信任边界) |
| 读或迁移内建会话存储格式 | [`session-storage.md`](./session-storage.md) |

三篇能力文档各自的性质：

- [`session-storage.md`](./session-storage.md) 是**已发布的兼容契约**（`SES-006`），
  外部实现按它写；改 `builtins/session_jsonl/codec.py` 的字段就得改它。
- [`plugin-development.md`](./plugin-development.md) 的代码块由
  `tests/e2e/test_plugin_docs.py` **直接执行**，因此不会漂移。

四篇用户文档里，`configuration.md` 的字段表与 `cli.md` 的子命令清单同样由测试钉着
（`tests/e2e/test_user_docs.py`）。**加一个配置字段要连它一起改**——这是
「加一个配置小节要改五处」那份清单之外的第六处。

官方插件各自带 README，说明自己的配置项与已知边界：
[`plugins/README.md`](../plugins/README.md)。

## 文档规则

- 不把 Karyvia 用户指向上游 nanobot 的安装器、PyPI 包、issue、PR、release
  或社区渠道。
- 上游归属记在 `LICENSE`、`THIRD_PARTY_NOTICES.md` 或明确的历史说明里，
  不作为本项目的当前归属。
- 一条能力以插件形态落地时，在同一个 PR 里写它的文档；**不要**先留一篇描述
  「将来会怎样」的占位文档。
- 所有权边界移进或移出 Kernel 时更新架构说明（当前在
  [`project/architecture-map.md`](./project/architecture-map.md) 与
  [`project/technical-design.md`](./project/technical-design.md)）。
- 活跃入口只描述当前事实和长期规则。阶段流水账压缩到
  [`project/history.md`](./project/history.md)，精确过程交给 Git，不再复制进 `AGENTS.md`。
- 新的公开接缝或持久化语义要同步更新
  [`project/evolution-boundaries.md`](./project/evolution-boundaries.md)；常见改动位置发生变化时
  更新 [`project/change-guide.md`](./project/change-guide.md)。
