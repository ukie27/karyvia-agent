"""内建 Context Provider：无 Memory、无检索插件时的可用上下文（技术方案 §8.1、`CTX-006`）。

职责：实现 `ContextProvider`，为每次 turn 贡献基线系统指令、运行时事实与运维配置的自定义
指令三类片段；同时提供内建注册入口 `setup(api)`。
不负责：重放会话历史、按预算裁剪、决定片段的最终位置（都在
`kernel/turn/context_builder.py`）、持久化任何东西——**本模块不做任何 IO**。

三条决定了本模块形状的规则：

- **Provider 只读不写**（技术方案 §14 的职责划分风险项）。它连 `os` / `pathlib` 都不
  import，由架构测试的一条扫描盯着。「贡献上下文」和「记住些什么」是两件事，后者是
  Memory 插件。
- **`trust` 分成两级**：内建基线指令与运行时事实是系统自己产出的，`trust=SYSTEM`；
  运维在 `config.json` 里写的 `instructions` 是 `TrustLevel.OPERATOR`——契约对这一级的
  定义就是「实例拥有者通过配置显式提供的内容，可信但不是系统本身」。代价是它落在
  历史之后的一条 user 消息里而不是 system 消息里，因此给它 `priority=0`（与内建基准同级、
  最晚被裁）。想让配置文本进系统指令位置，等于取消 `CMD-005` 的分级，那不是一个内建能力
  该自行决定的事。
- **历史不由这里贡献**。§8.1 原文写的是「系统指令 + 历史 + 尾部保留裁剪」，但 `D14` 之后
  历史重放（`replay_messages`）与从最旧丢起的裁剪都在组装器里。Provider 再贡献一份历史
  片段就是把同一段对话讲两遍，还绕过了 `EDG-305` 的投影规则。见交接文档对 §8.1 的细化。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Final

from nucleamind.contracts import (
    CancelSignal,
    ContextFragment,
    Correlation,
    ErrorCode,
    FragmentKind,
    FragmentScope,
    JsonValue,
    NucleaError,
    SessionSnapshot,
    TrustLevel,
)
from nucleamind.sdk import NucleaAPI, PluginContext

from .instructions import (
    BASELINE_INSTRUCTIONS,
    estimate_tokens,
    normalize_instructions,
    render_runtime_facts,
)

__all__ = [
    "CAPABILITY_NAME",
    "CONFIG_INSTRUCTIONS_KEY",
    "CONFIG_RUNTIME_FACTS_KEY",
    "CONFIG_USE_BASELINE_KEY",
    "FRAGMENT_SOURCE",
    "OPERATOR_PRIORITY",
    "BasicContextProvider",
    "BasicContextSettings",
    "resolve_settings",
    "setup",
]

#: 本内建的能力名。CONTEXT 是 MULTI，因此这个名字是诊断标签而不是唯一键。
CAPABILITY_NAME: Final = "basic"

#: 全部片段的 `source`。形状受 `contracts/context.py::_SOURCE_PATTERN` 约束
#: （`builtin:` 前缀 + 小写标识），也是诊断里「这段是谁塞进来的」的答案。
FRAGMENT_SOURCE: Final = "builtin:context_basic"

#: 三个配置键。manifest 的 `config_schema` 与这里必须一致，由测试对照。
CONFIG_INSTRUCTIONS_KEY: Final = "instructions"
CONFIG_USE_BASELINE_KEY: Final = "use_baseline_instructions"
CONFIG_RUNTIME_FACTS_KEY: Final = "include_runtime_facts"

#: 运维指令片段的优先级。取 0 = 内建基准 = `HISTORY_TRIM_PRIORITY`：它进不了系统指令
#: 位置（`trust=OPERATOR`），但也不该比一个第三方检索插件的片段更早被丢。
OPERATOR_PRIORITY: Final = 0

#: 「既不要基线、也不给自定义指令」的拒绝理由。抽成常量是为了让 `TRY003` 与「失败信息
#: 要说人话」同时成立（与 `sdk/testing/contracts.py` 的做法一致）。
_NO_INSTRUCTIONS_AT_ALL: Final = (
    "关闭了基线系统指令就必须提供自定义指令，否则本 Provider 将不产出任何指令。"
)


class BasicContextSettings:
    """一份已校验的配置。构造后不可变，`provide()` 每轮直接读它。

    刻意不是 dataclass：`instructions` 已经在 `resolve_settings()` 里规范化过，用普通类
    可以让「规范化只发生一次」在类型上就成立——没有一个能绕过校验的公开构造路径。
    """

    __slots__ = ("_baseline", "_instructions", "_runtime_facts")

    def __init__(
        self, *, instructions: str, use_baseline: bool, include_runtime_facts: bool
    ) -> None:
        self._instructions = instructions
        self._baseline = use_baseline
        self._runtime_facts = include_runtime_facts

    @property
    def instructions(self) -> str:
        """运维自定义指令，未配置时为空串。"""
        return self._instructions

    @property
    def use_baseline(self) -> bool:
        return self._baseline

    @property
    def include_runtime_facts(self) -> bool:
        return self._runtime_facts


class BasicContextProvider:
    """`ContextProvider` 的内建实现。纯内存、无状态、每次返回同样顺序的片段。

    `clock` 可注入是为了让运行时事实片段可被逐字符断言；生产路径用默认的 UTC 时钟。
    """

    __slots__ = ("_clock", "_settings")

    def __init__(
        self,
        settings: BasicContextSettings,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def settings(self) -> BasicContextSettings:
        return self._settings

    async def provide(
        self,
        snapshot: SessionSnapshot,
        correlation: Correlation,
        cancel: CancelSignal,
    ) -> tuple[ContextFragment, ...]:
        """贡献本轮片段。空会话同样有产出（`CTX-006`、`EDG-307`）。

        **异常约定**：不抛。三段文本全部来自已校验的配置与快照自身的字段，没有可失败的
        外部依赖；配置非法在 `setup()` 时就已经抛出（`resolve_settings`），而不是拖到第
        一次 turn。
        **取消语义**：不检查 `cancel`——契约要求的是「每个**外部查询**前检查」，而本实现
        一次外部往返也没有。加一个必然为假的检查点只会让人以为这里有阻塞操作。
        """
        del correlation, cancel  # 本实现不需要：无外部往返、无关联日志。

        fragments: list[ContextFragment] = []
        if self._settings.use_baseline:
            fragments.append(
                ContextFragment(
                    source=FRAGMENT_SOURCE,
                    kind=FragmentKind.SYSTEM,
                    content=BASELINE_INSTRUCTIONS,
                    priority=0,
                    estimated_tokens=estimate_tokens(BASELINE_INSTRUCTIONS),
                    scope=FragmentScope.AGENT,
                    trust=TrustLevel.SYSTEM,
                )
            )
        if self._settings.include_runtime_facts:
            facts = render_runtime_facts(
                now=self._clock(),
                session_key=snapshot.session_key,
                live_count=len(snapshot.live_messages),
                compacted_count=snapshot.compacted_through,
            )
            fragments.append(
                ContextFragment(
                    source=FRAGMENT_SOURCE,
                    kind=FragmentKind.RUNTIME,
                    content=facts,
                    priority=0,
                    estimated_tokens=estimate_tokens(facts),
                    scope=FragmentScope.SESSION,
                    trust=TrustLevel.SYSTEM,
                )
            )
        if self._settings.instructions:
            fragments.append(
                ContextFragment(
                    source=FRAGMENT_SOURCE,
                    # `kind=SYSTEM` 但 `trust=OPERATOR`：种类说的是「它是一段指令」，
                    # 位置由 `trust` 决定（组装器的规则 2）。两者不是同一件事。
                    kind=FragmentKind.SYSTEM,
                    content=self._settings.instructions,
                    priority=OPERATOR_PRIORITY,
                    estimated_tokens=estimate_tokens(self._settings.instructions),
                    scope=FragmentScope.AGENT,
                    trust=TrustLevel.OPERATOR,
                )
            )
        return tuple(fragments)


# ------------------------------------------------------------------------------ 注册入口


def _invalid(message: str, **detail: object) -> NucleaError:
    return NucleaError(ErrorCode.CONFIG_INVALID, message, detail=detail)


def _read_bool(config: Mapping[str, JsonValue], key: str, *, default: bool) -> bool:
    value = config.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise _invalid("该配置项必须是布尔值。", key=key, actual_type=type(value).__name__)
    return value


def _read_instructions(config: Mapping[str, JsonValue]) -> str:
    """读 `instructions`：允许字符串或字符串数组，数组按行拼接。

    接受数组不是为了花样多，而是因为 JSON 里写一段多行提示词只有这两种写法，而「配置
    写法合法但被静默忽略」是本项目一贯拒绝的那类失败。
    """
    value = config.get(CONFIG_INSTRUCTIONS_KEY)
    if value is None:
        return ""
    if isinstance(value, str):
        return normalize_instructions(value.splitlines())
    if isinstance(value, Sequence) and all(isinstance(line, str) for line in value):
        return normalize_instructions([line for line in value if isinstance(line, str)])
    raise _invalid(
        "自定义指令必须是字符串或字符串数组。",
        key=CONFIG_INSTRUCTIONS_KEY,
        actual_type=type(value).__name__,
    )


def resolve_settings(ctx: PluginContext) -> BasicContextSettings:
    """把 `ctx.config` 校验成一份设置（`CFG-002`：只看得到自己那一块）。

    **异常约定**：类型不对抛 `CONFIG_INVALID`；同时关掉基线指令又不配 `instructions`
    也抛——那等于要求一个没有任何系统指令的 Agent，`CTX-006` 不允许内建 Provider 悄悄
    产出一份空上下文。要真的不要系统指令，正规做法是在 `plugins.disable` 里禁用本内建。
    """
    config = ctx.config
    use_baseline = _read_bool(config, CONFIG_USE_BASELINE_KEY, default=True)
    instructions = _read_instructions(config)
    if not use_baseline and not instructions:
        raise _invalid(_NO_INSTRUCTIONS_AT_ALL, key=CONFIG_USE_BASELINE_KEY)
    return BasicContextSettings(
        instructions=instructions,
        use_baseline=use_baseline,
        include_runtime_facts=_read_bool(config, CONFIG_RUNTIME_FACTS_KEY, default=True),
    )


def setup(api: NucleaAPI) -> None:
    """内建注册入口，`BUILTIN_MANIFESTS` 里那条 manifest 的 `setup` 指向它。

    配置在这里校验一次并固化进 Provider，避免把错误拖到用户发出第一条消息时。
    """
    api.register_context_provider(CAPABILITY_NAME, BasicContextProvider(resolve_settings(api.ctx)))
