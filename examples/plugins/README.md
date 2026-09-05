# examples/plugins/ — 教学用最小示例插件

这里放**最小可运行**的示例插件：演示 manifest、能力声明、宿主服务与生命周期
钩子各自最少需要写什么，供插件开发者对照。

与 `plugins/` 的区别：`plugins/` 是要发行的官方能力，这里的示例只用于阅读和
验收插件加载路径，不承诺功能。

| 目录 | 演示 |
| --- | --- |
| [`karyvia-plugin-echo-tool`](./karyvia-plugin-echo-tool) | 新增一项纯内存能力（TOOL） |
| [`karyvia-plugin-session-memory`](./karyvia-plugin-session-memory) | 覆盖一项内建能力（SINGLETON 的 SESSION_STORE），以及 disable 优先语义 |

两者都是完整独立发行包（`pyproject.toml` + `src/` + `tests/`），经 entry point 组
`karyvia.plugins` 被发现。**它们必须真的装进环境才会被发现**，仓库的测试套件
（`tests/e2e/test_plugin_runtime.py` 与各插件自己的 `tests/`）因此要求：

```bash
pip install --no-deps -e examples/plugins/karyvia-plugin-echo-tool
pip install --no-deps -e examples/plugins/karyvia-plugin-session-memory
```

写自己的插件请从 [`docs/plugin-development.md`](../../docs/plugin-development.md) 开始。
