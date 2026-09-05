# karyvia-plugin-cron

Karyvia 官方插件：**定时任务 / Automation**。到点时把一段指令作为一条新消息发给
Agent，结果自动回到你创建这条任务的那个会话。

一份 manifest 五条能力：

| 能力 | 名字 | 作用 |
| --- | --- | --- |
| `CHANNEL` | `cron` | 调度器本体。到期时产出一条入站消息 |
| `TOOL` | `cron.schedule` | 模型自己排期 |
| `TOOL` | `cron.list` | 列出**本会话**已排期的任务 |
| `TOOL` | `cron.cancel` | 取消**本会话**的一个任务 |
| `COMMAND` | `/cron` | 给人用的查看 / 暂停 / 立即运行 / 删除 |

## 安装与启用

```bash
pip install karyvia-plugin-cron
karyvia plugins enable cron
karyvia serve            # 常驻；karyvia run（CLI 交互）同样会跑调度循环
```

`karyvia capabilities` 里应当能看到上表那五条。

## 怎么用

在你希望结果出现的那个会话里，直接对 Agent 说：

```text
每个工作日早上 9 点，看一眼今天的日程，把要紧的告诉我。
明天下午 4 点提醒我发版本说明。
每 30 分钟检查一次构建状态。
```

Agent 会调 `cron.schedule` 把它排上。之后用 `/cron` 管理：

```text
/cron                    列出可用的子命令
/cron list               本会话的任务
/cron list all           全部会话的任务（需要管理员）
/cron show <标识>        详情与运行历史
/cron pause <标识>       暂停
/cron resume <标识>      恢复（下一次运行时刻按「现在」重算）
/cron run <标识>         排到最近一次运行
/cron rm <标识>          删除
```

**`/cron` 没有 `add` 子命令**，这是刻意的：排期要写一段给模型读的指令、挑一种调度、
可能还要选时区，让模型代劳比让人在一行命令里拼参数好。

## 三种调度

| 形态 | 参数 | 例子 |
| --- | --- | --- |
| 间隔 | `every_seconds` | `1800`（每半小时）。下界见 `min_interval_ms` |
| cron 表达式 | `cron_expr` + 可选 `tz` | `0 9 * * 1-5`（工作日 9 点） |
| 一次性 | `at` | `2026-08-20T16:00:00`；不带时区时按默认时区解释 |

cron 表达式是标准 5 字段（分 时 日 月 周），**由本插件自己解析**，支持
`*` / `n` / `a-b` / `a-b/n` / `*/n` / 逗号列表，以及三字母的月份与星期名。
星期 `0` 与 `7` 都是周日。日与星期都被限定时取**并集**（POSIX 传统语义：
`0 0 13 * 5` 是「每月 13 号**或**每个周五」）。

**不支持** croniter 的扩展语法（`L` / `W` / `#` / `?` / `@daily` / 秒字段）——
写了会直接报错，不会被静默当成字面量。

## 配置

```jsonc
{
  "plugins": {
    "cron": {
      "config": {
        "dir": "",                    // jobs.json 的落点，留空即 <state_dir>/cron
        "timezone": "Asia/Shanghai",  // cron 表达式的默认时区，留空即本机时区
        "tick_ceiling_ms": 60000,     // 调度循环的睡眠上界（兜系统时钟跳变）
        "min_interval_ms": 10000,     // 间隔任务的下界
        "catch_up_window_ms": 0,      // 停机期间错过的运行补不补，见下
        "max_jobs": 100,
        "instance_id": "default"      // 注入消息上标注的实例标识
      }
    }
  }
}
```

Windows 上用命名时区需要 `tzdata`（本包已按平台声明依赖）。

### 错过的运行

进程停了三天再起来，不该炸出三天份的提醒。`catch_up_window_ms` 一个旋钮表达两种行为：

- `0`（默认）：错过的一律不补，按「现在」重算下一次。
- `> 0`：错过的到期时刻落在窗口内则**补跑一次**（不是逐次补齐）。

一次性任务过期且不在窗口内时会被标成 `missed` 并停用，`/cron show` 里看得到——
不会静默消失。

## 三条如实记着的边界

- **原 Channel 没加载时，到期 turn 的输出会被静默丢弃。** 出站消息按 `channel_id`
  路由回创建这条任务的那个 Channel；那个插件这次没启用（比如任务是在 `karyvia run` 的 CLI 里
  排的，而现在跑的是 `karyvia serve`），Kernel 就没有可投递的去处。turn 仍然跑完并完整入库。
  `/cron list all` 会把每条任务的投递目标印出来，这是唯一看得见的线索。
- **运行历史记的是「派发」，不是 turn 的成败。** Channel 泵吞掉 `TurnReceipt`，插件看不到
  turn 的结局。`dispatched` 的意思是「消息已经交给 Kernel」，不是「任务做成了」。
- **任务正文以命令前缀（默认 `/`）开头时会被当成命令分流**，而注入消息的发送者
  `is_operator=False`，因此 operator-only 命令会被拒。本插件不额外拦这件事。

## 刻意不做的事

- **heartbeat**（参考实现的 `HEARTBEAT.md`）：它是「定时 + 一段固定提示词 + 只在有结论时
  才说话」，用一条普通任务加一句「没有要紧的就回一个字：无」就能表达。
- **local trigger**（`nanobot trigger <id> "..."`）：它要一个进程外的入队通道与至少一次
  投递语义，那是另一件事，不该塞进本插件。
- **第三方依赖**：不用 `croniter`（表达式自己解析）、不用 `filelock`
  （写盘走「临时文件 → `fsync` → `os.replace`」）。唯一的例外是 Windows 上的 `tzdata`。

## 存储

`<state_dir>/cron/jobs.json`，整份原子重写：

```json
{
  "version": 1,
  "updated_at": "2026-08-15T02:00:00+00:00",
  "jobs": [
    {
      "id": "cj-3f9a2c81de",
      "name": "工作日晨会提醒",
      "message": "看一眼今天的日程，把要紧的告诉我。",
      "enabled": true,
      "created_at": "2026-08-14T01:00:00+00:00",
      "next_run_at": "2026-08-17T01:00:00+00:00",
      "origin": { "channel_id": "cli", "conversation_id": "local" },
      "schedule": { "kind": "cron", "expr": "0 9 * * 1-5", "tz": "Asia/Shanghai" },
      "history": [
        { "fired_at": "2026-08-14T01:00:00+00:00", "status": "dispatched", "detail": "" }
      ]
    }
  ]
}
```

**文件损坏时不被覆盖**：解析失败会把它改名成 `jobs.json.corrupt-<时间戳>`，插件进入
降级态——零任务、不调度，任何改动都会被拒绝并指向那份备份。实例本身照常启动
（`BAS-009`：任何配置下都要有本地交互入口），`/cron list` 会明说当前不可用。

## 许可

MIT
