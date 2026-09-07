# karyvia-plugin-feishu

Karyvia 官方插件：**飞书 / Lark Channel**。收消息、跑 turn、用 CardKit 流式卡片把回复
一边生成一边更新进同一张卡里。

## 安装与启用

```bash
pip install 'karyvia-plugin-feishu[gateway]'   # gateway extra 带上 lark-oapi
karyvia plugins enable feishu
karyvia serve
```

装上不等于启用（`plugins.enabled` 是唯一闸门），改完配置要重启实例。

## 准备一个飞书应用

在[飞书开放平台](https://open.feishu.cn/)自建应用，拿到 `App ID` 与 `App Secret`，然后：

1. **事件订阅**选「**长连接**」（本插件不支持 webhook，见「已知边界」）；
2. 订阅 `im.message.receive_v1` 事件；
3. 开权限：`im:message`、`im:message:send_as_bot`、`im:resource`（读附件）、
   `im:message.reaction:write`（打反应）；建议再开 `bot:info` 让插件能取到自己的 open_id
   （拿不到时群聊 @ 门控会走兜底启发式，见下）。

本插件只支持在飞书开放平台手动创建的应用，不调用未公开的注册端点。

## 最小配置

```jsonc
{
  "plugins": {
    "enabled": ["feishu"],
    "feishu": {
      "secrets": {
        "app_id": "${FEISHU_APP_ID}",
        "app_secret": "${FEISHU_APP_SECRET}"
      },
      "config": {
        "allow_from": ["ou_xxxxxxxxxxxx"],
        "operators": ["ou_xxxxxxxxxxxx"]
      }
    }
  }
}
```

**`app_id` 也走 `secrets`**（虽然它本身不敏感）：`config` 块不解析 `${VAR}`，写在那里会
让你拿到字面串 `"${FEISHU_APP_ID}"` 并在连接时得到一个无法诊断的 401。凭据是一对，
就一起走凭据通道。

## 配置项

全部落在 `plugins.feishu.config` 下。

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `channel_id` | string | `feishu` | `SessionKey` 第一分量与出站路由键 |
| `domain` | `feishu` \| `lark` | `feishu` | 飞书还是 Lark（国际版）。填错会连到另一个租户体系上 |
| `allow_from` | string[] | `[]` | 发送者 open_id 白名单。**空 = 允许所有** |
| `allow_chats` | string[] | `[]` | 会话 chat_id 白名单，空 = 全部 |
| `operators` | string[] | `[]` | 谁是 operator。`/config` 这类命令只对名单里的人可用；**默认空 = 无人** |
| `group_policy` | `mention` \| `open` | `mention` | 群聊门控。`mention` 下四条命中路径见下 |
| `topic_isolation` | bool | `true` | 群聊里**每个话题一份会话历史**；关掉则整个群共用一份 |
| `reply_to_message` | bool | `false` | 开启后机器人会以「回复」形式发言，**并因此新建话题** |
| `streaming` | bool | `true` | 关掉就只在终态发一条完整消息 |
| `stream_edit_interval_ms` | integer ≥100 | `500` | 两次卡片更新之间的最小间隔 |
| `react_emoji` | string | `THUMBSUP` | 收到消息时打的反应；空串 = 不打 |
| `done_emoji` | string | `""` | 回答完成后补打的反应 |
| `tool_hint_prefix` | string | `🔧` | 工具进度提示的行前缀；**空串 = 关闭**（连那条后台泵都不派生） |

Secret 两条：`app_id`、`app_secret`，缺任一即 `CONFIG_INVALID` 并指向那一个键。

## 行为

- **消息格式自动选**：代码块 / 表格 / 标题 / 长文 / 粗斜体 / 列表 → 卡片；带链接 → 富文本；
  短纯文本 → 文本。
- **一张卡片最多一个表格**：飞书对含多个表格的卡片直接报错（11310），因此一段带三个表格
  的回答会变成三条卡片消息——每个表格都送达。
- **流式**：建一张 CardKit 流式卡片，按节流更新它，结束时**显式关掉流式模式**
  （不关会让会话列表永久显示「生成中」）。整条链失败会回落成普通卡片。
- **话题即会话**：群聊里 `topic_isolation` 开启时，每个话题有自己的历史；`conversation_id`
  是 `chat_id:话题根` 的合成串。
- **入站附件不下载**：飞书的资源没有公开 URL，只能用 `message_id + file_key` 换取，
  因此存成 `AttachmentSource.OPAQUE` 引用 + 正文标记，不在归一化阶段落盘。
- **中断与失败有明确标记**：正文尾部追加 `[已中断：以上是中断前已产生的内容]` 或
  `[本轮失败]`，与终端里看到的**逐字相同**。
- **工具进度提示**：模型每发起一次工具调用，卡片里插一行 `🔧 fs.read`；同一批里相邻的
  同名调用折叠成 `🔧 fs.read × 5`。它订阅的是 `tool.call_started` 事件而不是出站流
  （出站消息里没有工具信息），终态时被完整答案替换。
- **群聊 @ 门控的四条命中路径**：`group_policy: open`；正文含 `@_all`；@ 到了 bot；
  以及**拿不到 bot 身份时的兜底**——一条没有 `user_id` 且 open_id 以 `ou_` 开头的 @ 被当成
  机器人。兜底的代价是同群另一个 bot 被 @ 时会误命中一次；不要它的代价是没配
  `bot:info` 权限时**整个群聊功能静默失效**。

## 已知边界

- **只支持长连接（WebSocket），不支持 webhook。** 因此没有只对 webhook 有意义的
  `encryptKey` / `verificationToken` 配置项。
- **一个实例只接一个飞书应用。** 要运行多个应用，请创建多个 Karyvia 实例；会话、日志与
  插件状态会彼此隔离。
- **换了 `app_id` 之后 `allow_from` 里的 open_id 全部失效**，必须重新填写（open_id 按应用
  隔离）。旧名单不会匹配新应用，因此保持 fail-closed。
- **陌生人的私聊被静默忽略**。要允许其使用，请把 open_id 写进 `allow_from`。
- **没有飞书原生命令**。命令只有一个来源（`routing.command_prefix`），在飞书里打 `/help`
  就是一条普通消息，由内建 `commands-core` 处理。
- **不做语音转写**：语音消息只留 `OPAQUE` 引用 + `[audio]` 标记。
- **出站 workspace 附件传不出去**（`FileAccess` 没有 `read_bytes`），发一条文本标记。
- **工具提示只有工具名，没有参数。** Channel 拿得到的只有 `tool.call_started` 的载荷
  `{"tool", "call_id"}`。要恢复参数级细节就得把工具参数（其中有文件内容、绝对路径与
  shell 命令）放进一条会被全部订阅者看到、会落进事件日志、还会被发到聊天平台上的载荷里
  ——那是一次要单独评审的脱敏决定。
- **不处理表情回应与已读回执事件**。
- **不做扫码建应用**：请在开放平台自行创建应用。
- **插件自己拥有平台连接**：`lark-oapi` 直接建立 WebSocket 与 HTTPS 连接；宿主资源门面
  不是强制网络代理。启用插件就是信任其代码。
- **Channel 准入不是进程隔离**：能在允许的会话里说话的人就能驱动实例上的全部工具，
  包括 `shell.exec`。`allow_from` 与 `allow_chats` 是唯一的闸门。
- **依赖 lark-oapi 的四个内部接口**（`_connect` / `_disconnect` / `_ping_loop` /
  `_auto_reconnect`）：它的 `Client.start` 是阻塞的且没有 `stop`，干净地连上并断开
  只能这么做。版本上界锁在 `<2.0.0`，缺任何一个接口会在启动时报一句能照做的错误
  而不是 `AttributeError`。

## 并发

同一会话的 turn 严格按到达顺序串行，**不同会话并发**（泵扇出）。
话题隔离开启时每个话题是独立会话，因此群里不同话题的对话互不阻塞。

## 开发

```bash
pip install --no-deps -e plugins/karyvia-plugin-feishu
python -m pytest plugins/karyvia-plugin-feishu -q
```

**绝大多数用例不需要装 `lark-oapi`**：`gateway.py` 与 `client.py` 是仅有的两个接触 SDK 的
模块（有一条 AST 用例钉住这件事），其余全是纯函数或对 Protocol 编程。`tests/conftest.py`
有一条 autouse 的网络闸门盯着「一个 socket 都不开」。
