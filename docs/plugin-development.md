# 插件开发入门

本文写给要为 NucleaMind 写插件的人。读完你会知道一个插件由哪几样东西组成、它能做什么、
不能做什么，以及出错时去哪里看。

仓库里有两个可以直接对照的最小示例：

| 示例 | 演示 |
| --- | --- |
| [`examples/plugins/nucleamind-plugin-echo-tool`](../examples/plugins/nucleamind-plugin-echo-tool) | 新增一项能力（工具） |
| [`examples/plugins/nucleamind-plugin-session-memory`](../examples/plugins/nucleamind-plugin-session-memory) | 覆盖一项内建能力（会话存储） |

本文里的代码块由 `tests/e2e/test_plugin_docs.py` 直接执行，因此它们不会与实现脱节。

## 1. 一个插件由四样东西组成

```text
nucleamind-plugin-<id>/
├── pyproject.toml                      # entry point：让宿主发现得到它
├── src/nucleamind_plugin_<id>/
│   └── __init__.py                     # MANIFEST（声明） + setup（注册）
└── tests/                              # 继承 sdk.testing 的契约测试基类
```

插件是**独立发行包**，不放在宿主包里面。这条边界由打包机制强制：包内的「插件」可以随手
import 兄弟模块，依赖规则就成了空话。

## 2. Manifest：声明你要做什么

`MANIFEST` 是一个模块顶层的常量。**导入这个模块必须无副作用且廉价**——宿主在发现阶段只
import 它取这一个对象，此时不该发生任何 IO。

```python
from nucleamind.contracts import CapabilityKind
from nucleamind.sdk import CapabilityDecl, PluginManifest

MANIFEST = PluginManifest(
    # 小写字母、数字与中划线。它同时是包名 nucleamind-plugin-<id> 的后半段、
    # 状态目录名，以及别人覆盖你时写的 "plugin:<id>:<name>"。
    id="my-plugin",
    version="0.1.0",
    # 你支持的 SDK 区间。宿主落在区间外时拒绝加载并报 PLUGIN_SDK_INCOMPATIBLE，
    # 不带病运行。
    sdk_range=">=4.0.0,<5.0.0",
    setup="nucleamind_plugin_my_plugin:setup",
    # 有约束力的全集：setup 里注册的每一项都必须在这里声明，反之亦然。
    capabilities=(CapabilityDecl(kind=CapabilityKind.TOOL, name="my.tool"),),
    # 用户能在 plugins.my-plugin.config 里写什么。宿主在加载前按它校验。
    config_schema={
        "type": "object",
        "properties": {"endpoint": {"type": "string"}},
        "additionalProperties": False,
    },
)
```

几条容易踩的：

- **不要写 `priority`**。它的默认值是 100，而内建的基准是 0；写了就会被原样采纳，
  「内建排在插件前」会静默失效。
- **`capabilities` 是有约束力的**。声明了却没注册、注册了却没声明，都是
  `PLUGIN_LOAD_FAILED`。这不是形式主义——`overrides` 只能从声明来，`nm capabilities`
  与启动诊断都建立在「声明即全集」上。
- 外部插件加载失败会进入诊断并跳过，不会由插件自己决定中断实例。宿主自带的内建基线
  若装配失败仍属于启动错误；这个边界由 Runtime 按提供方身份决定，不是 manifest 字段。

## 3. setup：注册

```python
from nucleamind.contracts import RiskLevel, ToolSpec
from nucleamind.sdk import NucleaAPI


def setup(api: NucleaAPI) -> None:
    """在同步返回前完成全部注册。"""
    api.register_tool(
        ToolSpec(
            name="my.tool",
            description="模型只能靠这句话决定要不要调用它。",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            read_only=True,
            risk=RiskLevel.SAFE,
        ),
        MyTool(api.ctx.config.get("endpoint")),
    )
```

