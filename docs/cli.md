# `nm` 命令参考

八个子命令的完整参数、退出码，以及每条命令**不做**什么。

字段含义见 [`configuration.md`](./configuration.md)，第一次跑起来见
[`getting-started.md`](./getting-started.md)。

> 这份文档列出的子命令由 `tests/e2e/test_user_docs.py` 与
> `src/nucleamind/runtime/cli/main.py` 的派发分支逐项比对，漏一条会让测试失败。

## 顶层

```text
nm <命令> [参数...]
```

| 选项 | 作用 |
| --- | --- |
| `--instance <名字>` | 选实例，默认 `default`（对应 `NUCLEAMIND_INSTANCE`） |
| `--instance-dir <目录>` | 直接指定实例目录，**压过** `--instance`（对应 `NUCLEAMIND_INSTANCE_DIR`） |
| `--set <小节>.<字段>=<值>` | 本次运行的临时配置覆盖，可重复；值按 JSON 解一次 |
| `-V`, `--version` | 打印版本 |
| `-h`, `--help` | 打印总说明 |

这三个选项对**每条**子命令都可用，摘出它们之后其余参数原样交给子命令。

### 公共退出码

| 码 | 含义 |
| --- | --- |
| `0` | 成功 |
| `1` | 命令跑完了但结论是失败（例如 `nm serve` 没有可服务的 Channel、`nm run -p` 的那一轮失败） |
| `2` | 参数错、配置错、未知命令——任何 `NucleaError` 都折成这个码，并打印 `user_message` 与 `detail`（两者都已脱敏） |
| `3` | 「无事可做」：`nm init` 时配置已存在、`nm plugins enable` 时本来就已启用、`purge` 没带 `--confirm` |
| `130` | 被 `Ctrl-C` 中断 |

**用户看到的不该是 traceback**：启动失败最常见的原因是配置写错或凭据没导出，
而那两件事的补救办法都写在错误的 `detail` 里。

## `nm init`

```text
nm init
```

在实例目录里生成 `config.json` 与 `config.schema.json`。

- **既有 `config.json` 一个字节都不动**，这时退出码是 `3`——一个已经有配置的实例不需要
  被初始化，退让并把路径印出来比什么都不说有用。
- 用 `O_CREAT|O_EXCL` 建文件，因此**没有 `--force`**：一个能覆盖用户配置的旋钮就是那条
  需求的反面；两个 `nm init` 同时跑也不会有一个把另一个刚写的盖掉。
- `config.schema.json` 是**我们生成的**，不是你的资产：内容与当前字段表不一致就会被刷新
  （内容相同则一个字节都不写，免得每次 `nm init` 都改一次 mtime）。
- **不取实例锁**：生成配置与「另一个实例正在跑」无关。

## `nm run`

```text
nm run [-p 提示词] [--reasoning]
```

装配实例并把进程交给 CLI 入口能力。

| 选项 | 作用 |
| --- | --- |
| `-p`, `--prompt TEXT` | 单次执行：跑完这一条就退出 |
| `--reasoning` | 显示模型的推理片段 |

不带 `-p` 时进入交互式会话：每行输入是一轮对话，输入 `/exit` 或 `/quit` 退出。

**`-p` 的退出码反映那一轮的终态**：`0` 正常结束（含撞上预算上限）、`130` 被中断、
`1` 失败。

**两次 `Ctrl-C` 的语义**：第一次在有 turn 在跑时**取消那些 turn**，会话继续；
没有 turn 在跑时它就是「退出」。第二次请求入口的取消令牌，先跑一遍 `stop()`
（实例锁因此正常释放）再强制结束进程——读 stdin 的线程没有可移植的唤醒方式，
干等它只会让你按第三次。

**首次运行只生成配置就退出**（退出码 `0`）：紧接着进会话看起来更顺手，却会让同一条命令
有两种结局，取决于你有没有提前 export 那个环境变量。首次运行恰恰是最需要确定性的时刻。

`-p` / `--reasoning` 由 **CLI 入口能力**解析，不是 `nm` 本身——那条能力可以被插件覆盖。

## `nm serve`

```text
nm serve [--host <地址>] [--port <端口>]
```

无头模式：装配实例 → `start()` → 等信号 → `stop()`。它启动**全部已启用的 Channel 能力**
并常驻。

- **它是通用的，不是给某一个插件写的**：HTTP 接口、飞书、cron 调度器都用这一条。
- ⚠️ **`--host` / `--port` 覆盖的是 `openai-api` 插件的配置块**
  （等价于 `--set plugins.openai-api.config.port=...`）。这条命令本身不认识任何协议，
  但这两个参数是网络 Channel 的公分母、而 `--set` 的完整路径写起来太长。
  别的 Channel 的监听参数一律走 `--set`。
- 没有任何可服务的 Channel 时退出码 `1` 并指路 `nm plugins list`。
- `Ctrl-C` **只有一档**（这里没有阻塞在 `readline()` 的线程）：按一次就干净地走完
  `stop()`，在跑的 turn 先被请求取消、已产生的内容因此落库。停止过程中再按一次才强制退出。
- 配置文件不存在时与 `nm run` 走完全同一条首次运行分支：只生成、只指路。

## `nm config show`

```text
nm config show [--origins] [--json]
```

| 选项 | 作用 |
| --- | --- |
| `--origins` | 逐字段打印来源（`default` / `config.json` / `env` / `cli`） |
| `--json` | 输出 JSON 而不是文本 |

