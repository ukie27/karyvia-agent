"""能力契约：能力标识、arity 表与 Hook 表面。

职责：定义 `CapabilityKind` 的 10 个取值与每个 kind 的 arity 常量表、结构化的
`ProviderId`（`Builtin` | `Plugin`）与 `CapabilityRef`，以及冻结的 9 个 `HookName`、
其观察者/拦截器分类和 Hook 的输入输出 `HookContext` / `HookOutcome`。
不负责：注册、冲突解析、覆盖判定、Hook 的调度与超时——那些在 `kernel/registry/`、
`kernel/observability/` 与 `kernel/turn/`；本模块不含任何 IO。

三件必须由本模块（而不是各注册点）统一持有的东西：

- **arity 表**决定全部冲突语义。它是数据不是文档，`CAPABILITY_ARITY` 缺一个 kind
  就构造不出对应的判定，注册器也就没有「按加载顺序择一」的可乘之机。
- **`ProviderId` 是联合类型而不是裸字符串**。"builtin" 与某个恰好叫
  builtin 的插件在字符串世界里无法区分，在类型世界里连写错的机会都没有。
- **覆盖目标的编解码**（`CapabilityRef.target` / `parse_capability_target`）。Manifest
  声明与 Registry 覆盖解析必须复用同一份实现，两处各写一套
  正则是这类字段最典型的失配来源。

`MEMORY` 使用 `MULTI_UNIQUE`：`register_memory_provider(name, m)` 的名字允许多个后端
并存，也让故障降级或切换后端不必先卸载现有实现。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, TypeAlias

from .context import ContextFragment
from .errors import ErrorCode, KaryviaError
from .ids import Correlation, PluginId, validate_identifier
from .message import InboundMessage
from .model import ModelRequest, ModelResponse
from .session import TurnOutcome
from .tool import ToolInvocation, ToolResult

__all__ = [
    "CAPABILITY_ARITY",
    "HOOK_KINDS",
    "HOOK_REQUIRED_SLOTS",
    "Builtin",
    "CapabilityArity",
    "CapabilityKind",
    "CapabilityRef",
    "HookAction",
    "HookContext",
    "HookKind",
    "HookName",
    "HookOutcome",
    "Plugin",
    "ProviderId",
    "parse_capability_target",
    "parse_provider",
    "provider_sort_key",
]

#: 能力名的形状。比工具名宽（要容纳 `openai-compat`、`cli`、`memory-sqlite`），
#: 但仍限制在可安全嵌入标识串的字符集内。工具名另有更严格的形状，见 `tool.py`。
_NAME_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

#: `ProviderId` 与能力名之间的分隔符，也是 `overrides` 字段的分隔符。
_TARGET_SEPARATOR: Final = ":"

#: `Builtin` 的渲染值。它字典序小于 `"plugin:"`，因此按 provider 排序时内建天然在前，
#: 与 §6.1「内建能力优先注册，priority 基准值 0」一致，不需要额外的排序特例。
_BUILTIN_TOKEN: Final = "builtin"

#: `Plugin` 的渲染前缀。
_PLUGIN_TOKEN: Final = "plugin"


class CapabilityKind(StrEnum):
    """可注册的能力种类，恰好 10 个。

    与 `sdk.KaryviaAPI` 的 10 个注册方法一一对应：新增 kind 等于新增注册方法，
    属于公开表面变化，须按  论证。
    """

    TOOL = "tool"
    COMMAND = "command"
    CONTEXT = "context"
    TURN_COMPACTOR = "turn_compactor"
    HOOK = "hook"
    CHANNEL = "channel"
    MODEL = "model"
    MEMORY = "memory"
    SESSION_STORE = "session_store"
    CLI_ENTRY = "cli_entry"

    @property
    def arity(self) -> CapabilityArity:
        """本 kind 的 arity，等价于 `CAPABILITY_ARITY[self]`。"""
        return CAPABILITY_ARITY[self]


class CapabilityArity(StrEnum):
    """一个 kind 内允许存在多少个实现，决定冲突语义。"""

    MULTI = "multi"
    """可同名并存，全部生效，按 `(priority, provider)` 排序。"""

    MULTI_UNIQUE = "multi_unique"
    """可有多个实现，但 name 在 kind 内唯一；同名重复即启动错误，除非显式覆盖。"""

    SINGLETON = "singleton"
    """唯一生效实现；替换必须显式声明覆盖。"""


#: kind 到 arity 的唯一映射。
#: 10 个 kind 全部登记，缺项会让 `CapabilityKind.arity` 直接 KeyError——这是刻意的：
#: 冲突语义未定的能力不该有注册路径。
CAPABILITY_ARITY: Final[Mapping[CapabilityKind, CapabilityArity]] = MappingProxyType(
    {
        CapabilityKind.TOOL: CapabilityArity.MULTI_UNIQUE,
        CapabilityKind.COMMAND: CapabilityArity.MULTI_UNIQUE,
        CapabilityKind.CONTEXT: CapabilityArity.MULTI,
        CapabilityKind.TURN_COMPACTOR: CapabilityArity.MULTI_UNIQUE,
        CapabilityKind.HOOK: CapabilityArity.MULTI,
        CapabilityKind.CHANNEL: CapabilityArity.MULTI_UNIQUE,
        CapabilityKind.MODEL: CapabilityArity.MULTI_UNIQUE,
        CapabilityKind.MEMORY: CapabilityArity.MULTI_UNIQUE,
        CapabilityKind.SESSION_STORE: CapabilityArity.SINGLETON,
        CapabilityKind.CLI_ENTRY: CapabilityArity.SINGLETON,
    }
)


# ------------------------------------------------------------------------- ProviderId


@dataclass(frozen=True, slots=True)
class Builtin:
    """内建提供方。无字段——内建实现只有一个来源，不需要再区分是哪个内建包。"""

    def __str__(self) -> str:
        return _BUILTIN_TOKEN


@dataclass(frozen=True, slots=True)
class Plugin:
    """插件提供方，由 manifest 中的稳定 `plugin_id` 标识。"""

    plugin_id: PluginId

    def __post_init__(self) -> None:
        validate_identifier("provider.plugin_id", self.plugin_id)
        if not _NAME_PATTERN.match(self.plugin_id):
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                "插件标识形状非法（应为小写、点/中划线分隔）。",
                detail={"plugin_id": self.plugin_id},
            )

    def __str__(self) -> str:
        return f"{_PLUGIN_TOKEN}{_TARGET_SEPARATOR}{self.plugin_id}"


#: 能力提供方。用联合类型而不是裸字符串：一个恰好叫 "builtin" 的插件
#: 在字符串世界里能冒充内建，在类型世界里连表达这件事的方式都没有。
ProviderId: TypeAlias = Builtin | Plugin


def provider_sort_key(provider: ProviderId) -> str:
    """排序键。`Builtin` 渲染为 `"builtin"`，字典序小于任何 `"plugin:*"`。

    §6.1 要求「同 priority 按 provider id 字典序」，这就是那个字典序的唯一定义。
    """
    return str(provider)


def parse_provider(text: str) -> ProviderId:
    """`str(provider)` 的逆运算。形状非法时抛 `INPUT_MALFORMED`。"""
    if text == _BUILTIN_TOKEN:
        return Builtin()
    prefix, separator, plugin_id = text.partition(_TARGET_SEPARATOR)
    if not separator or prefix != _PLUGIN_TOKEN or not plugin_id:
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            "提供方标识形状非法（应为 builtin 或 plugin:<id>）。",
            detail={"provider": text},
        )
    return Plugin(PluginId(plugin_id))


def parse_capability_target(text: str) -> tuple[ProviderId, str]:
    """解析 manifest 的 `overrides` 目标：`"builtin:fs.read"` / `"plugin:<id>:<name>"`。

    返回 `(提供方, 能力名)`。kind 不在目标串里——它由声明该覆盖的 `CapabilityDecl`
    自己带，重复编码只会多出一处可以对不上的信息。
    """
    provider_text, separator, name = text.rpartition(_TARGET_SEPARATOR)
    if not separator or not provider_text or not name:
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            "覆盖目标形状非法（应为 builtin:<name> 或 plugin:<id>:<name>）。",
            detail={"target": text},
        )
    provider = parse_provider(provider_text)
    if not _NAME_PATTERN.match(name):
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            "覆盖目标的能力名形状非法。",
            detail={"target": text, "name": name},
        )
    return provider, name


@dataclass(frozen=True, slots=True)
class CapabilityRef:
    """一个能力的完整标识。

    `name` 在 kind 内定位能力，`provider` 说明由谁提供，`version` 用于诊断与兼容展示
    （报告必须标明每项能力由内建还是插件提供）。四个字段合起来才唯一——
    同名不同 provider 正是「覆盖」与「shadowed」要表达的关系。
    """

    kind: CapabilityKind
    name: str
    provider: ProviderId
    version: str = "0"

    def __post_init__(self) -> None:
        validate_identifier("capability.name", self.name)
        if not _NAME_PATTERN.match(self.name):
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                "能力名形状非法（应为小写、点/中划线分隔）。",
                detail={"kind": self.kind.value, "name": self.name},
            )
        validate_identifier("capability.version", self.version)

    @property
    def arity(self) -> CapabilityArity:
        """本能力所属 kind 的 arity。"""
        return self.kind.arity

    @property
    def target(self) -> str:
        """覆盖目标串，`parse_capability_target()` 的逆运算。"""
        return f"{self.provider}{_TARGET_SEPARATOR}{self.name}"

    @property
    def sort_key(self) -> tuple[str, str]:
        """`(provider, name)` 排序键，用于报告与同 priority 时的确定性排序。"""
        return (provider_sort_key(self.provider), self.name)


# ------------------------------------------------------------------------------ Hook


class HookKind(StrEnum):
    """扩展点的两种语义。混在一个机制里会让失败隔离规则无法自洽。"""

    OBSERVER = "observer"
    """只读。并发执行、整体超时，异常与超时只记 `PLUGIN_FAILURE`，不影响 turn。"""

    INTERCEPTOR = "interceptor"
    """可改变流水线。顺序执行，顺序 = `(priority, plugin_id)`，每个 handler 独立超时。"""


class HookName(StrEnum):
    """冻结的 9 个 Hook。新增须按  论证。"""

    INSTANCE_READY = "instance_ready"
    INSTANCE_SHUTDOWN = "instance_shutdown"
    TURN_START = "turn_start"
    CONTEXT_ASSEMBLE = "context_assemble"
    BEFORE_MODEL_REQUEST = "before_model_request"
    AFTER_MODEL_RESPONSE = "after_model_response"
    BEFORE_TOOL_CALL = "before_tool_call"
    AFTER_TOOL_CALL = "after_tool_call"
    TURN_END = "turn_end"

    @property
    def kind(self) -> HookKind:
        """本 Hook 是观察者还是拦截器，等价于 `HOOK_KINDS[self]`。"""
        return HOOK_KINDS[self]


#: Hook 到其语义的唯一映射。
HOOK_KINDS: Final[Mapping[HookName, HookKind]] = MappingProxyType(
    {
        HookName.INSTANCE_READY: HookKind.OBSERVER,
        HookName.INSTANCE_SHUTDOWN: HookKind.OBSERVER,
        HookName.TURN_START: HookKind.INTERCEPTOR,
        HookName.CONTEXT_ASSEMBLE: HookKind.INTERCEPTOR,
        HookName.BEFORE_MODEL_REQUEST: HookKind.INTERCEPTOR,
        HookName.AFTER_MODEL_RESPONSE: HookKind.OBSERVER,
        HookName.BEFORE_TOOL_CALL: HookKind.INTERCEPTOR,
        HookName.AFTER_TOOL_CALL: HookKind.INTERCEPTOR,
        HookName.TURN_END: HookKind.OBSERVER,
    }
)

#: 每个 Hook 必须填充的 `HookContext` 槽位。这张表是 §6.6 表格的可执行形态：
#: 「`before_tool_call` 能改工具参数」这句话，落地就是「它一定拿得到 `invocation`」。
#: 未列出的槽位允许缺席，但不禁止填充——多给一份只读上下文不会让 handler 判断出错。
HOOK_REQUIRED_SLOTS: Final[Mapping[HookName, frozenset[str]]] = MappingProxyType(
    {
        HookName.INSTANCE_READY: frozenset(),
        HookName.INSTANCE_SHUTDOWN: frozenset(),
        HookName.TURN_START: frozenset({"correlation", "message"}),
        HookName.CONTEXT_ASSEMBLE: frozenset({"correlation", "fragments"}),
        HookName.BEFORE_MODEL_REQUEST: frozenset({"correlation", "request"}),
        HookName.AFTER_MODEL_RESPONSE: frozenset({"correlation", "response"}),
        HookName.BEFORE_TOOL_CALL: frozenset({"correlation", "invocation"}),
        HookName.AFTER_TOOL_CALL: frozenset({"correlation", "invocation", "result"}),
        HookName.TURN_END: frozenset({"correlation", "outcome"}),
    }
)


@dataclass(frozen=True, slots=True)
class HookContext:
    """交给 handler 的只读上下文。

    用「一个类型 + 若干可选强类型槽 + 必填表」而不是 9 个专用类型：Hook 集合已经冻结，
    专用类型只会让 `HookHandler` 变成 9 个 Protocol，与  正面冲突；而用
    `Mapping[str, JsonValue]` 装载荷则会把 `ModelRequest` 这类结构化对象拍平成字典，
    handler 想改写请求就只能自己拼回去。

    实例级 Hook（`instance_ready` / `instance_shutdown`）没有 `correlation`：那时还没有
    会话与 turn。
    """

    hook: HookName
    correlation: Correlation | None = None
    message: InboundMessage | None = None
    fragments: tuple[ContextFragment, ...] = ()
    request: ModelRequest | None = None
    response: ModelResponse | None = None
    invocation: ToolInvocation | None = None
    result: ToolResult | None = None
    outcome: TurnOutcome | None = None

    def __post_init__(self) -> None:
        present = {
            "correlation": self.correlation is not None,
            "message": self.message is not None,
            "fragments": bool(self.fragments),
            "request": self.request is not None,
            "response": self.response is not None,
            "invocation": self.invocation is not None,
            "result": self.result is not None,
            "outcome": self.outcome is not None,
        }
        missing = sorted(slot for slot in HOOK_REQUIRED_SLOTS[self.hook] if not present[slot])
        if missing:
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "Hook 上下文缺少该 Hook 必需的槽位。",
                detail={"hook": self.hook.value, "missing": missing},
            )

    @property
    def kind(self) -> HookKind:
        """本次分发的 Hook 语义，决定返回值会不会被采纳。"""
        return self.hook.kind


class HookAction(StrEnum):
    """拦截器的处置方式。"""

    CONTINUE = "continue"
    """无意见，流水线原样继续。Observer 只能返回它或 `None`。"""

    REJECT = "reject"
    """终止本 turn（仅 `turn_start`）。必须给出 `reason`，它会进入用户可见的诊断。"""

    BLOCK = "block"
    """不执行本次工具调用（仅 `before_tool_call`）。必须给出 `reason`。"""

    REPLACE = "replace"
    """用载荷替换流水线中的对应对象。恰好携带一种载荷，由 Kernel 按 Hook 取用：
    `context_assemble` 取 `fragments`、`before_model_request` 取 `request`、
    `before_tool_call` 取 `invocation`（这就是「改写工具参数」）、
    `after_tool_call` 取 `result`。"""


@dataclass(frozen=True, slots=True)
class HookOutcome:
    """handler 的返回值。返回 `None` 与 `CONTINUE` 等价。

    载荷四选一而不是「全填、Kernel 自己挑」：让一个结果同时带片段、请求和工具结果，
    调度器就只能靠当前 Hook 反推该用哪个，一旦 handler 填错就是静默失效。
    """

    action: HookAction
    fragments: tuple[ContextFragment, ...] = ()
    request: ModelRequest | None = None
    invocation: ToolInvocation | None = None
    result: ToolResult | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        payloads = [
            name
            for name, filled in (
                ("fragments", bool(self.fragments)),
                ("request", self.request is not None),
                ("invocation", self.invocation is not None),
                ("result", self.result is not None),
            )
            if filled
        ]
        if self.action is HookAction.REPLACE and len(payloads) != 1:
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "REPLACE 必须且只能携带一种载荷。",
                detail={"payloads": payloads},
            )
        if self.action is not HookAction.REPLACE and payloads:
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "只有 REPLACE 可以携带载荷。",
                detail={"action": self.action.value, "payloads": payloads},
            )
        if self.action in (HookAction.REJECT, HookAction.BLOCK) and not self.reason:
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "拒绝与阻断必须给出原因，否则用户只会看到「什么都没发生」。",
                detail={"action": self.action.value},
            )
