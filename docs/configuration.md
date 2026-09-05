# 配置参考

实例布局、配置的四层优先级、`${VAR}` 凭据引用，以及**全部十个小节的逐字段表**。

这篇是字段的权威说明。怎么把实例跑起来见 [`getting-started.md`](./getting-started.md)，
命令参数见 [`cli.md`](./cli.md)。

> 这份文档里的字段表由 `tests/e2e/test_user_docs.py` 直接与
> `src/karyvia/kernel/config/schema.py::SECTION_SPECS` 逐项比对——那张表是
> `extra="forbid"` 的唯一依据，也是这里每一行的来源。漂移会让测试失败。

## 1. 实例布局

一个**实例**是一份配置 + 一份数据。命名实例统一落在
`~/.karyvia/instances/<实例名>/`：

```text
~/.karyvia/instances/default/
├── config.json            # 你的配置（karyvia init 建，karyvia plugins enable 改，加载路径只读）
├── config.schema.json     # 派生 JSON Schema，供编辑器补全，运行期忽略
├── instance.lock          # 实例锁，同一实例目录同时只跑一个进程
├── sessions/              # 内建会话存储（见 session-storage.md）
├── plugins/<插件 id>/     # 每个插件的状态目录（ctx.state_dir）
├── logs/events-<日期>.jsonl
└── workspace/             # 工作区：文件工具与 ctx.fs 的根
```

`KARYVIA_HOME` 可以改变数据根本身；它直接指向原本的 `~/.karyvia/` 这一层，命名
实例仍统一放在其 `instances/` 子目录。

选实例的三种方式，优先级从高到低：

| 方式 | 例子 |
| --- | --- |
| `--instance-dir <目录>` / `KARYVIA_INSTANCE_DIR` | 直接指定目录，跳过命名实例目录的推导 |
| `--instance <名字>` / `KARYVIA_INSTANCE` | 目录落在 `~/.karyvia/instances/` 下 |
| 都不给 | `~/.karyvia/instances/default/` |

**布局在配置之前解析**：要先知道 `config.json` 在哪才能读它。因此 `workspace.root`
只能改 workspace，永远改不了实例目录本身。实例名会成为一段路径分量，长度上限 64。

## 2. 四层优先级

```text
default  <  config.json  <  env  <  cli
```

这个顺序只在 `kernel/config/sources.py::collect_layers()` 的返回顺序里定义一次。
**内置默认值是完整的一层**，不是 dataclass 兜底——所以「这个值取自默认值」是一个查得到
的答案，而不是「查不到来源」：

```bash
karyvia config show --origins
```

三种覆盖方式：

| 层 | 怎么写 | 例子 |
| --- | --- | --- |
| `config.json` | 直接编辑 | `{"turn": {"max_iterations": 32}}` |
| `env` | `KARYVIA_CFG_<小节>__<字段>`，双下划线分层 | `KARYVIA_CFG_TURN__MAX_ITERATIONS=32` |
| `cli` | `--set <小节>.<字段>=<值>`，可重复 | `karyvia run --set turn.max_iterations=32` |

环境变量用**双下划线**分层是因为字段名本身就含下划线（`max_tool_calls_per_turn`），
单下划线分不出「层级」与「词间」。前缀 `KARYVIA_CFG_` 是通用形式，不为每个字段登记
一个专名——字段是会长的，而每加一个字段就要改一张映射表的设计注定会漏。
环境变量名统一转小写（环境变量惯例是大写，而配置字段是 snake_case）。

**`env` 与 `cli` 两层的值先按 JSON 解一次**：`32` 是整数、`false` 是布尔、
`["a","b"]` 是数组，解不动的就保持字符串（`gpt-4o-mini` 因此不用加引号）。
`sources.py` 不认识字段表，先试 JSON 再退化成字符串是唯一不需要提前知道目标类型的做法。

**`config.json` 永远不被加载路径回写**：你手写的键序与格式里有信息，任何「顺手规范化
一下」都会毁掉它。建它的是 `karyvia init`（`O_CREAT|O_EXCL`，没有 `--force`），
改它的只有 `karyvia plugins enable|disable|uninstall`。

## 3. `$schema`

`karyvia init` 生成的配置第一行是 `"$schema": "./config.schema.json"`。它**不是配置字段**：
编辑器读它做补全与校验，运行期忽略。它是顶层唯一被放行的非小节键，而且是**具名的一条**
——不是「`$` 开头就放行」，否则一个拼错成 `$turn` 的小节会静默消失。

## 4. 未知键

