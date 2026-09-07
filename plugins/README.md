# plugins/ — 官方插件

每个官方插件是**独立发行包**，目录形如：

```text
plugins/karyvia-plugin-<id>/
├── pyproject.toml
├── src/karyvia_plugin_<id>/
└── tests/
```

放在仓库顶层而不是包内，是为了让边界由打包机制强制：包内的「插件」可以随手
import 兄弟模块，依赖规则 `R4` 就成了空话。官方插件与第三方插件走**完全相同**
的加载路径（entry point 组 `karyvia.plugins`）。

写插件请看 [`docs/plugin-development.md`](../docs/plugin-development.md)，可运行的最小范例在
[`examples/plugins/`](../examples/plugins/README.md)。

## 当前的官方插件

| 目录 | id | 能力 | 落地于 |
| --- | --- | --- | --- |
| `karyvia-plugin-openai-api/` | `openai-api` | `CHANNEL:openai` —— OpenAI 兼容 HTTP 接口（`/v1/chat/completions`、`/v1/models`），配合 `karyvia serve` 常驻 |  |
| `karyvia-plugin-anthropic/` | `anthropic` | `MODEL:anthropic` —— Anthropic 原生 Messages API，与内建 `model-openai` 并存 |  |
| `karyvia-plugin-feishu/` | `feishu` | `CHANNEL:feishu` —— 飞书 / Lark bot（WS 长连接、CardKit 流式卡片），配合 `karyvia serve` 常驻 |  |
| `karyvia-plugin-web/` | `web` | `TOOL:web.fetch` + `TOOL:web.search` —— 抓网页与搜网两件工具 |  |
| `karyvia-plugin-mcp/` | `mcp` | `TOOL:mcp.*` —— 把 MCP server 的工具接进实例（命名空间声明， 机制的第一个使用者） |  |
| `karyvia-plugin-memory/` | `memory` | `MEMORY:jsonl` + `CONTEXT:memory` + 三条 `TOOL:memory.*` + `COMMAND:memory` —— 跨 Session 的长期记忆（JSONL 存储 + 关键词召回，零新依赖） |  |
| `karyvia-plugin-cron/` | `cron` | `CHANNEL:cron` + 三条 `TOOL:cron.*` + `COMMAND:cron` —— 定时任务：到点自己开一条 turn，结果回到创建它的那个会话（自写 cron 解析，零新依赖） |  |

**它们必须真的装进环境才会被发现**（entry point 没有第二条路）：

```bash
pip install --no-deps -e plugins/karyvia-plugin-openai-api
pip install --no-deps -e plugins/karyvia-plugin-anthropic
pip install --no-deps -e plugins/karyvia-plugin-feishu
pip install --no-deps -e plugins/karyvia-plugin-web
pip install --no-deps -e plugins/karyvia-plugin-mcp
pip install --no-deps -e plugins/karyvia-plugin-memory
pip install --no-deps -e plugins/karyvia-plugin-cron
```

装上不等于启用——`plugins.enabled` 是唯一闸门，改完要重启实例。
