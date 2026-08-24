"""唯一的 Host `NucleaAPI` 实现：把 10 个注册方法分派进 `RegistrationBatch`（技术方案 §7.5）。

职责：接住插件 `setup(api)` 里的 10 类注册调用，回查声明表取 `overrides` 与 `priority`，
包成对应 kind 的注册载荷，逐条放进批次；并在 `finish()` 时核对声明与实际注册一一对应。
不负责：提交或回滚批次（那是 `builtin_loader.py`，因为 `setup` 返回给的是 loader 而不是
Host）、构造 `PluginContext`、发现插件、判定谁最终生效
（`kernel/registry/resolution.py`）。本模块不做 IO。

**内建与插件共用这一个实现**（`SDK-007`、`BAS-005`）：不存在内建专用注册 API。两者的差别
全部在 `LoadRequest` 的产出方式上（内建来自静态可信清单，插件还要过发现与依赖校验），
差异不延伸到能力注册接口。

**不 import `sdk/`**（规则 `R2`），因此本类是 `NucleaAPI` 的**结构化**实现而非继承——
`HookRouter` 同样结构化满足 `deps.HookDispatcher`。ctx 做成泛型参数
`CapabilityHost[ContextT]`：kernel 对它连一个结构假设都不做，只负责原样转交。把 ctx 标成
`object` 是行不通的——那样 `ctx` 的返回类型与 `NucleaAPI.ctx` 声明的 `PluginContext`
不兼容，「Host 真的满足 `NucleaAPI`」就在任何地方都证明不了。一致性的证明落在
`runtime/wiring.py`（唯一同时看得见两层、又在 basedpyright 检查范围内的地方，
`pyproject.toml` 的 `exclude = ["**/tests"]` 让测试无法承担这个角色）。

**未声明的注册是错误**（`PLUGIN_LOAD_FAILED`）。放行它等于让 manifest 的 `capabilities`
变成一份没有约束力的文档，而 `overrides` 只能从那里来（`EDG-102`：覆盖永不由加载顺序
决定），`nm capabilities` 也建立在「声明即全集」上。反过来，声明了
却没注册同样是错误——那说明 manifest 骗过了阶段 A，用户会看到一项查得到却不存在的能力。
两者共用一个码，靠 `detail` 区分：诊断要回答的是「manifest 和实现对不上」这件事本身。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Generic, TypeVar

from nucleamind.contracts import (
    CapabilityKind,
    Channel,
    CliEntry,
    CommandHandler,
    CommandSpec,
    ContextCompactor,
    ContextProvider,
    ErrorCode,
    HookHandler,
    HookName,
    MemoryProvider,
    ModelProvider,
    NucleaError,
    SessionStore,
    ToolHandler,
    ToolSpec,
)
from nucleamind.kernel.registry import PLUGIN_BASE_PRIORITY, RegistrationBatch
from nucleamind.kernel.routing import RegisteredCommand
from nucleamind.kernel.turn import RegisteredContextProvider, RegisteredHook, RegisteredTool

from .capabilities import (
    RegisteredChannel,
    RegisteredCliEntry,
    RegisteredContextCompactor,
    RegisteredMemoryProvider,
    RegisteredModelProvider,
    RegisteredSessionStore,
)
from .declarations import CapabilityDeclaration

__all__ = ["CapabilityHost"]

_ContextT = TypeVar("_ContextT")


class CapabilityHost(Generic[_ContextT]):
    """`NucleaAPI` 的宿主实现。恰好 10 个注册方法 + `ctx`，与 `CapabilityKind` 一一对应。

    生命周期：由 loader 建好、交给 `setup(api)`、`setup` 返回后 loader 调 `finish()` 再
    `commit()`。Host 自己**从不提交**——`setup` 的返回时刻在 loader 的作用域里，而
    `EDG-103`「中途抛异常整批丢弃」要求提交发生在那之后。
    """

    __slots__ = ("_batch", "_ctx", "_declared", "_hook_counts", "_namespaces", "_used")

    def __init__(
        self,
        batch: RegistrationBatch,
        ctx: _ContextT,
        *,
        declarations: Sequence[CapabilityDeclaration] = (),
    ) -> None:
        self._batch = batch
        self._ctx = ctx
        #: **精确声明表，命名空间不在其中**。分成两张表是为了让规则只有一条：
        #: 一条命名空间声明放行的**恰好是** `<前缀>.<后缀>`，前缀本身不在内。让它同时
        #: 落进精确表，就等于一条声明有两套判据，而其中一套没写在任何地方。
        self._declared = {
            declaration.slot: declaration
            for declaration in declarations
            if not declaration.namespace
        }
        self._namespaces = tuple(
            declaration for declaration in declarations if declaration.namespace
        )
        self._used: set[tuple[CapabilityKind, str]] = set()
        self._hook_counts: dict[HookName, int] = {}

    @property
    def ctx(self) -> _ContextT:
        """本插件的受限运行时。Host 只持有并转交，自己一个成员都不碰。"""
        return self._ctx

    # ---------------------------------------------------------------------- 9 个注册方法

    def register_tool(self, spec: ToolSpec, handler: ToolHandler) -> None:
        """注册一个工具。能力名取自 `spec.name`，声明与注册因此不可能对不上。"""
        self._register(CapabilityKind.TOOL, spec.name, RegisteredTool(spec=spec, handler=handler))

    def register_command(self, spec: CommandSpec, handler: CommandHandler) -> None:
        """注册一个斜杠命令。别名冲突由 `build_command_index()` 在启动期查（`CMD-002`）。"""
        self._register(
            CapabilityKind.COMMAND, spec.name, RegisteredCommand(spec=spec, handler=handler)
        )

    def register_context_provider(self, name: str, provider: ContextProvider) -> None:
        """注册一个上下文贡献者。"""
        self._register(
            CapabilityKind.CONTEXT,
            name,
            RegisteredContextProvider(provider=provider),
        )

    def register_context_compactor(self, name: str, compactor: ContextCompactor) -> None:
        """注册一个上下文压缩策略。"""
        self._register(
            CapabilityKind.COMPACTOR,
            name,
            RegisteredContextCompactor(compactor=compactor),
        )

    def register_model_provider(self, name: str, provider: ModelProvider) -> None:
        """注册一个模型供应商。"""
        self._register(CapabilityKind.MODEL, name, RegisteredModelProvider(provider=provider))

    def register_channel(self, name: str, channel: Channel) -> None:
        """注册一个外部平台接入。注册只是登记，`start()` 由 Runtime 在阶段 D 调用。"""
        self._register(CapabilityKind.CHANNEL, name, RegisteredChannel(channel=channel))

    def register_memory_provider(self, name: str, provider: MemoryProvider) -> None:
        """注册一个长期记忆实现。"""
        self._register(CapabilityKind.MEMORY, name, RegisteredMemoryProvider(provider=provider))

    def register_session_store(self, name: str, store: SessionStore) -> None:
        """注册会话存储实现（SINGLETON，替换必须显式声明 `overrides`）。"""
        self._register(CapabilityKind.SESSION_STORE, name, RegisteredSessionStore(store=store))

    def register_cli_entry(self, name: str, entry: CliEntry) -> None:
        """注册本地命令行入口（SINGLETON，不可禁用，见 `BAS-009`/`EDG-108`）。"""
        self._register(CapabilityKind.CLI_ENTRY, name, RegisteredCliEntry(entry=entry))

    def on(
        self, hook: HookName, handler: HookHandler, *, priority: int = PLUGIN_BASE_PRIORITY
    ) -> None:
        """订阅一个 Hook。

        **能力名由 `hook` 派生**：`on()` 没有 name 形参，而 registry 需要一个。同一提供方
        对同一 Hook 绑第二个 handler 是合法的（HOOK 是 MULTI），但批次内 `(kind, name)`
        必须唯一，因此第二次起用 `<hook>.2`、`<hook>.3`。声明表**始终按基名回查**，
        N 次注册共享同一条声明。

        **`priority` 的判定**：`NucleaAPI.on()` 的签名默认值恰好是 `PLUGIN_BASE_PRIORITY`
        （100），因此「作者写了 100」与「作者什么都没写」在调用侧不可区分。取值等于基准值
        时一律视为未声明，回落到声明表、再回落到 `base_priority_for()`——否则每一个内建
        Hook 都会落在 100，内建排在插件前（§6.1 规则 1）与「内建最后被裁」（§10.2）
        两条同时失效。要显式要 100 的插件本来就会拿到 100（插件基准值就是它）。
        """
        seen = self._hook_counts.get(hook, 0) + 1
        self._hook_counts[hook] = seen
        name = hook.value if seen == 1 else f"{hook.value}.{seen}"
        self._register(
            CapabilityKind.HOOK,
            name,
            RegisteredHook(hook=hook, handler=handler),
            slot_name=hook.value,
            priority=None if priority == PLUGIN_BASE_PRIORITY else priority,
        )

    # ------------------------------------------------------------------------ 分派与收尾

    def _register(
        self,
        kind: CapabilityKind,
        name: str,
        payload: object,
        *,
        slot_name: str | None = None,
        priority: int | None = None,
    ) -> None:
        """唯一的分派点：回查声明 → 取 `overrides`/`priority` → 放进批次。

        `slot_name` 只有 HOOK 用得上（登记名带序号、声明名不带）。`priority` 参数同样只有
        HOOK 传，其余 kind 的优先级只能来自声明。

        **回查是两步：先精确、再命名空间**。顺序不可颠倒——一条精确声明与一条
        命名空间声明可能同时匹配（`mcp.probe` 与前缀 `mcp`），静默挑一个就等于让
        「哪条声明生效」取决于表的遍历顺序。
        """
        lookup = slot_name or name
        declaration = self._declared.get((kind, lookup)) or self._namespace_for(kind, lookup)
        if declaration is None:
            raise NucleaError(
                ErrorCode.PLUGIN_LOAD_FAILED,
                "注册了 manifest 未声明的能力。",
                detail={
                    "provider": str(self._batch.provider),
                    "capability": f"{kind.value}:{lookup}",
                    "declared": sorted(f"{k.value}:{n}" for k, n in self._declared),
                },
            )
        self._used.add(declaration.slot)
        self._batch.add(
            kind,
            name,
            payload,
            priority=priority if priority is not None else declaration.priority,
            overrides=declaration.overrides,
        )

    def _namespace_for(
        self, kind: CapabilityKind, name: str
    ) -> CapabilityDeclaration | None:
        """找放行这次注册的命名空间声明。

        **两条同时匹配是错误而不是择一**：`mcp` 与 `mcp.remote` 两个前缀都能放行
        `mcp.remote.read`，选哪一条会决定它拿到哪个 `priority`。静默择一正是
        `EDG-102`「覆盖永不由加载顺序决定」在这一层的对应物。
        """
        matches = [decl for decl in self._namespaces if decl.covers(kind, name)]
        if not matches:
            return None
        if len(matches) > 1:
            raise NucleaError(
                ErrorCode.PLUGIN_LOAD_FAILED,
                "同一次注册被多条命名空间声明放行，无法判定用哪一条。",
                detail={
                    "provider": str(self._batch.provider),
                    "capability": f"{kind.value}:{name}",
                    "namespaces": sorted(decl.name for decl in matches),
                },
            )
        return matches[0]

    def finish(self) -> None:
        """核对每条声明都真的被注册过。由 loader 在 `setup` 正常返回后、提交之前调用。

        **命名空间声明豁免**：它表达的是「本提供方可以注册这个前缀下的能力」，
        不是「一定会注册」。一个 MCP server 连不上时该插件注册零条工具，那是它如实反映
        外部状态，不该被判成「声明了却没注册」。这条豁免是结构性的——`_declared` 里
        本来就没有命名空间声明。

        **异常约定**：有声明未兑现抛 `PLUGIN_LOAD_FAILED`，`detail["unfulfilled"]` 列出
        全部缺项——一次报全，与 `validate_config()` 同构。
        """
        unfulfilled = sorted(
            f"{kind.value}:{name}" for kind, name in self._declared if (kind, name) not in self._used
        )
        if unfulfilled:
            raise NucleaError(
                ErrorCode.PLUGIN_LOAD_FAILED,
                "manifest 声明的能力未在 setup 中注册。",
                detail={"provider": str(self._batch.provider), "unfulfilled": unfulfilled},
            )
