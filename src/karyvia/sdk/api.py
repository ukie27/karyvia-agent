"""Host API：插件与 Kernel 之间的注册面与受限运行时。

职责：声明 `KaryviaAPI`（恰好 10 个能力注册方法 + `ctx`）、`PluginContext` 及其生命周期和
四个资源访问器 Protocol（`fs` / `net` / `shell` / `secret`），以及配套的 `HttpResponse`、
`ShellResult`。
不负责：实现注册与冲突判定、构造 `PluginContext` 或决定插件加载结果。
这些分别属于 Kernel 与 Runtime；本模块只有公开签名和纯数据类型。

`SecretStr`、`InstanceView` 与 `TurnControl` 位于 `contracts`，因为 Kernel 也需要创建或实现
它们，而 Kernel 不能依赖 SDK。插件应直接从 `karyvia.contracts` 导入共享契约；SDK 不做
重复转发。

三件必须在这一层说清楚的事：

- **10 个注册方法与 `CapabilityKind` 的 10 个取值一一对应**，不多不少。多出一个方法就等于
  多出一类没有冲突语义的能力（`CAPABILITY_ARITY` 会 KeyError），少一个就等于某类能力
  只能靠 Kernel 内部特权注册——那正是 「内建能力不享受特权」要堵的路。
- **安装并启用插件即表示完全信任它**。资源访问器提供一致的路径、超时、SSRF 防护和密钥
  封装，但不是权限或进程隔离；插件也可以直接使用 Python/OS API。需要隔离时应使用独立
  进程、容器或部署边界，而不是扩张同进程 Host API。
- **后台任务只能经 `ctx.spawn_task()` 创建**。Host API 不暴露裸 `asyncio.create_task`，
  否则「这个任务是谁的」在禁用插件时就无从判定， 的痕迹清理也就无从谈起。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from logging import Logger
from pathlib import Path
from typing import Protocol, runtime_checkable

from karyvia.contracts import (
    Channel,
    CliEntry,
    CommandHandler,
    CommandSpec,
    ContextProvider,
    EventName,
    HookHandler,
    HookName,
    InstanceView,
    JsonValue,
    MemoryProvider,
    ModelProvider,
    RuntimeEvent,
    SecretStr,
    SessionStore,
    ToolHandler,
    ToolSpec,
    TurnContextCompactor,
    TurnControl,
)

__all__ = [
    "EventHandler",
    "EventSubscriber",
    "FileAccess",
    "HttpAccess",
    "HttpResponse",
    "KaryviaAPI",
    "PluginContext",
    "ShellAccess",
    "ShellResult",
]

#: 事件订阅者可以同步返回，也可以返回 awaitable。桥接层负责判断是否需要 await；调用方
#: 不应自行创建无人管理的 Task。两种 handler 都是观察者，返回值不参与事件处理。
EventHandler = Callable[[RuntimeEvent], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """一次 HTTP 往返的结果（经 SSRF 守卫后）。

    `body` 保持 `bytes`：解码是调用方的事，替它猜编码只会在非 UTF-8 站点上悄悄出错。
    """

    status: int
    headers: Mapping[str, str]
    body: bytes
    #: `body` 是否因为 `request(max_bytes=...)` 被截断。没有这一位，「正好等于上界」
    #: 与「被截断了」长得一模一样，
    #: 而调用方对这两件事的处理完全不同（后者要告诉模型内容不完整）。
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class ShellResult:
    """一次子进程执行的结果。

    `timed_out=True` 时 `exit_code` 无意义——进程是被杀掉的，不是自己退出的，
    副作用是否已经发生同样不可知（对应 `SideEffect.UNKNOWN` 的判断依据）。
    """

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


@runtime_checkable
class FileAccess(Protocol):
    """受工作区边界约束的文件访问服务。

    所有路径都相对于插件被授予的根，解析后必须落在允许根内（`realpath` 之后重新校验，
    覆盖符号链接、`..`、Windows 大小写与重解析点）。绝对路径一律拒绝——接受绝对路径就
    等于把「根在哪」的决定权交给了调用方。
    """

    async def read_text(self, path: str) -> str:
        """读取文本文件（UTF-8）。

        **异常约定**：越界抛 `PERMISSION_PATH_OUTSIDE_WORKSPACE`；不存在或读失败抛
        `PERSISTENCE_READ_FAILED`。
        """
        ...

    async def write_text(self, path: str, content: str) -> None:
        """原子写入文本文件（临时文件 + 替换）。

        **异常约定**：同 `read_text()` 的越界规则；写失败抛
        `PERSISTENCE_WRITE_FAILED`。不得留下半份文件。
        """
        ...

    async def read_bytes(self, path: str) -> bytes:
        """读取二进制文件。

        **异常约定**与 `read_text()` 逐条相同。差别只有一个：`read_text()` 用
        `errors="replace"` 解码，因此它对一个 PNG 也「成功」，只是交回一串替换字符——
        要原字节就必须走这条。

        二进制必须有独立方法：文本读取会以 replacement character 处理解码错误，既不能
        保真，也会迫使图片、音频等插件绕过受控文件门面。
        """
        ...

    async def write_bytes(self, path: str, data: bytes) -> None:
        """原子写入二进制文件。**异常约定**同 `write_text()`。"""
        ...

    async def list_dir(self, path: str) -> tuple[str, ...]:
        """列出目录下的条目名（不递归）。

        **异常约定**：同 `read_text()`。目录不存在抛 `PERSISTENCE_READ_FAILED`，
        空目录返回空元组——「空」和「没有」是两回事。
        """
        ...


@runtime_checkable
class HttpAccess(Protocol):
    """带 SSRF 守卫的 HTTP 服务。"""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout_ms: int = 30_000,
        max_bytes: int | None = None,
    ) -> HttpResponse:
        """发起一次 HTTP 请求。

        解析出的 IP 与**每一次重定向**的目标都会被重新校验，私有网段与云元数据地址一律
        拒绝。插件仍可直接使用其他网络库；安装并启用插件即表示信任其代码。

        `max_bytes` 给响应体一个**下载上界**：到量即停止读取并断开，
        `HttpResponse.truncated` 标着。不给就整份读完。

        **上界必须在读取过程中执行，而不是读完整个响应后再截断**，否则大响应仍会占满
        内存和网络带宽。

        **刻意没有做完整的流式接口。** 那需要把响应对象的生命周期交给调用方（异步上下文
        管理器），而守卫的重定向重校验正发生在响应头与响应体之间；今天没有任何一个消费者
        需要它——两个模型 provider 消费 SSE 但走的是 raw httpx（端点由运维配置、私有网段
        常见，守卫会按设计拒掉），`openai-api` 产出 SSE 用的是 aiohttp。为一个没有消费者
        的用例设计一个要长期兼容的接口，只能设计错。

        **异常约定**：目标被守卫拒绝抛 `PERMISSION_DENIED` 并在 `detail` 说明原因；
        网络失败抛 `EXTERNAL_SERVICE` 类错误
        并如实标注 `retryable`；超时抛 `TIMEOUT` 类错误。**非 2xx 不是异常**——它是结果，
        由调用方按业务判断。`max_bytes` 非正抛 `INPUT_MALFORMED`。
        """
        ...


@runtime_checkable
class ShellAccess(Protocol):
    """带工作区、环境和超时约束的子进程服务。"""

    async def run(
        self,
        command: Sequence[str],
        *,
        cwd: str | None = None,
        timeout_ms: int = 60_000,
    ) -> ShellResult:
        """执行一条命令。参数是**列表**而不是命令行字符串，因此不存在 shell 注入面。

        默认 cwd 限定在 workspace，默认不继承敏感环境变量。Windows 与 Linux 的命令构造
        分别实现，但对外行为契约一致：同样的参数给出同样的退出码语义、同样的输出截断
        规则、同样的超时行为。

        **异常约定**：cwd 越界抛 `PERMISSION_PATH_OUTSIDE_WORKSPACE`；**非零退出码不是异常**，它在
        `ShellResult.exit_code` 里。超时同样不抛，返回 `timed_out=True` 的结果——
        调用方需要拿到超时前已产生的输出。
        """
        ...


@runtime_checkable
class EventSubscriber(Protocol):
    """事件订阅面。订阅在插件被禁用时由 Kernel 统一取消。"""

    def subscribe(self, event: EventName, handler: EventHandler) -> None:
        """订阅一个事件名。同一 handler 重复订阅同一事件视为一次。

        没有 `unsubscribe`：订阅的生命周期就是插件的生命周期，让插件自行退订只会多出
        「退订了但任务还在跑」这种中间状态。需要临时静音就在 handler 里判断。

        **异常约定**：事件名未登记抛 `INPUT_MALFORMED`（`EventName` 是冻结枚举，
        传进来的一定是它的成员，因此这条只在跨版本反序列化时才可能触发）。
        handler 自身的异常由 Kernel 隔离并记 `PLUGIN_FAILURE`，不影响事件发布。
        """
        ...


@runtime_checkable
class PluginContext(Protocol):
    """插件拿到的宿主运行时；它提供服务和生命周期所有权，不提供安全隔离。"""

    @property
    def plugin_id(self) -> str:
        """本插件的稳定 id，与 manifest 一致。"""
        ...

    @property
    def config(self) -> Mapping[str, JsonValue]:
        """**只有自己那一块**配置。没有读取他人配置的 API，也不打算有。"""
        ...

    @property
    def state_dir(self) -> Path:
        """`<instance_dir>/plugins/<id>/`，已创建。插件的持久化数据只应写在这里，
        卸载时按 `state_version` 与用户选择整体处理（§10.5）。"""
        ...

    @property
    def logger(self) -> Logger:
        """已绑定 `plugin_id` 的 logger，输出自动脱敏（`contracts.errors`当前规则）。"""
        ...

    @property
    def events(self) -> EventSubscriber:
        """事件订阅面。"""
        ...

    def on_start(self, action: Callable[[], Awaitable[None]]) -> None:
        """登记实例激活时执行的异步动作。

        `setup()` 只登记，不应自行连接外部服务。Runtime 在 Registry 冻结、实例门面可用后，
        按插件依赖顺序逐个执行这些动作；失败会记录为插件故障并跳过该插件。

        **异常约定**：插件已经激活或进入停止流程后再登记，抛
        `KERNEL_INVARIANT_VIOLATED`。
        """
        ...

    def add_cleanup(self, action: Callable[[], Awaitable[None]]) -> None:
        """登记停止时执行的异步清理动作，执行顺序与登记顺序相反。

        清理动作可释放连接池、数据库连接或其他非 Task 资源。一个动作失败不会阻断其余动作；
        Runtime 在全部动作执行后统一记录插件停止失败。

        **异常约定**：插件已进入停止流程后再登记，抛 `KERNEL_INVARIANT_VIOLATED`。
        """
        ...

    def spawn_task(self, coro: Awaitable[None], *, name: str) -> None:
        """在本插件的 task group 下登记或创建后台任务。

        这是插件创建后台任务的**唯一**途径。`name` 必填且会出现在诊断里——一个匿名的
        挂起任务和一个没有任务是同一种排查体验。

        `setup()` 期间调用只登记，任务在插件激活后才真正启动；激活后调用则立即创建。

        **异常约定**：插件已进入停止流程时抛 `KERNEL_INVARIANT_VIOLATED`。任务自身的
        异常由 Kernel 捕获并记 `PLUGIN_FAILURE`，不会冒泡到别的插件或 turn。
        **取消语义**：插件被停止或禁用时，其全部任务被 `cancel()`，随后按
        `plugin_stop_timeout_ms` 等待，超时即放弃等待并继续停止流程。
        """
        ...

    @property
    def fs(self) -> FileAccess:
        """限定在 workspace 内的文件访问器。"""
        ...

    @property
    def net(self) -> HttpAccess:
        """带 URL 与 SSRF 检查的出网访问器。"""
        ...

    @property
    def shell(self) -> ShellAccess:
        """带 cwd、环境、超时和输出上界的子进程访问器。"""
        ...

    def secret(self, name: str) -> SecretStr:
        """按名字取得本插件配置的凭据。

        **异常约定**：配置里没有该值时抛 `CONFIG_SECRET_MISSING`。返回值默认渲染为掩码，明文需
        `reveal()`。
        """
        ...

    @property
    def instance(self) -> InstanceView:
        """实例的只读视图：已注册命令、能力解析报告、插件状态、完整配置、会话快照。

        `/help`、`/capabilities`、`/plugins`、`/config`、`/session` 要回答的都是
        「这个实例现在是什么样」，而那些数据在 `kernel/` 里，
        `R4` 禁止 `builtins/` 与 `plugins/` 够到它。没有这条通道，`commands_core` 就只能
        由 `runtime/` 特权注册——「内建能力不享受特权」当场破例，而第三方也就
        永远写不了 `/status` 这类命令。**这类命令本来就该是插件能写的东西。**

        `config_document` 是唯一越过的成员，但明文凭据结构性地不在那份文档里
        （配置树自始至终持有 `${VAR}` 字面量）。
        """
        ...

    @property
    def turns(self) -> TurnControl:
        """在跑 turn 的观测与取消。

        与 `instance` 分开而不是合成一个门面：一个是只读可观测性，一个是**控制动作**；
        两者分开后才能独立授予、替换与测试。
        """
        ...


@runtime_checkable
class KaryviaAPI(Protocol):
    """插件的注册面（§7.5）。**恰好 10 个注册方法 + `ctx`**。

    形态对应 Pi 的 `ExtensionAPI`：`setup(api)` 拿到它，在**同步返回前**完成全部注册。
    注册先进 `RegistrationBatch` 暂存区，`setup` 正常返回才一次性并入 registry；中途抛
    异常则整批丢弃，registry 不留半注册状态。因此「注册」不是立即生效的
    副作用，插件也不该在 `setup` 之后再回头注册——那时批次已经提交，registry 已冻结。

    声明式扩展不需要新方法：Skill、Prompt 片段与斜杠命令的文本模板都通过
    `register_context_provider` / `register_command` 承载，内容以 `ContextFragment` 形式
    提交，因此天然受 `trust`、`priority` 与预算约束。
    """

    @property
    def ctx(self) -> PluginContext:
        """本插件的受限运行时。"""
        ...

    def register_tool(self, spec: ToolSpec, handler: ToolHandler) -> None:
        """注册一个工具（`CapabilityKind.TOOL`，MULTI_UNIQUE）。

        能力名取自 `spec.name`，因此工具的声明与注册不可能对不上。

        **异常约定**：名字冲突且未在 manifest 声明 `overrides` 时抛
        `PLUGIN_REGISTRATION_CONFLICT`；批次已提交后再注册抛 `KERNEL_INVARIANT_VIOLATED`。
        """
        ...

    def register_command(self, spec: CommandSpec, handler: CommandHandler) -> None:
        """注册一个斜杠命令（`CapabilityKind.COMMAND`，MULTI_UNIQUE）。

        `spec.all_names`（命令名 + 别名）整体参与冲突检查。

        **异常约定**：同 `register_tool()`。
        """
        ...

    def register_context_provider(self, name: str, provider: ContextProvider) -> None:
        """注册一个上下文贡献者（`CapabilityKind.CONTEXT`，MULTI）。

        MULTI 意味着同名可以并存、全部生效，按 `(priority, provider)` 排序，
        因此这里的 `name` 是诊断标签而不是唯一键。

        **异常约定**：批次已提交后再注册抛 `KERNEL_INVARIANT_VIOLATED`。
        """
        ...

    def register_turn_compactor(self, name: str, compactor: TurnContextCompactor) -> None:
        """注册统一上下文压缩策略（`TURN_COMPACTOR`，MULTI_UNIQUE）。

        实现只处理当前模型请求的投影，不得读写 Session。Kernel 会在 Turn 收口时持久化
        能够映射到 Session 连续前缀的摘要。Runtime 必须选中一个生效实现。

        **异常约定**：同 `register_tool()`。
        """
        ...

    def register_model_provider(self, name: str, provider: ModelProvider) -> None:
        """注册一个模型供应商（`CapabilityKind.MODEL`，MULTI_UNIQUE）。

        **异常约定**：同 `register_tool()`。
        """
        ...

    def register_channel(self, name: str, channel: Channel) -> None:
        """注册一个外部平台接入（`CapabilityKind.CHANNEL`，MULTI_UNIQUE）。

        Channel 是长生命周期服务：注册只是登记，`start()` 由 Runtime 在激活阶段按拓扑序
        调用；插件不应在 `setup` 里自行建立连接。

        **异常约定**：同 `register_tool()`。
        """
        ...

    def register_memory_provider(self, name: str, provider: MemoryProvider) -> None:
        """注册一个长期记忆实现（`CapabilityKind.MEMORY`，MULTI_UNIQUE）。

        带 `name` 即意味着可以并存多个具名实现： 的降级要求换一个后端不必先
        卸载现有的。

        **异常约定**：同 `register_tool()`。
        """
        ...

    def register_session_store(self, name: str, store: SessionStore) -> None:
        """注册会话存储实现（`CapabilityKind.SESSION_STORE`，SINGLETON）。

        SINGLETON：全实例只有一个生效实现，替换必须在 manifest 显式声明 `overrides`。
        会话历史是用户资产，「装上就换掉」这种语义不该由加载顺序决定。

        **异常约定**：已有实现且未声明覆盖时抛 `PLUGIN_REGISTRATION_CONFLICT`。
        """
        ...

    def register_cli_entry(self, name: str, entry: CliEntry) -> None:
        """注册本地命令行入口（`CapabilityKind.CLI_ENTRY`，SINGLETON）。

        CLI 入口不可禁用：不装任何 Channel 插件也必须存在本地
        交互入口。插件可以覆盖内建实现，但覆盖实现加载失败时 Runtime 强制回落到内建实现
        而不是让实例失去入口。

        **异常约定**：同 `register_session_store()`。
        """
        ...

    def on(self, hook: HookName, handler: HookHandler, *, priority: int = 100) -> None:
        """订阅一个 Hook（`CapabilityKind.HOOK`，MULTI）。

        `hook` 是冻结的 9 个之一；它是 observer 还是 interceptor 由 `HOOK_KINDS` 决定，
        不由注册方选择——同一个 Hook 对不同插件有不同语义，失败隔离规则就无法自洽。

        **异常约定**：批次已提交后再注册抛 `KERNEL_INVARIANT_VIOLATED`。
        handler 自身的异常由 Kernel 隔离并上报，不影响其余 handler 或当前 turn。
        """
        ...
