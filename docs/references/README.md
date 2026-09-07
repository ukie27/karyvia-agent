# 参考项目导航

本目录保存 Karyvia 维护的参考项目文档。参考项目源码本身位于根目录的 `references/`，该目录被 Git 忽略，换设备时重新克隆即可。

## 参考项目

| 项目 | 用途 | 导航 |
| --- | --- | --- |
| OpenClaw | 研究插件 SDK、宿主与插件边界，以及未来的插件生态兼容 | [openclaw.md](./openclaw.md) |
| Pi | 研究极简 Agent Runtime、扩展点、Session、Context 和 coding-agent 设计 | [pi.md](./pi.md) |

项目路径和主题入口见 [`catalog.json`](./catalog.json)。

## 按需读取流程

1. 先判断当前问题属于哪个主题，例如 Plugin Runtime、Memory、Context、Channel 或 Session。
2. 阅读对应项目导航文档中的主题入口。
3. 使用 `scripts/reference_index.py query` 或 `rg` 定位具体文件和符号。
4. 只读取相关定义、直接调用方、直接调用的实现和最近的测试。
5. 需要比较多个项目时，先分别确认各自的公开契约，再整理差异，不要根据单个实现推断通用设计。

示例：

```powershell
.\.venv\Scripts\python.exe scripts/reference_index.py build
.\.venv\Scripts\python.exe scripts/reference_index.py build --project openclaw --symbols
.\.venv\Scripts\python.exe scripts/reference_index.py query --project openclaw --text plugin
.\.venv\Scripts\python.exe scripts/reference_index.py query --project pi --symbol ExtensionAPI
```

## 索引边界

索引是轻量导航工具，不是完整语义代码图。当前只记录：

- 文件路径、语言和文件大小
- 可选的 Python 顶层类、函数和异步函数
- 可选的 TypeScript/JavaScript 常见声明
- 基于文本的主题搜索结果

默认 `build` 只生成文件级索引；使用 `--symbols` 才解析符号。对大型项目优先使用
`build --project <name> --symbols`。生成数据位于 `docs/references/.index/`，不提交到 Git。
索引过期时重新运行对应的 `build` 命令。

## 维护原则

- 本目录的文档是 Karyvia 的设计研究资料，应提交并接受版本控制。
- `references/` 内只放本地克隆的参考源码，不在其中新增 Karyvia 文档。
- 导航文档记录“从哪里开始读”，不复制参考项目的大段源码。
- 参考项目版本变化时，更新 `catalog.json` 中的版本或 commit 信息，并检查受影响的导航路径。