`NucleaAPI` 恰好有 11 个注册方法，与 11 类能力一一对应：`register_tool` /
`register_command` / `register_context_provider` / `register_model_provider` /
`register_channel` / `register_memory_provider` / `register_session_store` /
`register_context_compactor` / `register_turn_compactor` / `register_cli_entry` / `on`（Hook）。

`register_context_compactor(name, compactor)` 注册的是持久化上下文压缩策略。安装或注册不会
自动生效，用户还必须在 `context.compactor` 显式选择同名能力。`ContextCompactor.compact()`
只返回摘要正文与 `through` 水位；何时触发、结果校验、Session 写入、重载和故障回退都由
Kernel 负责。

`register_turn_compactor(name, compactor)` 注册的是模型—工具迭代期间的临时压缩策略。
Runtime 总是选中一个 `TURN_COMPACTOR`，默认为内建 `basic`；用户可通过
`context.turn_compactor` 选择第三方实现。`TurnContextCompactor.compact()` 收到不可拆分的
`TurnContextUnit` 序列，返回要替换的连续前缀长度与非空摘要。它不得改写 Session、
Transcript 或工具副作；非法结果和运行失败都会终止当前 Turn，不会静默改用内建策略。

如果策略需要模型摘要，使用 `compact()` 当次收到的 `CompactionModel`，不要自行查找
Provider。这个窄门面绑定当前实例已选模型、Turn correlation、取消和剩余时间，
只允许无工具、非流式的 `complete(messages, cancel, max_output_tokens=...)`，且不会再进入
Turn 压缩层。内建 `basic` 当前不调用它，第三方和后续内建策略可直接使用。

**注册是事务性的**：先进暂存批次，`setup` 正常返回才一次性并入能力表；中途抛异常则整批
丢弃，不会留下半注册状态。因此不要在 `setup` 里派生一个后台任务去「稍后注册」。

### 长生命周期资源

`setup()` 只登记，不应在这里建立长连接或让后台任务抢跑。使用 `PluginContext` 的三个入口：

```python
from nucleamind.sdk import NucleaAPI


class Service:
    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def run(self) -> None: ...


def setup(api: NucleaAPI) -> None:
    service = Service()

    async def connect() -> None:
        await service.connect()

    async def close() -> None:
        await service.close()

    api.ctx.on_start(connect)
    api.ctx.add_cleanup(close)
    api.ctx.spawn_task(service.run(), name="service-loop")
```

- `on_start()` 按插件依赖顺序执行，此时 Registry 已冻结，`ctx.instance` 与 `ctx.turns` 已可用。
- `spawn_task()` 在 `setup()` 中只登记，完成 `on_start()` 后才真正启动；插件激活后调用则立即
  启动。
- `add_cleanup()` 释放连接池、数据库连接等非 Task 资源，同一插件内按登记逆序执行。
- 停止时先取消后台任务，再执行清理；一个清理失败不会跳过其余清理。

不要用 `instance_shutdown` Observer 释放关键资源：Observer 的失败按设计被隔离，而且它不是
资源所有权接口。

## 4. entry point：让宿主发现得到

```toml
[project.entry-points."nucleamind.plugins"]
my-plugin = "nucleamind_plugin_my_plugin:MANIFEST"
```

**name 必须等于 manifest 的 `id`**，对不上即失败。理由是「发现与启用分离」：宿主要在
**读 manifest 之前**就知道候选叫什么，才能把没启用的候选直接筛掉——那是「未启用的插件
不产生任何导入开销」的实现方式，不是一条要人遵守的纪律。

Runtime 只读取由 `nm` 写入全局安装目录的 entry point 记录，不扫描整个 Python 环境，也不从
实例配置读取代码路径。开发中的本地包同样交给 `nm plugins install <本地路径>`；修改后用
`nm plugins update <id>` 重新构建。实例的 `plugins/` 目录只保存状态，不保存代码。默认安装
会记录解析出的全部 Python 发行包版本，并拒绝与其他已安装插件形成同名包多版本；如果使用
`--no-deps`，则明确表示依赖由运行 `nm` 的 Python 环境统一提供。

