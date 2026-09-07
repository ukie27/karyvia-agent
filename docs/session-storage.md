# 会话存储格式：`session_jsonl`

内建会话存储 `builtins/session_jsonl/` 的磁盘格式说明。

这份文档是**对外承诺**：内建实现的存储格式必须可被外部实现读取。
文档中的每个示例都由 `tests/builtins/test_session_jsonl.py` 直接喂给实现的解码器解析，
因此它不会与代码漂移——漂移会让测试失败。

## 1. 文件布局

每个会话两个文件，落在同一个目录里（默认是实例目录下的 `sessions/`）：

```text
<sessions_dir>/
├── cli~local~default.jsonl        # 历史，逐条追加
└── cli~local~default.meta.json    # 元数据，原子替换
```

文件名的主干是 `SessionKey.storage_id`：三个分量（`channel_id`、`conversation_id`、
`scope`）各自按 UTF-8 逐字节百分号编码后，用 `~` 连接。编码是单射的，因此不同的
`SessionKey` 不可能落在同一个文件上。`~` 不在安全字符集内，所以按 `~`
切分永远不会切错位置。

```text
SessionKey("cli", "local")            -> cli~local~default
SessionKey("telegram", "-100123")     -> telegram~-100123~default
SessionKey("cli", "a:b", "proj")      -> cli~a%3Ab~proj
```

## 2. 历史文件（`.jsonl`）

UTF-8 编码，每行一条记录，行尾是 `\n`。记录内容里的换行由 JSON 转义成 `\n`，
所以「一行一条」不会被消息内容破坏。

```jsonl
{"message_id": "m-1", "role": "user", "content": "请查看报告", "created_at": "2026-08-12T09:30:00+00:00", "turn_id": "turn-7", "attachments": [{"source": "workspace", "locator": "reports/final.pdf", "media_type": "application/pdf", "size_bytes": 2048, "filename": "final.pdf"}]}
{"message_id": "m-2", "role": "assistant", "content": "我来看一下。", "created_at": "2026-08-12T09:30:02+00:00", "turn_id": "turn-7"}
{"message_id": "m-3", "role": "tool", "content": "42", "created_at": "2026-08-12T09:30:03+00:00", "turn_id": "turn-7", "tool_call_id": "call-1"}
{"message_id": "m-4", "role": "assistant", "content": "一共 42 个。", "created_at": "2026-08-12T09:30:05+00:00", "turn_id": "turn-7", "interrupted": true, "metadata": {"channel": {"edited": true}}}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `message_id` | string | 是 | 记录标识，非空、无控制字符 |
| `role` | string | 是 | `system` / `user` / `assistant` / `tool` |
| `content` | string | 是 | 文本内容，可为空串 |
| `created_at` | string | 是 | **带时区**的 ISO-8601 时间戳 |
| `turn_id` | string | 否 | 产生这条记录的 turn |
| `tool_call_id` | string | 否 | **当且仅当** `role="tool"` 时出现 |
| `interrupted` | bool | 否 | 缺省为 `false`；流式中途被打断的内容标记为 `true`，不得当作完整回答 |
| `attachments` | array | 否 | 缺省为 `[]`；结构与 `AttachmentRef` 一致，只保存引用与元数据，不保存文件字节 |
| `metadata` | object | 否 | 缺省为 `{}`；受契约层的条数、深度与大小上限约束 |

**可选字段缺席而不是写 `null`**。解码方一律用「取不到就用默认值」的规则，写入方不产生
恒为 `null` 的键——会话文件是逐条追加的长文件。

## 3. 元数据文件（`.meta.json`）

UTF-8 编码的单个 JSON 对象，每次更新都以「临时文件 + `fsync` + `os.replace`」整体替换，
因此读到的永远是某一次完整写入的结果。

```json
{
  "schema_version": 2,
  "session_key": {
    "channel_id": "cli",
    "conversation_id": "local",
    "scope": "default"
  },
  "created_at": "2026-08-12T09:30:00+00:00",
  "updated_at": "2026-08-12T09:30:05+00:00",
  "compacted_through": 0,
  "committed_bytes": 612
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `schema_version` | int | 格式版本，当前为 `2`。语义变化必须递增 |
| `session_key` | object | 会话归属，与文件名同源；保留明文结构便于外部工具读取 |
| `created_at` | string | 首次写入时间 |
| `updated_at` | string | 最近一次写入时间 |
| `compacted_through` | int | 压缩水位，见 §5 |
| `committed_bytes` | int | 历史文件中**已提交**的 UTF-8 字节数，见 §4 |

`meta.json` 不存在即表示会话不存在；此时即使 `.jsonl` 在，也读作空会话。
`schema_version` 不等于当前版本时，实现必须报错。运行时只实现当前格式，不包含旧会话的
识别、兼容读取或迁移路径。

## 4. `committed_bytes`：整批原子性与半写恢复

历史文件是追加写的，而 `SessionStore.append` 要求**整批原子生效**。
本格式用 `committed_bytes` 表达这件事，规则只有两条：

- **读**：只有 `[0, committed_bytes)` 这段字节算数。超出的部分一律忽略。
- **写**：先把文件截断到 `committed_bytes`（丢弃上次崩溃留下的尾巴），追加整批并 `fsync`，
  **最后**才原子替换 `meta.json`。

因此进程在写入过程中被杀死时，只有两种可能的结果：`meta.json` 还没换掉，这一批整体不存在；
或者已经换掉，这一批整体存在。不会出现半条记录，也不会出现半批。

历史文件比 `committed_bytes` **短**是另一回事——那说明文件被外部截断或损坏，实现必须
报错（`PERSISTENCE_RECORD_CORRUPT`），不能当成「少了几条」。

## 5. 压缩与数据保留

压缩把前 `through` 条记录折成一条摘要。本实现的语义是：

- 摘要记录**插入在水位处**，原始记录**不删除**；
- `meta.json` 的 `compacted_through` 记为 `through`。

于是 `messages[:compacted_through]` 是已被摘要覆盖、不再逐条送入模型的部分，
`messages[compacted_through:]`（摘要打头）是仍然生效的部分。

```text
压缩前   m1 m2 m3 m4                     compacted_through = 0
compact(through=2, summary=S)
压缩后   m1 m2 S m3 m4                   compacted_through = 2
                ^ live_messages 从这里开始
```

保留语义因此是明确的三条：

| 操作 | 数据 |
| --- | --- |
| 压缩 | 原文保留在文件里，只是不再送进模型；水位**只能前进** |
| 删除 | 两个文件都被物理删除，不可撤销 |
| 过期 | 本实现**不做**自动过期；没有任何后台任务会动这些文件 |

## 6. 写给外部实现

一个只想**读**这份数据的外部工具需要做的事：

1. 按 `*.jsonl` 枚举目录，文件名主干反解出 `SessionKey`（按 `~` 切三段，百分号解码）；
2. 读同名的 `.meta.json`，确认 `schema_version == 2`，再取 `committed_bytes` 与
   `compacted_through`；
3. 只解析历史文件的前 `committed_bytes` 字节，按行解析记录。

一个想**写**的实现，额外要遵守 §4 的写入顺序。不遵守也能产生可读的文件，
但会丢掉整批原子性——崩溃时留下的半批会被后来的读者当成历史的一部分。

同一目录同时只应有一个写者。Karyvia 靠实例锁（`<instance_dir>/instance.lock`）
保证这一点，本格式**不**包含任何跨进程互斥机制。
