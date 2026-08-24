"""示例插件 `session-memory`：用一个纯内存实现**覆盖**内建会话存储（开发方案 `D30`）。

职责：声明覆盖 `builtin:jsonl` 的 `SESSION_STORE` 能力，并给出一个完整的 `SessionStore`
实现（五个方法俱全）。
不负责：持久化——这正是它的用途。进程退出即忘，因此适合一次性容器、演示与
「不要在磁盘上留下对话」的场景。

**为什么用它来演示覆盖**：`SESSION_STORE` 的 arity 是 SINGLETON——全实例只有一个生效实现，
替换**必须**在 manifest 里显式声明 `overrides`（`EDG-102`：覆盖永不由加载顺序决定）。
装上一个插件就悄悄换掉用户的会话历史后端，是这套设计明确要堵的路。

把 `session-memory` 写进 `plugins.disable` 后，Runtime 不加载本插件，它的覆盖声明也不
参与本次启动；未被单独禁用的内建 JSONL 存储正常生效。

**只 import `nucleamind.contracts` 与 `nucleamind.sdk`**（依赖规则 `R4`）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Final

from nucleamind.contracts import (
    CapabilityKind,
    ErrorCode,
    NucleaError,
    SessionKey,
    SessionMessage,
    SessionSnapshot,
)
from nucleamind.sdk import CapabilityDecl, NucleaAPI, PluginManifest

__all__ = ["CAPABILITY_NAME", "MANIFEST", "OVERRIDE_TARGET", "MemorySessionStore", "setup"]

#: 本插件提供的会话存储名。
CAPABILITY_NAME: Final = "memory"

#: 覆盖目标串。`builtin:<name>` 是覆盖内建能力的写法；覆盖另一个插件写
#: `plugin:<id>:<name>`。串里**不带 kind**——kind 取自声明覆盖的这一方（技术方案 §7.2）。
OVERRIDE_TARGET: Final = "builtin:jsonl"

MANIFEST: Final = PluginManifest(
    id="session-memory",
    version="0.1.0",
    sdk_range=">=4.0.0,<5.0.0",
    setup="nucleamind_plugin_session_memory:setup",
    capabilities=(
        CapabilityDecl(
            kind=CapabilityKind.SESSION_STORE,
            name=CAPABILITY_NAME,
            overrides=OVERRIDE_TARGET,
        ),
    ),
    # 从不落盘正是它的卖点；状态只存在于当前进程。
    config_schema={"type": "object", "properties": {}, "additionalProperties": False},
)


class MemorySessionStore:
    """`SessionStore` 的内存实现。

    **键用 `storage_id()` 而不是 `SessionKey` 本身**（`SessionKey` 可哈希，看上去更直接）：
    `storage_id()` 是已发布的持久化契约，让内存实现也走一遍，任何破坏「不同 key 不撞同一个
    id」的改动在这里就会被撞出来，而不是等到某个真的落盘的实现上线（`EDG-203`）。

    单写者由 Kernel 的 session 锁保证（`KER-008`），因此这里不加锁；但「整批原子生效」
    （`SES-002`）仍要自己兑现——下面的 `append()` 一次性 `extend`，不存在半批状态。
    """

    __slots__ = ("_compacted", "_created", "_keys", "_messages", "_updated")

    def __init__(self) -> None:
        self._messages: dict[str, list[SessionMessage]] = {}
        self._keys: dict[str, SessionKey] = {}
        self._compacted: dict[str, int] = {}
        self._created: dict[str, datetime] = {}
        self._updated: dict[str, datetime] = {}

    async def load(self, key: SessionKey) -> SessionSnapshot:
        """不存在时返回**空快照而不是抛异常**——「第一次说话」不是错误。"""
        storage_id = key.storage_id()
        if storage_id not in self._messages:
            return SessionSnapshot(session_key=key)
        return SessionSnapshot(
            session_key=key,
            messages=tuple(self._messages[storage_id]),
            created_at=self._created[storage_id],
            updated_at=self._updated[storage_id],
            compacted_through=self._compacted[storage_id],
        )

    async def append(self, key: SessionKey, messages: Sequence[SessionMessage]) -> None:
        storage_id = key.storage_id()
        now = datetime.now(UTC)
        if storage_id not in self._messages:
            self._messages[storage_id] = []
            self._keys[storage_id] = key
            self._compacted[storage_id] = 0
            self._created[storage_id] = now
        self._messages[storage_id].extend(messages)
        self._updated[storage_id] = now

    async def compact(self, key: SessionKey, through: int, summary: SessionMessage) -> None:
        """摘要**插在水位处**而不是替换掉前缀。

        契约要求 `load()` 之后 `compacted_through == through`，同时摘要必须出现在快照里。
        原地插入同时满足两者；原始记录是否物理删除由实现自行决定，这里留着——内存实现
        删不删都不省磁盘，而留着让「压缩到哪里」可比对。
        """
        storage_id = key.storage_id()
        existing = self._messages.get(storage_id, [])
        if not 0 <= through <= len(existing):
            raise NucleaError(
                ErrorCode.INPUT_MALFORMED,
                "压缩水位超出会话记录范围。",
                detail={"through": through, "messages": len(existing)},
            )
        self._messages[storage_id] = [*existing[:through], summary, *existing[through:]]
        self._compacted[storage_id] = through
        self._updated[storage_id] = datetime.now(UTC)

    async def delete(self, key: SessionKey) -> bool:
        """返回**是否真的存在过**。删一个不存在的会话不是错误，但也不是「删掉了」。"""
        storage_id = key.storage_id()
        existed = storage_id in self._messages
        for table in (self._messages, self._keys, self._compacted, self._created, self._updated):
            table.pop(storage_id, None)
        return existed

    async def list_keys(self) -> tuple[SessionKey, ...]:
        return tuple(self._keys[storage_id] for storage_id in sorted(self._keys))


def setup(api: NucleaAPI) -> None:
    """注册入口。

    注册的名字是 `CAPABILITY_NAME`，而**覆盖谁**由 manifest 的 `overrides` 决定——两件事
    分开是刻意的：注册面不认识覆盖，覆盖只从声明来（`EDG-102`）。
    """
    api.register_session_store(CAPABILITY_NAME, MemorySessionStore())
