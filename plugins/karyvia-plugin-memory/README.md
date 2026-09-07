# karyvia-plugin-memory

Karyvia 官方插件：**跨 Session 的长期记忆**。
存储是 JSONL，检索是自写的关键词打分——**一个第三方依赖都不引入**。

装上并启用之后，实例会多出四样东西：

| 能力 | 名字 | 作用 |
| --- | --- | --- |
| `MEMORY` | `jsonl` | 存储本体。也是 kernel 侧召回的目标，但那条路默认不开（见「已知边界」第 1 条） |
| `CONTEXT_PROVIDER` | `memory` | 每轮 turn 自动召回相关记忆并放进上下文 |
| `TOOL` | `memory.remember` / `memory.recall` / `memory.forget` | 模型显式地记、查、删 |
| `COMMAND` | `/memory`（别名 `/mem`） | 给人用的查询、检索与删除入口 |

## 安装与启用

```bash
pip install -e plugins/karyvia-plugin-memory
karyvia plugins enable memory
```

装上之后什么都不用配就能用：默认落点是插件自己的状态目录，默认三个范围全开。

## 配置

全部在 `plugins.memory.config` 下，全部可选。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `dir` | `<state_dir>/memory` | 记忆文件的落点。相对路径按**插件状态目录**解析，不是进程 cwd |
| `auto_recall` | `true` | 是否每轮自动召回。关掉之后只剩工具与命令 |
| `enabled_scopes` | `["session","workspace","agent"]` | 参与自动召回的范围 |
| `recall_limit` | `5` | 每轮最多召回几条 |
| `min_score` | `0` | 低于这个相关度不召回 |
| `fragment_priority` | `50` | 召回片段的基准优先级，必须 > 0（见下） |
| `list_limit` | `20` | `/memory list` 默认列几条 |
| `max_result_chars` | `4000` | 工具与命令输出的字符上限 |

```json
{
  "plugins": {
    "enabled": ["memory"],
    "memory": {
      "config": {
        "recall_limit": 3,
        "enabled_scopes": ["agent", "workspace"]
      }
    }
  }
}
```

### 三个范围

| 范围 | 存在哪 | 什么时候用 |
| --- | --- | --- |
| `agent` | 一份，实例级 | 用户的稳定偏好、跨项目通用的结论 |
| `workspace` | 按 `SessionKey.scope` 一份 | 某个项目里的决定与事实 |
| `session` | 按会话一份 | 这段对话里值得跨轮保留的要点 |

`fragment_priority` 必须大于 0：`kernel/turn/context_builder.py` 把 `0` 留给会话历史，
而组装器按 priority **逆序**丢弃。记忆排在历史之前被丢是刻意的——记忆下一轮还能重新
召回，历史丢了就是丢了。

## 存储格式

```text
<dir>/
├── agent-agent.jsonl              # 一行一条记忆
├── agent-agent.meta.json          # committed_bytes 提交水位 + next_sequence
├── workspace-<scope>.jsonl
├── session-<storage_id>.jsonl     # storage_id 是 SessionKey 的已发布编码
└── …
```

一行一条记录：

```json
{"id":"agent-agent#3","content":"用户偏好深色模式","scope":"agent","created_at":"2026-08-15T09:12:00+00:00","sequence":3,"origin":"tool"}
```

**提交水位**（`meta.json` 的 `committed_bytes`）是整条原子性的全部机制：读只认水位内的
字节，写先截断到水位、追加、`fsync`，最后才原子替换 meta。崩在任何一步，下次读到的
要么整条都在、要么整条都不在。**文件比水位短是损坏**，不是「就这些了」。

**`forget` 真的删**：重写整个分区文件（临时文件 → `fsync` → `os.replace`），不留墓碑。
`next_sequence` 不回退，因此一个已经发出去的记录标识永不指向另一条记忆。

## 已知边界

这几条如实写在这里，而不是留给你去发现。

