# karyvia-plugin-anthropic

Karyvia 官方插件：**Anthropic 原生 Messages API 的 Model Provider**。

它提供一条 `MODEL` 能力（能力名 `anthropic`），与内建的 `model-openai`（能力名 `openai`）
**并存**——不是取代它。OpenAI 兼容层能连到 Anthropic 的中转，但 prompt caching 的断点、
thinking 的四种形态与 `stop_sequence` 这个终止原因在那条路上表达不出来，这三样就是本插件
存在的理由。

## 安装与启用

```bash
pip install karyvia-plugin-anthropic     # 开发中：pip install --no-deps -e plugins/karyvia-plugin-anthropic
karyvia plugins enable anthropic
```

装上不等于启用（`plugins.enabled` 是唯一闸门），改完配置要重启实例才生效。
用 `karyvia plugins list` 与 `karyvia capabilities` 确认它被发现、被加载、能力真的生效了。

## 最小配置

```jsonc
{
  "model": { "provider": "anthropic", "model_id": "claude-sonnet-4-5" },
  "plugins": {
    "enabled": ["anthropic"],
    "anthropic": {
      "secrets": { "api_key": "${ANTHROPIC_API_KEY}" }
    }
  }
}
```

凭据只能以 `${VAR}` 引用出现（明文不进配置文档）。`auth` 默认 `x_api_key`，
即 Anthropic 官方的 `x-api-key:` 头。

## 配置项

全部落在 `plugins.anthropic.config` 下。

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `base_url` | string | `https://api.anthropic.com/v1` | **原样使用不追加后缀**，请求路径固定 `/messages`。中转（MiniMax 的 `https://api.minimax.io/anthropic`、各家 Anthropic 兼容端点）改这一行即可 |
| `auth` | `x_api_key` \| `bearer` \| `none` | `x_api_key` | OAuth 令牌用 `bearer`；`none` 给本地 relay，此时**完全不取用** `api_key` |
| `anthropic_version` | string | `2023-06-01` | `anthropic-version` 头，必填头，中转可能钉别的值 |
| `beta_headers` | string[] | `[]` | 逗号连接后作为 `anthropic-beta`。需要某个 beta 特性的人填一行，本插件**不维护一张 beta 特性表** |
| `models` | object | `{}` | 非空即**白名单**：不在表里的 model_id 直接 `CAPABILITY_MISSING` |
| `default_context_window_tokens` | int | `200000` | `models` 未覆盖时的窗口 |
| `default_max_output_tokens` | int | `8192` | Anthropic 的 `max_tokens` 是**必填**字段，没有「用服务端默认」这一档 |
| `capabilities` | string[] | `["tool_calls","streaming"]` | 能力基线。`reasoning` 与 `prompt_caching` **不能写在这里**，它们由下面两个开关派生 |
| `supports_temperature` | bool | `true` | `false` 时 `temperature` 被**省略而不是钳值**。Opus 4.7+ / Sonnet 5 对它直接 400 |
| `thinking` | object | `{"mode":"off"}` | 见下 |
| `effort` | `low`\|`medium`\|`high`\|`xhigh`\|`max` | 缺席 | 配了才发 `output_config.effort` |
| `prompt_caching` | object | `{"enabled":false}` | 见下 |
| `request_timeout_ms` | int | `120000` | 单次请求超时 |
| `stream_idle_timeout_ms` | int | `60000` | 流空闲看门狗。请求级超时保护不了「开了口就不再吐字」的流 |

`models.<id>` 可以逐模型覆盖：`context_window_tokens` / `max_output_tokens` /
`capabilities` / `supports_temperature` / `thinking` / `effort` / `prompt_caching`。

### `thinking`

```jsonc
"thinking": { "mode": "adaptive", "display": "summarized" }
```

`mode` 的四个取值一一对应线格式的四种形状，**由你按自己在用的模型选，本插件不按模型名猜**：

