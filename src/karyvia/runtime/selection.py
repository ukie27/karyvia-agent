"""§10.1 步骤 8：从已冻结的 registry 里按配置挑出必需能力。

职责：模型供应商与模型标识、会话存储、长期记忆召回与 Turn 压缩策略的能力选择：配置指名一个、
registry 里找它，找不到就以稳定错误码拒绝启动。
不负责：注册能力（`wiring.py`）、解析覆盖（`kernel/registry/`）、装 `OrchestratorDeps`
（`bootstrap.py::_assemble`）、只读诊断（`inspect.py`）。

模型、会话存储、记忆和 Turn 压缩策略的选择集中在这里，避免装配流程与只读诊断各自形成一套
“配置如何指向能力”的判定。

**它们都不发事件、不写盘**：只读 registry 与配置，要么返回实现体，要么抛
`CAPABILITY_MISSING` / `CONFIG_INVALID`。`inspect.py` 的只读查询因此可以跳过整个本模块
（它刻意不做步骤 8 的必需能力判定）。
"""

from __future__ import annotations

from karyvia.contracts import ErrorCode, KaryviaError, ModelInfo, ModelProvider, SessionStore
from karyvia.kernel.config import KaryviaConfig
from karyvia.kernel.plugins import (
    memory_providers_from,
    model_providers_from,
    session_store_from,
    turn_context_compactors_from,
)
from karyvia.kernel.registry import CapabilityRegistry
from karyvia.kernel.turn import MemoryRecall, select_memory
from karyvia.kernel.turn.turn_compaction import TurnCompactionPolicy

__all__ = [
    "missing_capability",
    "require_sessions",
    "select_model",
    "select_recall",
    "select_turn_compactor",
]


def select_model(
    registry: CapabilityRegistry, config: KaryviaConfig
) -> tuple[ModelProvider, str, ModelInfo | None]:
    """§10.1 步骤 8 的 MODEL 一项：选出生效的 provider 与模型标识。"""
    bindings = model_providers_from(registry)
    if not bindings:
        raise missing_capability("MODEL", "没有任何模型供应商，实例无法回答任何输入。")
    wanted = config.model.provider
    chosen = next((b for b in bindings if b.name == wanted), None) if wanted else bindings[0]
    if chosen is None:
        raise KaryviaError(
            ErrorCode.CAPABILITY_MISSING,
            "配置里指定的模型供应商没有注册。",
            detail={
                "pointer": "/model/provider",
                "wanted": wanted,
                "available": [b.name for b in bindings],
            },
        )
    model_id = config.model.name
    if not model_id:
        raise KaryviaError(
            ErrorCode.CONFIG_INVALID,
            "没有指定要用哪个模型。",
            detail={
                "pointer": "/model/name",
                "suggestion": '在 config.json 里写 {"model": {"name": "gpt-4o-mini"}}。',
            },
        )
    return chosen.value, model_id, chosen.value.describe(model_id)


def select_recall(registry: CapabilityRegistry, config: KaryviaConfig) -> MemoryRecall | None:
    """按 `memory.provider` 挑一条 `MEMORY` 能力，装成 `MemoryRecall`。

    **`None`（没配）就是不启用**，这是默认。自动挑一个会让「装上一个记忆插件」悄悄改变
    每一轮请求的内容；配了却不存在是 `CAPABILITY_MISSING`（判定在
    `kernel/turn/memory.py::select_memory`，这里不重写一遍）。

    Runtime 只在这里把注册表中的 Memory 提供方接入 Turn；Kernel 不认识具体实现。
    """
    if config.memory.provider is None:
        return None
    candidates = [
        (binding.name, binding.owner, binding.value)
        for binding in memory_providers_from(registry)
    ]
    name, owner, provider = select_memory(candidates, config.memory.provider)
    return MemoryRecall(
        provider=provider,
        name=name,
        owner=owner,
        limit=config.memory.recall_limit,
        timeout_ms=config.memory.recall_timeout_ms,
        priority_floor=config.memory.fragment_priority,
        critical=config.memory.critical,
    )


def select_turn_compactor(
    registry: CapabilityRegistry, config: KaryviaConfig
) -> TurnCompactionPolicy:
    """选择必需的 Turn 内压缩策略；不存在时拒绝启动，不做静默回落。"""
    wanted = config.context.turn_compactor
    bindings = turn_context_compactors_from(registry)
    if not bindings:
        raise missing_capability(
            "TURN_COMPACTOR",
            "没有 Turn Context Compactor，模型—工具迭代无法安全控制请求大小。",
        )
    chosen = next((binding for binding in bindings if binding.name == wanted), None)
    if chosen is None:
        raise KaryviaError(
            ErrorCode.CAPABILITY_MISSING,
            "配置里指定的 Turn Context Compactor 没有注册。",
            detail={
                "pointer": "/context/turn_compactor",
                "wanted": wanted,
                "available": [binding.name for binding in bindings],
            },
        )
    return TurnCompactionPolicy(
        compactor=chosen.value,
        name=chosen.name,
        owner=chosen.owner,
        timeout_ms=config.context.turn_compactor_timeout_ms,
    )


def require_sessions(registry: CapabilityRegistry) -> SessionStore:
    binding = session_store_from(registry)
    if binding is None:
        raise missing_capability("SESSION_STORE", "没有会话存储，历史无处可写。")
    return binding.value


def missing_capability(kind: str, why: str) -> KaryviaError:
    """必需能力缺失的统一形状。`bootstrap.py` 的 CLI 入口那一条也用它——
    四处各拼一遍消息会让「检查什么」的建议逐渐长得不一样。"""
    return KaryviaError(
        ErrorCode.CAPABILITY_MISSING,
        f"必需能力缺失：{kind}。{why}",
        detail={"kind": kind, "suggestion": "检查 plugins.disable 与插件加载结果（karyvia 会打印）。"},
    )