manifest 的 `dependencies` 表示其他插件的逻辑依赖。依赖必须先全局安装；被其他插件依赖的
插件不能直接卸载。外部插件也不能使用内建插件 id，替换内建能力应通过能力声明中的
`overrides` 表达。

## 5. 安装 ≠ 启用

```bash
nm plugins install nucleamind-plugin-my-plugin  # 全局装上，不生效
nm plugins enable my-plugin                  # 写进 plugins.enabled
nm run                                       # 下次启动生效（首版不热更新）
```

配置里长这样：

```json
{
  "plugins": {
    "enabled": ["my-plugin"],
    "my-plugin": {
      "config": { "endpoint": "https://example.com" },
      "secrets": { "api_key": "${MY_PLUGIN_TOKEN}" }
    }
  }
}
```

- `config` 原样交给 `ctx.config`。**你只看得见自己那一块**，没有读别人配置的 API。
- `secrets` 的值只能是 `${VAR}` 引用，明文由 `ctx.secret("api_key")` 在调用时从环境变量
  取。配置树里自始至终只有那个字面量，因此 `/config` 的脱敏是结构性成立的。

## 6. 资源服务与信任边界

插件是同进程运行的可信 Python 代码：安装并启用即授予完整信任，Kernel 不声明、审批或
拦截插件权限。`ctx.fs`、`ctx.net`、`ctx.shell` 与 `ctx.secret(name)` 是稳定的宿主服务，
用于统一工作区路径、SSRF 防护、进程超时和密钥包装，并不是安全沙箱。插件也可以直接使用
Python/OS API；需要隔离不可信代码时，应在进程外使用容器或操作系统策略。

## 7. 覆盖一项已有能力

想替换内建实现（或另一个插件的实现）时，在声明里写 `overrides`：

```python
from nucleamind.contracts import CapabilityKind
from nucleamind.sdk import CapabilityDecl

DECL = CapabilityDecl(
    kind=CapabilityKind.SESSION_STORE,
    name="memory",
    # 覆盖内建写 "builtin:<name>"，覆盖插件写 "plugin:<id>:<name>"。
    # 串里不带 kind——kind 取自声明覆盖的这一方。
    overrides="builtin:jsonl",
)
```

三条规矩：

1. **覆盖永不由加载顺序决定**。没声明 `overrides` 而撞了名字，是
   `PLUGIN_REGISTRATION_CONFLICT`，且**冲突各方都不生效**——选任何一边都是替用户做决定。
2. **覆盖不静默**。`nm capabilities` 的「被覆盖」段会印出被顶掉的那一项与顶掉它的那一项，
   两边都带提供方标识。
3. **覆盖目标不存在不会降级成新增注册**，而是 `CAPABILITY_OVERRIDE_TARGET_MISSING`。

### 被禁用之后

插件被写进 `plugins.disable` 后，Runtime 不读取其 manifest、不执行 `setup()`，也不注册
它的任何能力：

```json
{
  "plugins": {
    "enabled": ["my-plugin"],
    "disable": ["my-plugin"]
  }
}
```

`disable` 压过 `enabled`。如果该插件原本覆盖了内建能力，覆盖关系随插件一起退出本次启动，
未被单独禁用的内建实现会正常生效。这里没有额外的恢复策略键；禁用插件本身就是完整意图。

## 7.5 能力名要连上外部服务才知道：命名空间声明

manifest 是**静态**的，而 `CapabilityHost.finish()` 要求声明的 `(kind, name)` 与实际注册的
**逐条相等**。桥接类插件（MCP、远端工具网关）撞得上这条：远端工具名要连上 server、
`list_tools` 之后才可知。

对这种情况声明一个**命名空间**：

