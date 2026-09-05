# 部署

把 Karyvia 作为常驻服务跑起来：Docker、compose、systemd。

第一次跑起来见 [`getting-started.md`](./getting-started.md)，命令参数见
[`cli.md`](./cli.md)，配置字段见 [`configuration.md`](./configuration.md)。

## 常驻的形态只有一种：`karyvia serve`

`karyvia run` 把进程交给 CLI 入口，那条路要读 stdin，在 `nohup` / systemd / 容器里没有意义。
常驻一律用 `karyvia serve`：它装配实例、启动**全部已启用的 Channel 能力**、等信号、干净地停。

HTTP 接口、飞书、cron 调度器都是 Channel 能力，因此都用这同一条命令——
不为某个插件写第二条。

**先决条件是两步而不是一步**：插件要 (1) 装进环境（entry point 没有第二条发现路径），
(2) 写进 `plugins.enabled`。少任何一步 `karyvia serve` 都会以退出码 `1` 说「没有可服务的
Channel」。

## Docker

镜像在 `deploy/Dockerfile`。它默认跑 `karyvia serve`。

```bash
# 构建。KARYVIA_PLUGINS 是逗号分隔的官方插件后缀，对应
# plugins/karyvia-plugin-<名字>/；只装你真的要服务的那几个。
docker build -f deploy/Dockerfile \
  --build-arg KARYVIA_PLUGINS=openai-api,web \
  -t karyvia .

# 跑。实例目录挂进来，凭据走环境变量。
docker run --rm -it \
  -v ~/.karyvia:/home/nanobot/.karyvia \
  -e OPENAI_API_KEY \
  -p 127.0.0.1:8760:8760 \
  karyvia serve --host 0.0.0.0
```

几件必须知道的事：

- **镜像里装了插件 ≠ 启用**。第一次跑之前先在宿主上 `karyvia init` 并把
  `plugins.enabled` 写好（那份 `config.json` 是挂进容器的同一个文件），或者
  `docker run ... karyvia plugins enable openai-api`。
- **容器里必须绑 `0.0.0.0`**，否则端口映射打不到——而绑非回环地址时
  `openai-api` 插件**要求配 `api_key`**，没配就以 `CONFIG_INVALID` 拒绝启动
  （见下面「监听端口」一节）。
- **宿主侧只映射到 `127.0.0.1`**，除非你确实要把它暴露到网络上。
- 入口脚本 `deploy/entrypoint.sh` 在以 root 启动时会 `chown` 数据目录并用 `setpriv`
  降到非 root 用户 `nanobot`（UID 1000）；**降权失败就拒绝运行**，不会以 root 跑下去。
  数据目录属主不对时它会打印三种修法（`chown` / `--user` / `--userns=keep-id`）并退出。

## docker compose

`deploy/docker-compose.yml` 有两个服务：

| 服务 | 干什么 |
| --- | --- |
| `karyvia-serve` | 常驻 `karyvia serve --host 0.0.0.0`，`restart: unless-stopped`，端口只映射到 `127.0.0.1:8760` |
| `karyvia-cli` | 交互式 `karyvia run`，在 `cli` profile 里，默认不起 |

```bash
cd deploy
KARYVIA_PLUGINS=openai-api,web docker compose up -d karyvia-serve

# 需要在同一份数据上开个交互会话时
docker compose --profile cli run --rm karyvia-cli
```

两个服务共用同一个 `~/.karyvia` 卷。**不要让它们同时对同一个实例目录跑**——
实例锁会让第二个起不来，那是刻意的（同一实例目录同时只有一个写者）。要并行就用不同实例：
`--instance serve` / `--instance cli`。

资源限制在 compose 里写着（1 CPU / 1G 内存上限）。需要 bubblewrap 沙箱额外权限时叠加
`docker-compose.bwrap.yml`：

```bash
docker compose -f docker-compose.yml -f docker-compose.bwrap.yml up -d
```

## systemd

不用容器时，用一个 systemd unit 跑虚拟环境里的 `karyvia`：

```ini
# /etc/systemd/system/karyvia.service
[Unit]
Description=Karyvia agent (karyvia serve)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=karyvia
Group=karyvia
WorkingDirectory=/opt/karyvia
Environment=HOME=/var/lib/karyvia
# 凭据从一个 root-only 的文件读，不写进 unit 本身
EnvironmentFile=/etc/karyvia/secrets.env
ExecStart=/opt/karyvia/.venv/bin/karyvia serve
Restart=on-failure
RestartSec=5

# 收紧一点。工作区与实例目录都在 StateDirectory 下。
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
StateDirectory=karyvia
ReadWritePaths=/var/lib/karyvia

[Install]
WantedBy=multi-user.target
```

`/etc/karyvia/secrets.env`（`chmod 600`，属主 root）：

```text
OPENAI_API_KEY=sk-...
```

配置里写的仍然只有 `${OPENAI_API_KEY}` 这个**引用**——凭据本身永远不进 `config.json`。

```bash
sudo -u karyvia HOME=/var/lib/karyvia /opt/karyvia/.venv/bin/karyvia init
sudo systemctl enable --now karyvia
journalctl -u karyvia -f
```

**`SIGTERM` 就是干净停止**：`karyvia serve` 收到中断后先请求取消在跑的 turn（已产生的内容
因此落库）、再走完 `stop()`。默认的 `TimeoutStopSec` 足够——收尾只有取消任务与关文件。

## 日志与可观测性

事件以 JSONL 落在 `<实例目录>/logs/events-<日期>.jsonl`（`logging.file_enabled`，
默认开）。载荷在事件构造**之前**就已脱敏，敏感键名整值打码、已知令牌形状按值打码。

值得盯的几条：`turn.failed`、`channel.delivery_failed`（答案没送出去，与「答案没算出来」
是两件事：前者重发、后者重跑）、`plugin.failed`、`instance.input_dropped`（被 Channel 泵
的背压拒掉）。

## 监听端口这件事，如实说

安装并启用插件就是信任它在当前进程执行代码，Kernel 不监控或审批其网络行为。监听型插件
的启用闸门是 `plugins.enabled` / `plugins.disable` 及插件自身配置。

具体到 HTTP 接口插件，它自己做了两件事来兜底：

- **默认只绑回环**（`127.0.0.1:8760`）。
- **绑非回环地址时必须配 `api_key`**，没配就以 `CONFIG_INVALID` 拒绝启动，
  而不是开一个无鉴权的端口。

```json
{
  "plugins": {
    "enabled": ["openai-api"],
    "openai-api": {
      "config":  { "host": "0.0.0.0", "port": 8760 },
      "secrets": { "api_key": "${KARYVIA_API_KEY}" }
    }
  }
}
```

同进程插件是完全可信代码，可以直接 `import os`。要运行不可信代码，应使用独立插件宿主、
容器、用户、seccomp 等外部隔离；上面 Docker 与 systemd 两节写的就是部署侧方案。

## 升级

1. 停掉服务（`docker compose down` / `systemctl stop`）。
2. 更新 Kernel 代码并重装；插件通过 `karyvia plugins update <id>` 全局更新。
3. 起来。

**配置不会被自动改写**：加载路径只读 `config.json`，新增字段一律有默认值，
所以旧配置在新版本上仍然合法。`karyvia init` 会把 `config.schema.json` 刷新成新的字段表
（`config.json` 一个字节都不动），编辑器补全因此跟得上。

**插件状态目录没有迁移机制**：`state_version` 对不上时那个插件**拒绝加载**（升与降都拒），
而不是拿你的数据赌一把。真遇上了，看那个插件的 README。
