# karyvia-plugin-mcp

Karyvia 官方插件：把 [MCP](https://modelcontextprotocol.io) server 的工具接进实例。

```bash
pip install -e 'plugins/karyvia-plugin-mcp[client]'   # [client] 带上 mcp SDK
karyvia plugins enable mcp
```

## 配置

```jsonc
{
  "plugins": {
    "enabled": ["mcp"],
    "mcp": {
      "config": {
        "servers": {
          "files": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"]
          },
          "docs": {
            "type": "streamable_http",
            "url": "https://mcp.example.com/mcp",
            "headers": { "Authorization": "Bearer {api_key}" }
          }
        }
      },
      "secrets": { "api_key": "${MCP_TOKEN}" }
    }
  }
}
```

远端工具 `read_file` 在 server `files` 上，本地就叫 **`mcp.files.read_file`**。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `prefix` | `mcp` | 工具名第一段。改它要同时改 manifest 的命名空间声明 |
| `connect_timeout_ms` | `15000` | 每台 server 的连接 + `list_tools` 预算 |
| `call_timeout_ms` | `60000` | 单次远端调用预算 |
| `max_result_chars` | `30000` | 返回给模型的字符上限 |
| `servers.<name>.type` | `stdio` | `stdio` / `sse` / `streamable_http` |
| `servers.<name>.enabled` | `true` | |

`stdio` 必须配 `command`，两种 HTTP 传输必须配 `url`——少一项就是 `CONFIG_INVALID` 并指向
那个键，而不是启动时一条「连接超时」。

## 命名

`<prefix>.<server>.<tool>`，每一段都归一成契约允许的形状（小写、数字、下划线；
`^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$`）。

**归一化会丢信息**——`get-file` 与 `get_file` 撞成同一个名字。撞车时**各方都不注册**并写一条
日志，与 registry 对同名冲突的判定一致：选任何一边都是替用户做决定，而模型拿到一个
「名字对得上、行为却是另一个工具」的调用比少一个工具危险得多。

（这与同为官方插件的 `anthropic` 那条「工具名 `.` ↔ `-` 编码」不同：那是无碰撞双射，
这里不是。）

## 四条如实记着的边界

### 1. `side_effect` 恒为 `UNKNOWN`

MCP 协议不报告副作用：一个写文件的远端工具与一个只读的，在线格式上长得一模一样。
因此每一次调用都对编排层说「不知道外部世界变了没有」，`read_only` 恒为 `false`、
`risk` 恒为 `mutating`。

远端的 `annotations.readOnlyHint` 是**它自己说的**，而它正是那个不可信的一方。

失败分两档（`tool.py`）：发起**之前**失败（参数非法、会话不可用、入口取消）标 `NONE`；
发起之后失败（超时、传输中断）标 `UNKNOWN`。谎报 `NONE` 会让编排层以为可以安全重试一次
可能已经生效的写操作。

### 2. 插件自己拥有 MCP 连接

- `stdio` 要长驻子进程与管道，而 `ctx.shell` 是一次性 exec、拿不到 stdin 管道；
- HTTP 传输由 `mcp` SDK 自己开连接，一个字节都不过 `ctx.net` 的 SSRF 守卫。

**真正的边界是「你配了哪些 server」。** 一台 MCP server 能做什么，取决于它自己——
接一台 filesystem server 就等于把那个目录交给了模型。宿主资源门面是便利服务，
不是可信插件必须经过的代理。

### 3. 启动路径上多一次往返

连接发生在 `setup()` 里——registry 解析之后只读（`NFR-403`），没有第二个注册时机。因此每台
server 都会给冷启动加上它自己的连接时间，`connect_timeout_ms` 是上界，超时即跳过那台。

**单台连不上不致命**：记一条日志、跳过它的工具，其余照常。

### 4. 停止预算是每插件 5000 ms

`plugins.stop_timeout_ms`。一台赖着不退的 stdio server 会让本插件的停止超时，
`StopOutcome.timed_out` 如实标着——那时那个子进程可能还在跑。

连接由**一条后台任务**拥有（`supervisor.py`）：`mcp` 的三种传输都建在 anyio 的任务组上，
而任务组必须在进入它的那个任务里退出。在 `setup()` 里打开、在停止路径上关闭，会炸出
`Attempted to exit cancel scope in a different task`。

## 不做的事

| 不做 | 理由 |
| --- | --- |
| resources / prompts 桥接 | 新层没有对应的能力种类；伪装成工具会让模型拿到一堆语义不明的调用 |
| 热重载（旧实现的 `RUNTIME_CONTROL_MCP_RELOAD`） | registry 解析后只读、首版不热更新；只在自己这一层成立的「重载」会让 `karyvia capabilities` 说谎 |
| sampling（server 反向调模型） | 那是一条绕过主 Turn、`TurnLimits` 与取消链的模型调用通道 |
| OAuth 流程 | 目前只支持静态 header 鉴权 |

## 测试

```bash
python -m pytest plugins/karyvia-plugin-mcp -q
```

**不装 `mcp` 也必须全绿**：只有 `client.py` import 它，其余全部对 `session.py` 的两个
Protocol 编程。唯一需要碰 SDK 的用例是「没装它时说什么」。
