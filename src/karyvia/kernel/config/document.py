"""把一份已校验的 `KaryviaConfig` 渲染成诊断用的 JSON 文档。

职责：`KaryviaConfig` → `dict[str, JsonValue]`，元组转列表，保证真能被 `json.dumps` 编码。
不负责：定义有哪些字段（`schema.SECTION_SPECS`）、校验（`fields.py` / `schema.py`）、
读取任何来源（`sources.py`）。

**它是字段表的派生视图，不是第二份真相来源**：`json_schema.py` 面向编辑器，本模块面向
`/config` 与 `karyvia config show`。渲染逻辑独立成模块，字段定义仍只属于 `SECTION_SPECS`。

加字段时**两处都要改**：`schema.SECTION_SPECS` 与这里的渲染。`tests/kernel/test_config.py`
有一条「渲染出来的键集合 == 字段表的键集合」的对照测试盯着这件事。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from karyvia.contracts import JsonValue

from . import plugin_blocks as blocks

if TYPE_CHECKING:  # pragma: no cover - 仅为注解；运行期反向 import 会成环。
    from .schema import KaryviaConfig

__all__ = ["config_to_json"]


def config_to_json(config: KaryviaConfig) -> dict[str, JsonValue]:
    """诊断视图。元组转列表，保证真能被 `json.dumps` 编码。"""
    return {
        "turn": {
            "max_iterations": config.turn.max_iterations,
            "max_tool_calls_per_turn": config.turn.max_tool_calls_per_turn,
            "tool_timeout_ms": config.turn.tool_timeout_ms,
            "tool_result_max_bytes": config.turn.tool_result_max_bytes,
            "turn_timeout_ms": config.turn.turn_timeout_ms,
            "context_max_tokens": config.turn.context_max_tokens,
        },
        "workspace": {"root": config.workspace.root},
        "routing": {
            "command_prefix": config.routing.command_prefix,
            "session_concurrency": config.routing.session_concurrency,
            "queue_max_size": config.routing.queue_max_size,
            "dedup_capacity": config.routing.dedup_capacity,
            "dedup_ttl_ms": config.routing.dedup_ttl_ms,
            "channel_concurrency": config.routing.channel_concurrency,
        },
        "hooks": {
            "observer_timeout_ms": config.hooks.observer_timeout_ms,
            "interceptor_timeout_ms": config.hooks.interceptor_timeout_ms,
        },
        "context": {
            "provider_timeout_ms": config.context.provider_timeout_ms,
            "turn_compactor": config.context.turn_compactor,
            "turn_compactor_timeout_ms": config.context.turn_compactor_timeout_ms,
        },
        "memory": {
            "provider": config.memory.provider,
            "recall_limit": config.memory.recall_limit,
            "recall_timeout_ms": config.memory.recall_timeout_ms,
            "fragment_priority": config.memory.fragment_priority,
            "on_failure": config.memory.on_failure,
        },
        "plugins": {
            "enabled": list(config.plugins.enabled),
            "disable": list(config.plugins.disable),
            "stop_timeout_ms": config.plugins.stop_timeout_ms,
            **blocks.entries_to_json(config.plugins.entries),
        },
        "model": {"provider": config.model.provider, "name": config.model.name},
        "logging": {"level": config.logging.level, "file_enabled": config.logging.file_enabled},
    }
