# karyvia-plugin-echo-tool

Karyvia 的**最小工具插件**示例：注册一项 `TOOL` 能力 `echo.say`，把传入的 `text`
原样回显。

它演示一个插件最少需要写的四样东西：

| 位置 | 内容 |
| --- | --- |
| `pyproject.toml` | entry point 组 `karyvia.plugins`，name **等于** manifest 的 `id` |
| `MANIFEST` | 能力声明、`sdk_range`、`config_schema`，模块顶层、导入无副作用 |
| `setup(api)` | 同步返回前完成全部注册 |
| `tests/` | 继承 `karyvia.sdk.testing.ToolContract` |

## 安装与启用

```bash
pip install -e examples/plugins/karyvia-plugin-echo-tool
```

安装**不等于**启用。在实例配置里显式列出来才会被加载：

```json
{
  "plugins": {
    "enabled": ["echo-tool"],
    "echo-tool": { "config": { "prefix": ">> " } }
  }
}
```

然后 `karyvia capabilities` 里会出现 `tool:echo.say ← plugin:echo-tool`。

## 要点

- **只 import `karyvia.contracts` 与 `karyvia.sdk`**（依赖规则 `R4`）。够到
  `karyvia.kernel.*` 会被架构守卫拦下。
- **不使用资源服务**。本插件纯内存，因此只读取 `ctx.config`，不需要访问
  `ctx.fs` / `ctx.net` / `ctx.shell`。
- **失败是一等结果**：参数非法时返回 `ok=False` 的 `ToolResult` 而不是抛异常。逸出的异常
  会让 Kernel 只能把副作用标成 `UNKNOWN`。

完整的插件开发说明见仓库的
[`docs/plugin-development.md`](../../../docs/plugin-development.md)。
