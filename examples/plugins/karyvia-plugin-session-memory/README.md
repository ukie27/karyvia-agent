# karyvia-plugin-session-memory

Karyvia 的**覆盖内建能力**示例：用一个纯内存的 `SessionStore` 覆盖内建的 JSONL 会话
存储。进程退出即忘——适合一次性容器、演示，以及「不要在磁盘上留下对话」的场景。

它演示 `echo-tool` 覆盖不到的三件事：

1. **SINGLETON 能力的覆盖**：`session_store` 全实例只有一个生效实现，替换必须在 manifest
   里显式写 `overrides = "builtin:jsonl"`。覆盖永不由加载顺序决定。
2. **覆盖关系是可见的**：`karyvia capabilities` 的「被覆盖」段会印出
   `session_store:jsonl ← builtin` 与覆盖它的 `session_store:memory ← plugin:session-memory`。
   静默替换用户的会话历史后端是这套设计明确要堵的路。
3. **禁用覆盖插件后，内建实现正常恢复**（见下）。

## 安装与启用

```bash
pip install -e examples/plugins/karyvia-plugin-session-memory
```

```json
{
  "plugins": { "enabled": ["session-memory"] }
}
```

## 禁用它

把它写进 `plugins.disable` 后，禁用优先于启用：

```json
{
  "plugins": {
    "enabled": ["session-memory"],
    "disable": ["session-memory"]
  }
}
```

Runtime 不读取或加载 `session-memory`，它的覆盖声明也不参与本次启动，因此内建 JSONL
存储正常生效，会话继续落盘。要再次使用内存存储，运行
`karyvia plugins enable session-memory`，该命令会从 `plugins.disable` 移除它。

完整的插件开发说明见仓库的
[`docs/plugin-development.md`](../../../docs/plugin-development.md)。