```python
from nucleamind.contracts import CapabilityKind
from nucleamind.sdk import CapabilityDecl

DECL = CapabilityDecl(
    kind=CapabilityKind.TOOL,
    # `namespace=True` 时 name 是**前缀**：本条声明放行注册任意多条 `mcp.<后缀>`。
    name="mcp",
    namespace=True,
)
```

`setup(api)` 里就可以注册任意多条 `mcp.` 开头的工具，名字不必事先写进 manifest。
**`setup` 可以是 `async` 的**，因此「连上去、拿到工具表、逐条注册」全在它里面完成——
registry 在解析之后只读，没有第二个注册时机。

五条规矩：

1. **只放行 `<前缀>.<后缀>`**。前缀本身（`mcp`）不在内，`mcpx.read` 也不在内——
   前缀比较落在分隔符边界上。要注册前缀本身就再写一条普通声明。
2. **精确声明优先**。同时匹配时用精确的那条；两条命名空间同时匹配则是
   `PLUGIN_LOAD_FAILED`——静默挑一个等于让加载顺序说了算。
3. **零注册是合法的**。远端服务连不上时你注册零条工具，那是如实反映外部状态，
   不算「声明了却没注册」。
4. **不能与 `overrides` 并存**。一条声明能注册出任意多个名字，哪一个是覆盖者无从判定。
5. **只有可并存且按名字唯一的能力**（`tool` / `command` / `model` / `channel` / `memory`）
   能声明命名空间。SINGLETON 的槽位只有一个，给它开前缀等于让「唯一」失去判定对象。

冲突语义一个字没变：registry 仍按精确 `(kind, name)` 判，`nm capabilities` 印的是**实际
注册的**名字。命名空间只影响 manifest 与动态注册项的对应方式，不改变资源服务或信任边界。

## 8. 测试：继承契约测试基类

`nucleamind.sdk.testing` 发布了 8 个契约测试基类与一批 Fake。内建实现与你的插件**继承
同一个基类**——这就是「可替换」的可执行形态。

```python
from nucleamind.contracts import SessionStore
from nucleamind.sdk.testing import InMemorySessionStore, SessionStoreContract


class TestMyStore(SessionStoreContract):
    def make_store(self) -> SessionStore:
        return InMemorySessionStore()
```

基类是 `ModelProviderContract` / `SessionStoreContract` / `ToolContract` /
`ContextProviderContract` / `ContextCompactorContract` / `TurnContextCompactorContract` / `MemoryProviderContract` /
`ChannelContract`。它们**不 import pytest**，所以你用什么 runner 都行；子类名必须以
`Test` 开头，否则 pytest 不收集。

## 9. 出错时看哪里

| 现象 | 命令 | 说明 |
| --- | --- | --- |
| 插件没被加载 | `nm plugins list` | 列出候选、跳过原因与两个阶段的失败 |
| 不知道谁提供了某项能力 | `nm capabilities` | 生效 / 被覆盖 / 已禁用 / 冲突四段，各带提供方 |

三类失败有各自稳定的错误码，别混着读：

| 错误码 | 含义 | 去改哪里 |
| --- | --- | --- |
| `CONFIG_INVALID` | 用户写的配置不符合你的 `config_schema` | `config.json` |
| `PLUGIN_SDK_INCOMPATIBLE` | 你声明的 `sdk_range` 与宿主不兼容 | 插件的 manifest |
| `PLUGIN_LOAD_FAILED` | `setup` 导不进 / 跑出异常 / 声明与注册对不上 | 插件的实现 |

## 10. 依赖规则

插件**只能** import `nucleamind.contracts` 与 `nucleamind.sdk`。够到 `nucleamind.kernel.*`
的插件在本仓库会被架构守卫拦下；在你自己的仓库里没人拦，但那些是私有模块，不承诺任何
兼容性，随时会变。

契约类型直接从 `nucleamind.contracts` 导入，不从 `nucleamind.sdk` 转发——`SecretStr`、
`SessionKey`、`ToolSpec` 这些都在前者。