任何不在字段表里的键都是错误（`CONFIG_UNKNOWN_FIELD`），报错带 JSON Pointer 与
「你是不是想写……」的建议，而且**一次报出全部问题**——逐条抛会让你改一个键、重启、
再看到下一个。

全项目只有两处对未知键让路：顶层的 `$schema`，以及 `plugins` 小节里的插件 id。

## 5. `${VAR}` 凭据引用

配置里的任何字符串值只要出现 `${VAR}`（整串或内嵌，如 `Bearer ${TOKEN}`），
整个值就是**密钥**：明文只活在 `SecretStr` 里，配置树自始至终持有 `${VAR}` 字面量。

因此 `karyvia config show` 与 `/config` 印出来的是 `${OPENAI_API_KEY}` 而不是 `***`——
它告诉你去哪个变量里找，比一串星号有用。

四条硬规则：

- **没有 `${VAR:-默认值}` 这类 shell 回退。** 缺变量是硬错误，静默降级只会把故障推到
  第一次调用。
- **没有 `$${VAR}` 转义。** 半个机制不如没有。
- **空变量按缺失处理**，错误消息会区分「没导出」和「导成了空串」——两者的修法不同。
- **变量名限定为 `[A-Za-z_][A-Za-z0-9_]*`。** 放宽到任意字符会让 `${}`、`${a b}` 这类
  明显的笔误被当成合法引用，然后报「变量未设置」。

## 6. 逐字段表

下面十个小节就是 `SECTION_SPECS` 的全部。缺失的小节、缺失的字段都合法——每个字段都有
默认值，因此一份**完全没有** `config.json` 的实例也是合法状态。

### `turn` —— 一次 turn 的预算

字段名与 `LimitKind` 的取值一一对应。越界之后的终态由 `LIMIT_OUTCOMES` 定死，
不在别处另写一份判断。

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `max_iterations` | 正整数 | `16` | 一次 turn 最多几轮「模型 → 工具」循环 |
| `max_tool_calls_per_turn` | 正整数 | `48` | 一次 turn 最多发起几次工具调用 |
| `tool_timeout_ms` | 正整数 | `120000` | 单次工具调用的超时；**没有「永不超时」这个选项** |
| `tool_result_max_bytes` | 正整数 | `65536` | 工具结果进模型前的截断上界 |
| `turn_timeout_ms` | 正整数 | `900000` | 一次 turn 的总超时（看门狗，模型调用挂住时靠它叫停） |
| `context_max_tokens` | 正整数或 `null` | `null` | 上下文预算。`null` = **由模型能力推导**，不是「无限制」 |

### `workspace` —— 工作区位置

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `root` | 字符串或 `null` | `null` | 工作区根。`null` = 实例目录下的 `workspace/`。它是文件工具、`ctx.fs` 与 shell 工具 cwd 的边界 |

工具产出的文件也应落在这里。**这不只是约定**：
出站附件的 locator 必须是 **workspace 相对路径**（契约禁止附件依赖绝对路径），
落在 workspace 之外的文件因此发不到聊天平台上。

默认插件 `tools-file` 注册 `file.send`。它读取 workspace 相对路径、确认文件存在且不超过
`plugins.tools-file.config.max_file_bytes`（默认 `26214400`），然后把引用附加到本轮最终
回复；它不直接连接任何 Channel。

### `routing` —— 输入分流与 Session 并发

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `command_prefix` | 字符串 | `"/"` | 斜杠命令的前缀。它是路由的配置项，不是命令身份的一部分——改前缀不等于改全部命令声明 |
| `session_concurrency` | `"queue"` / `"merge"` / `"reject"` | `"queue"` | 同一 session 已有 turn 在跑时，下一条消息怎么办 |
| `queue_max_size` | 正整数 | `32` | 单个 session 的等待上限；超出即拒绝并回音，不静默丢弃 |
| `dedup_capacity` | 正整数 | `4096` | 去重缓存记多少条 message_id |
| `dedup_ttl_ms` | 正整数 | `600000` | 去重记录的存活时间 |
| `channel_concurrency` | 正整数 | `64` | 单条 Channel 的总在途消息上限；只限制 Runtime 任务总量，不改变 Session 并发策略 |

`session_concurrency` 的三个取值：`queue` 排队（严格 FIFO）、`merge` 把等待中的消息并成
下一批、`reject` 直接拒绝并回音。三者的差别**只在「拿不到槽位时怎么办」**——
「同一 session 同时至多一个 turn」这条不变量三种策略都成立。
Channel、Embed 与直接 API 提交共用这套策略；单个 Session 的等待上限只有
`queue_max_size` 一处。

旧配置 `channel_queue_max_size` 已移除；请删除该字段，并用 `queue_max_size` 设置单个
Session 的等待上限。保留旧字段会按未知配置拒绝启动，避免配置被静默忽略。

