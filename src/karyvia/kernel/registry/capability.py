"""能力登记：注册项、事务性批次与冻结的注册表（技术方案 §6.1，需求 `EDG-103`、`NFR-403`）。

职责：定义 `Registration`（一条登记）、`RegistrationBatch`（先入暂存区、`commit()` 才并入
的事务性批次）与 `CapabilityRegistry`（登记容器 + 冻结 + `dict[(kind, name)]` 的 O(1)
查找）。
不负责：判定谁覆盖谁、谁最终生效——那是 `resolution.py`；也不负责发现插件、导入 `setup`、
插件加载（`kernel/plugins/`）。本模块不读文件、不访问网络、不碰全局状态。

三条贯穿本模块的约定：

- **注册与生效是两件事**。`commit()` 只表示「登记完成」，不表示「这个能力会被用到」；
  同名并存、覆盖与遮蔽都留给 `resolution.py` 统一裁决。注册点自己判冲突，就等于把
  「先注册的赢」写进了实现，而 `EDG-102` 要的恰恰是覆盖永不由加载顺序决定。
- **批次是全有或全无**（`EDG-103`）。`setup(api)` 中途抛异常时 registry 不得留下半注册
  状态，因此批次用上下文管理器形态：正常退出即 `commit()`，异常退出即 `rollback()` 并
  原样抛出。这不是便利封装，是唯一保证不遗漏 rollback 的写法。
- **冻结后只读**。解析完成即冻结，之后的写入抛 `KERNEL_INVARIANT_VIOLATED`；反过来，
  冻结之前不允许查找——一个「还没定下来」的注册表被查到的值随时可能被覆盖掉，让它可查
  就是在鼓励调用方依赖中间状态。

`payload` 的类型是 `object` 而不是 `Any`：注册表不解释载荷，只搬运它。用 `object` 时
调用方想使用载荷就必须先窄化，用 `Any` 则会让未经检查的值一路流进 Kernel
（`AGENTS.md` 约束 6）。按 kind 还原具体类型是消费方的事，在 `D16` 的 Host 分派里完成。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType

from karyvia.contracts import (
    Builtin,
    CapabilityArity,
    CapabilityKind,
    CapabilityRef,
    ErrorCode,
    KaryviaError,
    ProviderId,
    parse_capability_target,
    provider_sort_key,
)

__all__ = [
    "BUILTIN_BASE_PRIORITY",
    "PLUGIN_BASE_PRIORITY",
    "BatchState",
    "CapabilityRegistry",
    "Registration",
    "RegistrationBatch",
    "base_priority_for",
]

#: 内建能力的 priority 基准值（技术方案 §6.1 解析规则 1、§15 决策表第 3 行）。
#: 内建排在前面**不是**靠 provider 字典序，而是靠这个数值；它只决定能力解析与调用顺序，
#: 不参与 token 预算或压缩判断。
BUILTIN_BASE_PRIORITY = 0

#: 插件能力的 priority 基准值，与 manifest 的 `CapabilityDecl.priority` 默认值
#: （技术方案 §7.2）和 `KaryviaAPI.on(..., priority=100)` 一致。
#: 插件仍可显式声明更小的值，同值时按 provider 字典序，内建依然在前。
PLUGIN_BASE_PRIORITY = 100


def base_priority_for(provider: ProviderId) -> int:
    """提供方的 priority 基准值。内建 0，插件 100（§6.1 规则 1、§7.2）。

    这条规则放在 registry 而不是各注册点：让调用方「记得给内建传 0」，
    就等于把一条冲突语义交给了每一个 bootstrap 路径去复述。
    """
    return BUILTIN_BASE_PRIORITY if isinstance(provider, Builtin) else PLUGIN_BASE_PRIORITY


class BatchState(StrEnum):
    """批次的三个状态。终态不可逆——重复提交与提交后回滚都是调用方的逻辑错误。"""

    OPEN = "open"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True, slots=True)
class Registration:
    """一条登记：谁、提供了什么、以什么优先级、是否声明覆盖。

    `overrides` 是**唯一**的覆盖来源，取自 manifest 的同名字段（技术方案 §7.2）。这里存
    原始串而不是解析结果：`kernel/` 不能 import `sdk/`（规则 `R2`），manifest 类型在
    `sdk.manifest`，因此跨层传递的只能是契约层认识的东西——而 `parse_capability_target`
    正在契约层，两边解码同一份实现，不会出现两套正则对不上的情况。
    """

    ref: CapabilityRef
    payload: object
    priority: int = PLUGIN_BASE_PRIORITY
    overrides: str | None = None

    def __post_init__(self) -> None:
        if self.priority < 0:
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "能力优先级不得为负。",
                detail={"capability": self.ref.target, "priority": self.priority},
                capability=self.ref,
            )
        target = self.override_target
        if target is not None and target == (self.ref.provider, self.ref.name):
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "能力不得覆盖自身。",
                detail={"capability": self.ref.target},
                capability=self.ref,
            )

    @property
    def slot(self) -> tuple[CapabilityKind, str]:
        """`(kind, name)` 唯一性键。SINGLETON 的分组键更粗，见 `resolution.py`。"""
        return (self.ref.kind, self.ref.name)

    @property
    def override_target(self) -> tuple[ProviderId, str] | None:
        """解码后的覆盖目标 `(提供方, 能力名)`；未声明覆盖时为 `None`。

        形状非法时由 `parse_capability_target` 抛 `INPUT_MALFORMED`——这条路径通常在
        manifest 校验阶段就被拦掉了，留在这里是因为内建清单不走 manifest 解析。
        """
        return None if self.overrides is None else parse_capability_target(self.overrides)

    @property
    def sort_key(self) -> tuple[int, str, str]:
        """`(priority, provider 字典序, name)`。

        §6.1 只规定了「同 priority 按 provider id 字典序」，末尾补上 `name` 是为了让
        CONTEXT / HOOK 这类同 provider 可注册多项的 kind 也有全序，否则报告的行序会随
        字典插入顺序漂移，快照测试就只能改成集合比较。
        """
        return (self.priority, provider_sort_key(self.ref.provider), self.ref.name)


class RegistrationBatch:
    """一个提供方的一批登记，全有或全无（`EDG-103`）。

    用法是上下文管理器，正常退出即提交、异常退出即回滚：

        with registry.batch(Plugin(PluginId("acme"))) as batch:
            batch.add(CapabilityKind.TOOL, "fs.read", handler)

    也可以显式 `commit()` / `rollback()`——`D16` 的 Host 需要在 `setup(api)` 返回之后
    才提交，而 `setup` 的调用点与批次的创建点不在同一个作用域。
    """

    __slots__ = ("_provider", "_registry", "_slots", "_staged", "_state")

    def __init__(self, registry: CapabilityRegistry, provider: ProviderId) -> None:
        self._registry = registry
        self._provider = provider
        self._staged: list[Registration] = []
        self._slots: set[tuple[CapabilityKind, str]] = set()
        self._state = BatchState.OPEN

    @property
    def provider(self) -> ProviderId:
        """本批次的提供方。批次内每条登记的 `ref.provider` 都由它填充。"""
        return self._provider

    @property
    def state(self) -> BatchState:
        """批次状态。`OPEN` 之外都是终态。"""
        return self._state

    @property
    def staged(self) -> tuple[Registration, ...]:
        """暂存区内容，按加入顺序。提交前对 registry 不可见。"""
        return tuple(self._staged)

    def add(
        self,
        kind: CapabilityKind,
        name: str,
        payload: object,
        *,
        version: str = "0",
        priority: int | None = None,
        overrides: str | None = None,
    ) -> Registration:
        """把一条登记放进暂存区，返回构造出的 `Registration`。

        `provider` 不是参数——它由批次决定。让调用方传 provider 就等于允许一个插件以
        另一个插件（或内建）的名义注册能力，那会让 `PLG-006`「报告标明每项能力的来源」
        变成一句没有约束力的话。

        `priority` 省略时取提供方的基准值（内建 0、插件 100，见 `base_priority_for()`），
        而不是一个写死的常量：内建的基准是 §6.1 规则 1 定的，不该由每个 bootstrap 复述。
        显式传值一律优先，包括插件传 0。

        **异常约定**：批次已终结抛 `KERNEL_INVARIANT_VIOLATED`；同一批次内重复的
        `(kind, name)` 抛 `PLUGIN_REGISTRATION_CONFLICT`——一个提供方给同一个槽位交两份
        实现，跨提供方的同名冲突则留给覆盖解析裁决。
        """
        self._require_open("向批次追加登记")
        ref = CapabilityRef(kind=kind, name=name, provider=self._provider, version=version)
        if (kind, name) in self._slots:
            raise KaryviaError(
                ErrorCode.PLUGIN_REGISTRATION_CONFLICT,
                "同一提供方在一个批次内重复注册了同一能力。",
                detail={"capability": ref.target, "kind": kind.value},
                capability=ref,
            )
        registration = Registration(
            ref=ref,
            payload=payload,
            priority=base_priority_for(self._provider) if priority is None else priority,
            overrides=overrides,
        )
        self._staged.append(registration)
        self._slots.add((kind, name))
        return registration

    def commit(self) -> None:
        """把暂存区整体并入 registry。空批次也是合法的提交。

        **异常约定**：批次已终结或 registry 已冻结抛 `KERNEL_INVARIANT_VIOLATED`。
        """
        self._require_open("提交批次")
        self._registry.absorb(self._staged)
        self._state = BatchState.COMMITTED

    def rollback(self) -> None:
        """丢弃暂存区。已回滚时是幂等的空操作——清理路径不该自己制造第二个错误。

        **异常约定**：已提交的批次不能回滚，抛 `KERNEL_INVARIANT_VIOLATED`。提交之后
        registry 可能已经被解析和冻结，「撤回」在那之后没有可定义的语义。
        """
        if self._state is BatchState.COMMITTED:
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "已提交的批次无法回滚。",
                detail={"provider": str(self._provider)},
            )
        self._staged.clear()
        self._slots.clear()
        self._state = BatchState.ROLLED_BACK

    def _require_open(self, action: str) -> None:
        if self._state is not BatchState.OPEN:
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                f"批次已终结，无法{action}。",
                detail={"provider": str(self._provider), "state": self._state.value},
            )

    def __enter__(self) -> RegistrationBatch:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """异常即回滚，正常即提交；两条路径都不吞异常。

        已被显式 `commit()` / `rollback()` 终结的批次在这里不再动作，因此显式用法与
        `with` 用法可以混用。
        """
        if self._state is not BatchState.OPEN:
            return
        if exc_type is not None:
            self.rollback()
        else:
            self.commit()


class CapabilityRegistry:
    """能力登记容器。解析前只写不读，解析后只读不写（`NFR-403`）。

    「冻结前不可查找」是刻意的：注册表在解析完成前不知道谁会被覆盖，此时返回的任何结果
    都可能在下一次提交后失效。让它可查，调用方就会在启动期的某个角落缓存一个随后被
    覆盖掉的实现，而这种问题只在装了覆盖插件的用户那里复现。
    """

    __slots__ = ("_active", "_frozen", "_index", "_registrations")

    def __init__(self) -> None:
        self._registrations: list[Registration] = []
        self._active: tuple[Registration, ...] = ()
        self._index: dict[tuple[CapabilityKind, str], tuple[Registration, ...]] = {}
        self._frozen = False

    @property
    def frozen(self) -> bool:
        """是否已冻结。冻结由 `resolution.resolve_into()` 完成。"""
        return self._frozen

    @property
    def registrations(self) -> tuple[Registration, ...]:
        """全部已提交登记，按提交顺序。这是**解析前**的原始集合，含将被遮蔽的项。"""
        return tuple(self._registrations)

    def batch(self, provider: ProviderId) -> RegistrationBatch:
        """为一个提供方开一个批次。

        **异常约定**：registry 已冻结抛 `KERNEL_INVARIANT_VIOLATED`。
        """
        self._require_writable("开启新批次")
        return RegistrationBatch(self, provider)

    def absorb(self, registrations: Sequence[Registration]) -> None:
        """并入一批登记。由 `RegistrationBatch.commit()` 调用，不是给调用方的入口。

        直接调用它等于绕过批次的原子性；之所以不加下划线，是因为它跨对象被调用，
        伪装成私有方法只会让类型检查器和读者同时困惑。

        **异常约定**：registry 已冻结抛 `KERNEL_INVARIANT_VIOLATED`。
        """
        self._require_writable("并入登记")
        self._registrations.extend(registrations)

    def freeze(self, active: Sequence[Registration]) -> None:
        """以 `active` 为生效集合建索引并冻结。由 `resolution.resolve_into()` 调用。

        索引值是元组而不是单项：CONTEXT / HOOK 是 MULTI，同一槽位本就可以有多个实现，
        为它们单开一张表会让查找路径分叉成两条。

        **异常约定**：重复冻结抛 `KERNEL_INVARIANT_VIOLATED`。
        """
        self._require_writable("冻结注册表")
        ordered = tuple(sorted(active, key=lambda item: item.sort_key))
        index: dict[tuple[CapabilityKind, str], list[Registration]] = {}
        for registration in ordered:
            index.setdefault(registration.slot, []).append(registration)
        self._index = {slot: tuple(items) for slot, items in index.items()}
        self._active = ordered
        self._frozen = True

    def active(self) -> tuple[Registration, ...]:
        """生效集合，按 `(priority, provider, name)` 排序。

        **异常约定**：未冻结抛 `KERNEL_INVARIANT_VIOLATED`。
        """
        self._require_frozen("读取生效集合")
        return self._active

    def lookup(self, kind: CapabilityKind, name: str) -> Registration | None:
        """查一个唯一槽位，O(1)。不存在返回 `None`。

        **异常约定**：未冻结抛 `KERNEL_INVARIANT_VIOLATED`；对 MULTI 类 kind
        （CONTEXT / HOOK）调用抛同一个码——那两类天然可能有多个实现，用只取第一个的
        接口访问它们必然会静默丢掉其余实现，因此这里不给「顺手用一下」的机会。
        """
        self._require_frozen("查找能力")
        if kind.arity is CapabilityArity.MULTI:
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "MULTI 类能力可能有多个实现，应使用 lookup_all()。",
                detail={"kind": kind.value, "name": name, "arity": kind.arity.value},
            )
        found = self._index.get((kind, name))
        return found[0] if found else None

    def lookup_all(self, kind: CapabilityKind, name: str) -> tuple[Registration, ...]:
        """查一个槽位的全部实现，按 `(priority, provider, name)` 排序，O(1)。

        **异常约定**：未冻结抛 `KERNEL_INVARIANT_VIOLATED`。
        """
        self._require_frozen("查找能力")
        return self._index.get((kind, name), ())

    def of_kind(self, kind: CapabilityKind) -> tuple[Registration, ...]:
        """某个 kind 的全部生效实现，按 `(priority, provider, name)` 排序。

        遍历生效集合而不是查索引：这条路径只在启动装配与诊断输出上用（「列出全部工具」、
        `karyvia capabilities`），不在 turn 的热路径上，为它多维护一张按 kind 的表不划算。

        **异常约定**：未冻结抛 `KERNEL_INVARIANT_VIOLATED`。
        """
        self._require_frozen("按 kind 枚举能力")
        return tuple(item for item in self._active if item.ref.kind is kind)

    def __iter__(self) -> Iterator[Registration]:
        """迭代生效集合。**异常约定**：未冻结抛 `KERNEL_INVARIANT_VIOLATED`。"""
        return iter(self.active())

    def __len__(self) -> int:
        """生效集合的大小。**异常约定**：未冻结抛 `KERNEL_INVARIANT_VIOLATED`。"""
        return len(self.active())

    def _require_writable(self, action: str) -> None:
        if self._frozen:
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                f"注册表已冻结，无法{action}。",
                detail={"action": action, "active": len(self._active)},
            )

    def _require_frozen(self, action: str) -> None:
        if not self._frozen:
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                f"注册表尚未解析冻结，无法{action}。",
                detail={"action": action, "registered": len(self._registrations)},
            )