| mode | 发出去的 | 用在 |
| --- | --- | --- |
| `off`（默认） | 完全不发 `thinking` 字段 | 不关心，或模型不支持 |
| `adaptive` | `{"type":"adaptive"}` | 4.6 及更新的模型，推荐 |
| `budget` | `{"type":"enabled","budget_tokens":N}` | 4.5 及更早 |
| `disabled` | `{"type":"disabled"}` | 默认就思考的模型上显式关掉 |

`mode="budget"` 时 `budget_tokens` 必须**小于** `max_output_tokens`，否则以
`CONFIG_INVALID` 拒绝——本插件不会替你把输出上限抬高，那会让生效的上限不是你配的那个。

开了 `adaptive` 或 `budget` 之后，`describe` 交出的能力集自动包含 `reasoning`。

### `prompt_caching`

```jsonc
"prompt_caching": {
  "enabled": true,
  "ttl": "5m",
  "breakpoints": { "system": true, "tools": true, "history": true }
}
```

开启后在三个位置挂 `cache_control: {"type":"ephemeral"}`：最后一条工具定义、最后一个
system 块、`messages[-2]` 的最后一个块。分界点选在这里是为了把**稳定前缀**（工具、系统
指令、上一轮之前的历史）与本轮变动分开。断点最多 3 个，不会撞上 Anthropic 每请求 4 个的
上限。缓存是否真的写入可以在 `model.response_received` 事件的
`provider_metadata.cache_creation_input_tokens` 里看到。

开启后 `describe` 的能力集自动包含 `prompt_caching`。

## 用量口径

`TokenUsage.input_tokens` 是 **`input_tokens + cache_creation_input_tokens +
cache_read_input_tokens` 三项之和**——线格式里的 `input_tokens` 只是未命中缓存的余量，
不相加会让报出去的输入量凭空少一大截。`cached_input_tokens` 取 `cache_read_input_tokens`。

`reasoning_tokens` **恒为 0**：Anthropic 不单独报思考 token（它们含在 `output_tokens`
里），本插件**不估算**——一个猜出来的数字会被当成实测值写进事件日志。

## 已知边界

这几条如实记在这里，不留给你去发现：

- **thinking 块的回放只活到本轮 turn 结束。**当前 thinking 块（含 `signature`）会被
  原样回传，因此 thinking 与工具调用可以同时用（在此之前不行——那是相对被删除的
  Provider 所需的原生能力）。但 opaque 块不进
  `SessionMessage`，跨 turn 拿不回来。需要回放的场景全都是同一条 turn 内的工具循环，
  因此这够用；真要跨 turn 得先决定一份加密的思考签名该不该成为用户资产。
- **缺 `signature` 的 thinking 块与别家产出的 opaque 块都会被跳过。** 前者 Anthropic 直接
  拒绝，后者的载荷形状是私有的。
- **不支持图像与文档输入。** `ModelMessage.content` 是纯字符串，契约层没有多模态位置。
- **不做重试与故障转移。** 重试是编排层的策略，本插件只把 `retryable` 与 `retry_after_ms`
  如实标在 `KaryviaError` 上。两处都做会叠成一个放大器。
- **不声明任何 server tool**（web_search / code_execution 等）：它们会绕过 `ToolExecutor`，
  等于给模型开一条不受 `TurnLimits` 约束的副作用通道。
- **不提供 Bedrock / Vertex / Foundry 接入**：它们各自要 SigV4 / ADC / Entra 认证，
  是三套机制而不是一个 `base_url`。

## 信任边界

插件作为受信任代码在宿主进程内运行。网络请求和密钥读取使用 `PluginContext` 的统一服务，
但这些服务不是权限隔离；需要运行不可信代码时应使用进程外隔离。

## 开发

```bash
pip install --no-deps -e plugins/karyvia-plugin-anthropic
python -m pytest plugins/karyvia-plugin-anthropic -q
```

整套用例走 `httpx.MockTransport`，一个 socket 都不开（`tests/conftest.py` 里有一条 autouse
的网络闸门盯着这件事）。`ModelProviderContract` 是 `karyvia.sdk.testing` 提供的契约测试
基类，内建 `model-openai` 用的是同一个。