**只读，不写回任何文件，不取实例锁**——看一眼配置不该与正在跑的实例互斥。

明文凭据**结构性地**不在输出里：配置树自始至终持有 `${VAR}` 字面量，
所以你会看到 `${OPENAI_API_KEY}` 而不是 `***`。

## `nm session`

```text
nm session list
nm session show <会话 id>
```

会话 id 就是 `SessionKey.storage_id()`，形如 `cli~local~default`；`list` 会把它印出来。

- **只装会话存储那一条能力**：一条只读诊断不该因为模型凭据没导出而失败，
  也不该跟正在跑的实例抢实例锁。
- 读的是**生效的** `SessionStore`——被插件覆盖过就读插件那份。
- **没有删除或压缩**：那是有副作用的操作，要单独的确认流程。

## `nm plugins`

```text
nm plugins install   <包名、路径或 URL>
nm plugins update    <插件 id>
nm plugins uninstall <插件 id>
nm plugins list [--json]
nm plugins enable    <插件 id>
nm plugins disable   <插件 id>
nm plugins purge     <插件 id> --confirm
```

| 子命令 | 做什么 |
| --- | --- |
| `install` | 把插件全局安装到 `~/.nucleamind/plugin-packages/<id>/`，验证逻辑依赖和 Python 发行包版本集合，不自动为任何实例启用 |
| `update` | 从安装记录里的来源更新全局插件；依赖集合不兼容时保持旧版本 |
| `uninstall` | 无其他插件依赖它时删除全局代码，并事务性清除所有已知实例中的配置引用；实例状态目录默认保留 |
| `list` | 列出已发现的插件、状态、版本与能力（内建不在这张表里，看 `nm capabilities`） |
| `enable` | 外部插件写入 `plugins.enabled` 并从 `plugins.disable` 移除；内建插件本来就存在，因此只撤销 `disable`，不会伪造成外部安装引用 |
| `disable` | 写入 `plugins.disable`，**不动 `enabled`**（这样 `enable` 才是它的逆操作）。对内建同样有效 |
| `purge` | 删除插件的状态目录 |

- `install` / `update` / `uninstall` 是**全局操作**，不接受 `--instance`、
  `--instance-dir` 或 `--set`。执行时必须先停止所有实例；这样运行中的实例不会突然失去
  已导入代码。全局文件直接位于 `~/.nucleamind/`，没有额外的 `global/` 目录。
- 默认安装会记录插件目录中实际解析出的 Python 发行包版本；两个插件需要同一发行包的不同
  版本时，安装或更新会拒绝发布。`install --no-deps` 表示只安装插件自身，其余 Python 依赖
  由启动 `nm` 的环境负责，而不是由插件管理器检查。
- 外部插件 id 不能冒用内建插件 id；替换内建能力应在 manifest 中声明 `overrides`。
- 卸载仍被其他已安装插件依赖的插件会被拒绝。卸载写多个实例配置时，任何一步失败都会恢复
  已经修改的配置，并保留全局插件。
- `enable` / `disable` **只改配置，不在当前进程生效**（首版不热更新）。每次改动的输出
  都带这句话。
- 禁用一个**覆盖过别的能力**的插件时，输出会提前告诉你还得在
  `plugins.<id>.on_disable` 里表态（`restore_builtin` / `leave_missing`），
  否则下一次启动会以 `CONFIG_INVALID` 失败。
- **`purge` 是唯一会删用户数据的地方**：没带 `--confirm` 就只打印路径与体积、
  一个字节都不删（退出码 `3`）。路径与体积在确认**之前**打印——一句「确定吗」
  不足以让你知道自己将要失去什么。
- `list` / `enable` / `disable` / `purge` 是实例操作；前三者的改动要等实例重启后生效。

**`nm plugins enable|disable|uninstall` 是 `config.json` 唯一的修改点**
（`nm init` 是唯一的创建点，加载路径只读）。

## `nm capabilities`

```text
nm capabilities [--json]
```

跑一次只读装配，打印覆盖解析报告的四段：**生效 / 被覆盖 / 已禁用 / 冲突**，
每条都带提供方标识。

- **覆盖不静默**：一个插件替掉了内建的会话存储，你必须能一眼看到
  `(builtin:jsonl, plugin:session-pg)` 这对关系。因此「被覆盖」那段即使为空也照印——
  「零条」是一条有价值的结论。
- **冲突印出来而不是抛出去**：对这条命令来说冲突恰恰是要看的东西，
  把它折成一条退出码 2 的诊断只会少印另外三段。
- 与 `nm plugins list` / `nm session` 一样走只读诊断路径：不取实例锁、不写业务状态、
  不装编排器、不做启动期的「必需能力」判定。

## 斜杠命令（会话里）

它们**不是** `nm` 的子命令，而是在对话里输入的、由分流器路由的命令。内建六条：

```text
/help  /config  /session  /plugins  /capabilities  /cancel
```

前缀由 `routing.command_prefix` 配置（默认 `/`）。插件可以注册自己的命令
（`memory` 插件的 `/memory`、`cron` 插件的 `/cron`），走的是完全同一条分流路径——
内建在这里没有任何特权。

`/cancel` **显式拒绝取消自己所在的那一轮**（它正持有 session 槽位，取消自己会让输出发不
出去），不带参数时只列出而不是取消全部。

> Git Bash 下敲斜杠命令要带 `MSYS_NO_PATHCONV=1`，否则 `/memory` 会被 MSYS 的路径转换
> 改写成一个 Windows 路径，看起来像是命令没有被分流。
