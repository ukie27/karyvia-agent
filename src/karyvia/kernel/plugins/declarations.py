"""注册意图的 kernel 侧投影：`CapabilityDeclaration` 与 `LoadRequest`。

职责：以契约层认识的类型描述「一个插件声明了哪些能力、从哪导入 `setup`、依赖谁」，
作为 Host 与 loader 的输入。
不负责：解析或校验 manifest（那是 `sdk/manifest.py`，且 `R2` 禁止 `kernel/` import
`sdk/`）、导入实现、判定谁生效。本模块是纯数据，无 IO。

**为什么不直接用 `sdk.PluginManifest`**：`R2` 禁止 `kernel/` import `sdk/`。因此由
`runtime/`（唯一组装根）把 manifest 翻译成这里的形状——这与  定下的
「`overrides` 以原始串跨层传递、两侧共用 `contracts.parse_capability_target()`」是同一条
思路，kernel 侧不出现第二套 manifest 校验。内建与外部插件因此共用同一条注册路径
，差异只在「谁来产出 `LoadRequest`」。

**`priority: int | None` 是本模块最要紧的一个字段**，也是与 `sdk.CapabilityDecl` 唯一的
实质差别。后者的默认值是 `100`，照搬过来会让每一项内建能力都拿到 100，
而 §6.1 规则 1 定的内建基准是 **0**——这两条方案彼此打架， 的结论是：
**`None` 表示「声明里没写」**，交给 `RegistrationBatch.add(priority=None)` 落到
`base_priority_for()`（内建 0 / 插件 100）。翻译方（`runtime/wiring.py`）用 pydantic 的
`model_fields_set` 判断作者是否真的写过这个字段。若不这么做，§10.2 的裁剪顺序
（「其余按 priority 逆序丢弃」）就会失去「内建最后被裁」这个前提。
"""

from __future__ import annotations

from dataclasses import dataclass

from karyvia.contracts import (
    Builtin,
    CapabilityKind,
    CapabilityRef,
    ErrorCode,
    KaryviaError,
    ProviderId,
    parse_capability_target,
)

__all__ = ["CapabilityDeclaration", "LoadRequest"]


@dataclass(frozen=True, slots=True)
class CapabilityDeclaration:
    """manifest 里一项能力声明的 kernel 投影。

    `overrides` 保持**原始串**（`"builtin:fs.read"` / `"plugin:<id>:<name>"`），与
    `Registration.overrides` 同形：解码只用 `contracts.parse_capability_target()` 一处
    实现，两侧不会出现两套对不上的正则。

    **`namespace=True` 时 `name` 是前缀而不是能力名**：本条声明放行提供方注册
    任意多条 `<name>.<后缀>` 的能力，`finish()` 也不要求它至少注册一条。它是为
    「能力名要连上外部服务才知道」这类插件开的（MCP 的远端工具名）。约束的判定在
    `sdk/manifest.py`——`R2` 禁止 kernel 认识 manifest，本层只按标志位分派。
    """

    kind: CapabilityKind
    name: str
    overrides: str | None = None
    priority: int | None = None
    namespace: bool = False

    def __post_init__(self) -> None:
        # 名字的形状借 `CapabilityRef` 校验——与 manifest 侧同一份实现（`sdk/manifest.py`
        # 也是这么做的），因此「manifest 通过了但 kernel 拒绝」不可能发生。
        CapabilityRef(kind=self.kind, name=self.name, provider=Builtin())
        if self.overrides is not None:
            parse_capability_target(self.overrides)
        if self.priority is not None and self.priority < 0:
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "能力优先级不得为负。",
                detail={"capability": f"{self.kind.value}:{self.name}", "priority": self.priority},
            )

    @property
    def slot(self) -> tuple[CapabilityKind, str]:
        """`(kind, name)`，Host 回查声明表用的键。与 `sdk.CapabilityDecl.slot` 同形。"""
        return (self.kind, self.name)

    def covers(self, kind: CapabilityKind, name: str) -> bool:
        """本条**命名空间**声明是否放行 `(kind, name)` 这次注册。

        判据是 `<前缀>.` 开头，因此 `mcp` 放行 `mcp.fs.read` 但**不**放行 `mcpx.read`：
        前缀比较必须落在分隔符边界上，否则一个更长的名字会被误判成后代
        （`builtins/tools_fs/paths.py` 的路径前缀比较是同一条道理）。
        非命名空间声明恒为假——精确匹配走 `slot`。
        """
        return self.namespace and kind is self.kind and name.startswith(f"{self.name}.")


@dataclass(frozen=True, slots=True)
class LoadRequest:
    """一个插件的一次加载请求：逻辑身份、提供方、入口、依赖与声明。

    `plugin_id` 与 `dependencies` 是注册阶段 必须保留的最小拓扑信息。加载前校验 的排序只能保证
    依赖先执行；若依赖的 ``setup()`` 随后失败，loader 仍需据此跳过依赖者。不能从
    ``provider`` 反推逻辑身份，因为全部内建插件共享一个 :class:`Builtin` 提供方身份。
    """

    plugin_id: str
    provider: ProviderId
    setup: str
    declarations: tuple[CapabilityDeclaration, ...]
    dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        seen: set[tuple[CapabilityKind, str]] = set()
        for declaration in self.declarations:
            if declaration.slot in seen:
                raise KaryviaError(
                    ErrorCode.KERNEL_INVARIANT_VIOLATED,
                    "同一提供方重复声明了同一能力。",
                    detail={
                        "provider": str(self.provider),
                        "capability": f"{declaration.kind.value}:{declaration.name}",
                    },
                )
            seen.add(declaration.slot)
