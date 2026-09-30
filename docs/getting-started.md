# 快速上手

从零到第一次对话，再到装上第一个插件。

这篇讲**怎么把 Karyvia 跑起来**。字段含义与优先级见
[`configuration.md`](./configuration.md)，命令的完整参数见 [`cli.md`](./cli.md)，
容器与常驻部署见 [`deployment.md`](./deployment.md)。

## 1. 安装

需要 Python 3.11 或更新。Karyvia 尚未发布到 PyPI，请从本地检出安装：

```bash
git clone <本仓库地址> Karyvia
cd Karyvia

python -m venv .venv
.venv/bin/python -m pip install -e .          # Windows：.venv\Scripts\python.exe
```

装完之后 `karyvia` 就在虚拟环境的 `bin/`（Windows 是 `Scripts\`）里：

```bash
karyvia --version
karyvia --help
```

宿主只有四个第三方依赖（pydantic / httpx / jsonschema / packaging）。**能力所需的包由
那个能力自己的发行包声明**，不回到宿主——所以上面这一条命令装完就能用，官方插件按需再装
（见第 5 节）。

## 2. 生成配置

```bash
karyvia init
```

它在实例目录里建两个文件，**已经存在 `config.json` 一个字节都不会动**：

```text
~/.karyvia/instances/default/
├── config.json          # 你的配置，只有 karyvia init 建它、只有 karyvia plugins enable 改它
└── config.schema.json   # 派生的 JSON Schema，供编辑器补全，运行期忽略
```

生成的 `config.json` 只有你真的要改的几个键：

```json
{
  "$schema": "./config.schema.json",
  "model": {
    "provider": "openai",
    "name": "gpt-4o-mini"
  },
  "plugins": {
    "model-openai": {
      "secrets": {
        "api_key": "${OPENAI_API_KEY}"
      }
    }
  }
}
```

其余四十多个字段都有默认值，不写进模板是刻意的：全倒进去会让它们变成你不敢动的噪声，
而且每一个都会被 `karyvia config show --origins` 记成「来自 config.json」，
「我改过什么」这个问题就永远答不上来了。

## 3. 给上模型凭据

配置文件里**只有变量名，没有凭据本身**。`${OPENAI_API_KEY}` 是一个引用，值在环境变量里：

```bash
export OPENAI_API_KEY=sk-...        # Windows：set OPENAI_API_KEY=sk-...
```

`karyvia init` 的输出会告诉你还差哪个变量。凭据引用的完整语义（没有 `${VAR:-默认值}` 回退、
没有转义、空变量按缺失处理）见 [`configuration.md` 的 `${VAR}` 一节](./configuration.md#5-var-凭据引用)。

**用本地模型服务不需要凭据。** Ollama / vLLM / LM Studio 都是 OpenAI 兼容接口，
把内建 provider 指过去、把鉴权关掉即可：

```json
{
  "model": { "provider": "openai", "name": "qwen2.5:7b" },
  "plugins": {
    "model-openai": {
      "config": { "base_url": "http://127.0.0.1:11434/v1", "auth": "none" }
    }
  }
}
```

## 4. 第一次对话

```bash
karyvia run
```

进入交互式会话：每行输入是一轮对话，`Ctrl-C` 中断当前这一轮并继续，再按一次退出；
输入 `/exit` 或 `/quit` 也可以退出。

跑一条就退出用 `-p`（退出码反映这一轮的终态：`0` 正常、`130` 被中断、`1` 失败）：

```bash
karyvia run -p "用一句话介绍你自己"
```

试试内建命令——它们和插件提供的命令走完全同一条分流路径：

```text
> /help
> /capabilities
> /config
```

零配置下已经可用的内建能力还包括 Skill 目录：会话存储、上下文组装、OpenAI 兼容模型、
文件工具（`fs.read` / `write` / `edit` / `list` / `grep`）、shell 工具（`shell.exec`）、
命令集（`/help` `/config` `/session` `/plugins` `/capabilities` `/cancel`）。

用户级 Skill 直接放在 `~/.karyvia/skills/<name>/SKILL.md`。Agent 会先看到所有 Skill 的
`name`、`description` 与 `@skills/<name>/SKILL.md`，任务匹配时再用 `fs.read` 读取完整指令，
并可用 `shell.exec`、`cwd: "@skills/<name>"` 原地运行 `scripts/`；不会把 Skill 包复制进
workspace。workspace 级 Skill 默认不信任，只有显式配置
`plugins.skills-basic.config.workspace_enabled=true` 后才读取
`<workspace>/.karyvia/skills/<name>/SKILL.md`。Skill 挂载对文件写工具是只读的。

## 5. 装一个官方插件

官方插件是**独立发行包**，由 Karyvia 的全局插件管理器安装：

```bash
karyvia plugins install plugins/karyvia-plugin-web
```

全局安装**不等于实例启用**（安装 ≠ 启用）。没有写进 `plugins.enabled` 的候选
连 manifest 都不会被读——这既是安全边界，也是启动开销的边界：

```bash
karyvia plugins list          # 看看发现了哪些、状态是什么
karyvia plugins enable web    # 写进 config.json 的 plugins.enabled（下次启动生效）
```

然后确认它真的生效了：

```bash
karyvia capabilities          # 生效 / 被覆盖 / 已禁用 / 冲突，四段都印
```

官方插件可以由 Karyvia 自动解析依赖；只有在明确由当前 Python 环境自行提供依赖时才使用
`--no-deps`。从已发布的插件包安装飞书时，应使用普通安装，让安装器一并安装 `lark-oapi`：

```bash
karyvia plugins install karyvia-plugin-feishu
```

如果是在本仓库中安装本地源码，改用本地路径即可。安装器会使用当前虚拟环境中的宿主
`karyvia`，并自动安装插件自己的运行依赖：

```bash
karyvia plugins install ./plugins/karyvia-plugin-feishu
```

其余暂不自动安装平台 SDK 的插件可以使用：

```bash
karyvia plugins install --no-deps plugins/karyvia-plugin-openai-api
karyvia plugins install --no-deps plugins/karyvia-plugin-anthropic
karyvia plugins install --no-deps plugins/karyvia-plugin-web
karyvia plugins install --no-deps plugins/karyvia-plugin-mcp
karyvia plugins install --no-deps plugins/karyvia-plugin-memory
karyvia plugins install --no-deps plugins/karyvia-plugin-cron
```

每个插件自己带一份 README（配置表 + 已知边界 + 刻意不做的事），清单见
[`plugins/README.md`](../plugins/README.md)。

## 6. 插件信任边界

插件与 Kernel 在同一进程运行，安装并启用即表示完全信任其 Python 代码。项目不维护插件
权限清单或授权账本；第三方插件必须像普通 Python 依赖一样审查。需要运行不可信插件时，
请使用独立进程、容器或操作系统隔离。

## 7. 常驻跑一个 Channel

`karyvia run` 把进程交给 CLI 入口，在 `nohup` / systemd 下没有意义。要常驻的是 `karyvia serve`：

```bash
karyvia plugins install plugins/karyvia-plugin-openai-api
karyvia plugins enable openai-api
karyvia serve                      # 默认监听 127.0.0.1:8760
```

它启动全部已启用的 Channel 能力并等信号。飞书和 cron 调度器用的是同一条命令
——不为某个插件写第二条。容器与 systemd 见 [`deployment.md`](./deployment.md)。

## 下一步

| 想干的事 | 去哪 |
| --- | --- |
| 查某个配置字段是什么意思 | [`configuration.md`](./configuration.md) |
| 查某条命令的参数与退出码 | [`cli.md`](./cli.md) |
| 部署成常驻服务 | [`deployment.md`](./deployment.md) |
| 写一个自己的插件 | [`plugin-development.md`](./plugin-development.md) |
| 理解插件信任边界 | [`plugin-development.md`](./plugin-development.md#6-资源服务与信任边界) |
| 读或迁移会话存储 | [`session-storage.md`](./session-storage.md) |