### `plugins` —— 实例级插件启用与加载

这个小节里，除了下面三个**保留键**，其余的键都是**插件 id**（见第 7 节）。
保留键与插件 id 撞不上：插件 id 只允许小写字母、数字与中划线，带下划线的键名永远不是
一个合法的插件 id。

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | 字符串数组 | `[]` | 显式启用的插件 id。**不在这张表里的候选连 manifest 都不会被读**（安装 ≠ 启用） |
| `disable` | 字符串数组 | `[]` | 显式禁用的提供方 id。压过 `enabled`，**对内建同样有效** |
| `stop_timeout_ms` | 正整数 | `5000` | 单个插件的停止预算。**按插件各算一份**；超时是放弃等待而不是等它结束 |

插件代码不属于实例配置。安装、更新和卸载由全局 `karyvia plugins` 命令管理，文件直接位于
`~/.karyvia/`；实例只保存启用、禁用、配置和状态。

### `hooks` —— Hook 分发超时

观察者是**整批**超时，拦截器是**每个 handler** 超时：前者并发执行、返回值被忽略，
整体拖不动 turn 就行；后者串行且能改流水线，一个慢 handler 会连累后面全部。

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `observer_timeout_ms` | 正整数 | `2000` | 一批观察者 Hook 的整体超时 |
| `interceptor_timeout_ms` | 正整数 | `5000` | 单个拦截器 Hook 的超时 |

### `context` —— 上下文组装

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `provider_timeout_ms` | 正整数 | `3000` | 单个 Context Provider 的独立超时。超时后记录故障并跳过 |
| `turn_compactor` | 字符串 | `"basic"` | `TURN_COMPACTOR` 能力的名字。该能力必选；写了却不存在或禁用唯一提供方都会使启动失败 |
| `turn_compactor_timeout_ms` | 正整数 | `120000` | 每次请求级压缩的超时上限；超时终止当前 Turn，不切换另一策略 |

注意 `context_max_tokens` **不在这里**：它是 turn 的六项预算之一。
`turn_compactor` 是唯一压缩能力。它在 Engine 每轮最终 `ModelRequest` 即将发送前检查
完整请求（包括消息、工具 schema、工具参数和 provider blocks）。预算已预留模型最大输出
空间、5% 安全余量，并把压缩目标设为触发线的 80%。超限时，策略只能用
摘要替换可压缩单元的连续前缀，工具调用与它的全部结果永远是同一单元。摘要立即用于当前
Turn；若它只覆盖初始 Session 的连续前缀，Kernel 会先追加本 Turn 的 Transcript，再调用
`SessionStore.compact()` 持久化摘要和水位。混入 Context、Memory 或本 Turn 工具往返的摘要
不会写入 Session。内建 `basic` 默认启用；策略运行失败、返回非法结果或压缩后仍超限时，
当前 Turn 明确失败，Kernel 不再执行隐式裁剪或回落到另一策略。Provider 返回实际输入
usage 时，Kernel 会把实际请求保存为相同 Session 的计量锚点；模型、工具、采样参数和消息
前缀保持一致的后续请求只估算新增尾部，任一项变化则恢复完整请求估算。若 Provider 仍以
结构化错误报告上下文超限，Kernel 会在尚未交付实质输出时强制压缩并重试一次。

### `memory` —— 长期记忆召回

**`provider = null` 是默认，含义是「不启用 kernel 侧召回」而不是「自动挑一个」**：
自动挑会让「装上一个记忆插件」悄悄改变每一轮请求的内容。

⚠️ **它与插件自带的 Context Provider 会叠加。** `karyvia-plugin-memory` 同时注册了
`MEMORY:jsonl` 与 `CONTEXT:memory`，后者默认已经召回 `agent` 范围——两边都开着会让同一条
记忆在一轮里出现两次。要用 kernel 侧召回就把那个插件的 `enabled_scopes` 去掉 `agent`，
或者干脆别写这一节。两条路径**都是对的**，只是不该同时开。

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `provider` | 字符串或 `null` | `null` | `MEMORY` 能力的名字（例如 `"jsonl"`）。`null` = 不启用；**写了却不存在是启动失败**，不是静默不启用 |
| `recall_limit` | 正整数 | `5` | 每轮最多召回几条。记忆与会话历史抢同一份预算 |
| `recall_timeout_ms` | 正整数 | `3000` | 一次召回的预算，超时按 `on_failure` 处置 |
| `fragment_priority` | 正整数 | `100` | 召回片段的 priority 下界；供 Context 拦截器与诊断读取，不参与 token 预算 |
| `on_failure` | `"degrade"` / `"fail"` | `"degrade"` | 后端故障时：`degrade` = 这一轮没有记忆、turn 照常跑；`fail` = turn 失败。**降级不等于静默**，错误一定会被报出去 |