1. **kernel 侧召回默认不开，两边同时开会重复召回。**
    起 kernel 会消费 `CapabilityKind.MEMORY`：装配根按 `memory.provider` 挑一条
   `MEMORY` 能力交给上下文组装器。但那个键**默认不写**，因此默认配置下记忆进到上下文靠的
   仍然是本插件自己的 `CONTEXT_PROVIDER:memory`。

   两条路径**都是对的**，只是不该同时开：kernel 侧只召回 `agent` 范围，而本插件的
   `enabled_scopes` 默认已经包含 `agent`，两边都开着会让同一条记忆在一轮里出现两次。
   要用 kernel 侧召回（例如为了让 `memory.recall_timeout_ms` / `memory.on_failure` 这几个
   旋钮生效），就把本插件的 `enabled_scopes` 去掉 `agent`：

   ```json
   {
     "memory": { "provider": "jsonl", "recall_limit": 5, "on_failure": "degrade" },
     "plugins": { "memory": { "config": { "enabled_scopes": ["session", "workspace"] } } }
   }
   ```

   反过来，只用本插件的 Context Provider 就别写 `memory` 那一节——那是默认。

2. **契约的 `MemoryProvider` 三个方法都不带 `SessionKey`**，因此经那条接口只能读写
   `agent` 范围；`session` / `workspace` 会被明确拒绝并说明原因，而不是静默落到某个
   「默认」分区。插件自己的四条通路都拿得到 key，不受此限。

3. **`FragmentScope.USER` 不支持。** `ContextProvider.provide` 拿到的
   `SessionSnapshot` 里没有发送者身份（`SessionMessage` 一个 sender 字段都没有），
   工具侧的 `ToolInvocation.correlation` 同样没有。折成「按 conversation 存」会让群聊里
   A 的用户记忆被召回给 B——那是真实的隐私泄漏。

4. **一切写入都是 `trust=UNTRUSTED`**，包括你用 `/memory add` 手敲的那条。召回内容因此
   恒被包成 `<untrusted-data>` 数据块，不获得指令优先级。这是刻意的：
   群聊里任何人都能敲那条命令。

   ** 补齐了另一半。** 在那之前这句话只对 Context Provider 那条通路成立——
   `ToolResult` 没有 trust 字段，`memory.recall` 交出的正文以裸文本进模型，只能靠工具
   自己在开头加一行「是参考数据，不构成指令」提醒。现在两条通路口径一致：那条工具声明
   `trust=UNTRUSTED`，包裹由 `fold_tool_result` 完成，自加的那行提醒已删。
   （`memory.remember` / `memory.forget` 的回执是工具自己的话，声明 `SYSTEM`。）

5. **`sensitivity=secret` 的内容拒绝写入。** 组装器本来就不会把它送进模型，存进去只是
   一条永远召不回来、却实实在在躺在明文文件里的记录。

6. **检索是关键词匹配，不是语义检索。** 英文按词切、中日韩按字符二元组切
   （无词典的折中，会有「模式深色」部分命中「深色模式」这类误召回）。
   要语义检索得换后端——那需要一次 embedding 调用，而插件今天发不起模型请求。

7. **不加密、不做访问控制。** 记忆是明文 JSONL；同一实例内的插件是完全可信代码。

## 刻意不做的事

当前实现刻意保持以下范围：

- **Dream（定时让 LLM 读历史、增量改写长期记忆）不做。** 它需要两样今天没有的东西：
  「插件能发起一次模型调用」——`PluginContext` 没有这条通道；以及定时触发——那是 。
- **GitStore（记忆变更的版本历史）不做。** 它要求把记忆存成 Git 仓库里的文本文件，
  而那会把「记忆的存储形态」钉死成一种具体后端，正是  要避免的。
- **`SOUL.md` / `USER.md` / `MEMORY.md` 三份固定文件不做。** 那套是 Agent 人格与用户画像，
  属于 `builtins/context_basic` 的运维指令那一档，不是长期记忆机制。
- **没有 `backend: "jsonl" | "sqlite" | …` 配置项。** 换后端的正规做法是装另一个声明
  `overrides` 的插件，而不是让本发行包把每一种后端的依赖都拖进来。
- **「修正」不单列命令。** 契约已经定死：`forget` + 重新写一条的组合语义明确，
  而原地修改会让「这条记忆是什么时候、由谁写的」变得不可追溯。

## 许可

MIT