`MemoryProvider` 的三个方法**一个 `SessionKey` 都不带**，因此经这条接口只能服务实例级
（`agent`）记忆——这是契约上的决定。会话级与工作区级归 `ContextProvider`。

### `model` —— 默认模型选择

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `provider` | 字符串或 `null` | `null` | `MODEL` 能力的名字（内建是 `"openai"`，Anthropic 插件是 `"anthropic"`）。`null` = 不指名，由 registry 交出的第一条生效；写了却没注册是启动失败 |
| `name` | 字符串或 `null` | `null` | 模型名，例如 `"gpt-4o-mini"`，交给 provider 解释。默认值虽是 `null`，但**它实际上必填**——没有它启动会失败并指向 `/model/name`，`karyvia init` 已经写好了一条 |

**凭据不在这里**，走 `plugins.<provider 插件 id>.secrets`（见第 7 节）。

### `retry` —— 模型请求的重试

一次限流（429）或网关抖动（503）**不该让整轮对话失败**。这一节说的是「失败之后怎么办」，
与 `turn` 那六项「一次 turn 能用掉多少」是两件事。

判定依据只有一个：Provider 在错误上标的 `retryable`。它已经分得很细——内建
`model-openai` 连 429 里的「限速」（可重试）与「欠费」（重试一万次也不会好）都分开标。
**取消类错误一律不重试**，无论它自称什么。

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `max_attempts` | 正整数 | `3` | **总尝试次数含第一次**，因此 `1` 就是「不重试」。刻意没有第二个 `enabled` 开关 |
| `base_delay_ms` | 正整数 | `500` | 指数退避的基数：第 n 次重试等 `base × 2^(n-1)`，再乘一个 `[0.5, 1.0]` 的抖动系数 |
| `max_delay_ms` | 正整数 | `8000` | 退避上界。供应商发了 `Retry-After` 时**用它说的值且不加抖动**——抖动只会把它往小了调，那意味着再吃一次 429 |
| `retry_empty_response` | 布尔 | `true` | 空回复（既无正文也无工具调用）算不算故障 |

**只在还没有任何输出流出去之前才会重来。** 一旦第一个正文分片发给了你，后面的失败就照
原样抛出——重发会让同一段答案出现两次。

`retry_empty_response=true` 时，连续 `max_attempts` 次空回复会让 turn `FAILED` 并告诉你
「模型连续 N 次返回空回答」。设成 `false` 则原样放行，那时**你什么都不会收到**而 turn 记
成完成——这是它默认为 `true` 的原因。内容过滤、撞上 `max_tokens` 造成的空回复**不算**
空回复故障：那是模型的决定，重发只会再来一次。

每一次失败的尝试都会发一条 `model.request_failed` 事件（带 `attempt` / `delay_ms` /
`retrying`），因此一次**成功的重试**在日志里看得见，而不是悄悄好了。

### `logging` —— 事件 sink

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `level` | 字符串 | `"info"` | 日志级别 |
| `file_enabled` | 布尔 | `true` | 是否写 `logs/events-<日期>.jsonl` |

## 7. 插件的配置块

`plugins` 小节里除三个保留键之外的每个键都是插件 id，它下面最多两个键：

```json
{
  "plugins": {
    "enabled": ["web", "model-openai"],

    "model-openai": {
      "config":  { "base_url": "https://api.openai.com/v1", "auth": "bearer" },
      "secrets": { "api_key": "${OPENAI_API_KEY}" }
    }
  }
}
```

| 键 | 作用 |
| --- | --- |
| `config` | 交给插件的配置。逐字段的校验用插件 manifest 自带的 `config_schema` |
| `secrets` | 凭据。值**只能是 `${VAR}` 形态的字符串**，明文由 `ctx.secret()` 在调用时从环境变量取 |

**`secrets` 与 `config` 分开是结构性保证**：凭据不在插件自己的配置块里，因此
`ctx.config` 交给插件的那份东西里根本没有可泄漏的内容。`model-openai` 的 `config_schema`
里也就没有 `api_key` 这个键。

`plugins.disable` 压过 `plugins.enabled`。一个外部插件同时出现在两张表中时，Runtime
不会读取或加载它，它声明的覆盖关系也不会进入本次启动；原本未被单独禁用的内建能力因此
正常生效。要重新启用插件，使用 `karyvia plugins enable <id>`，它会从 `disable` 中移除该 id。
