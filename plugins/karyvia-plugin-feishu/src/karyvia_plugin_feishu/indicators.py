"""反应 emoji 当「收到了」指示器用（开发方案 `D34`）。

职责：入站时打一个反应、终态时移除它、可选再打一个「完成」反应；把 `message_id` 与
反应 id 的对应关系记在一张有界表里。
不负责：判断什么是终态（由 `channel.py` 传进来）、发消息（`stream.py`）、接触 SDK
（对注入的 `Reactions` Protocol 编程）。

**飞书没有 typing API**，所以这里只用 emoji，不维护 typing 后台循环，也不需要注入时钟。

**移除反应需要 `reaction_id`**（不是 emoji 名），因此必须把 `add` 的返回值记下来。
表是有界的（`OrderedDict`，上限 512）：一个跑几个月的实例不该把每条消息的反应 id 都留着。

**每一个平台调用都可能失败，而失败一次都不该影响 turn**：消息被删、权限不足、频道归档
都会让反应失败——那是装饰不是内容。因此本模块**全部动作都吞异常**。
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Coroutine
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

__all__ = ["REACTION_TABLE_CAPACITY", "Indicators", "Reactions"]

#: `message_id → reaction_id` 表的容量。见模块 docstring。
REACTION_TABLE_CAPACITY: Final = 512


class Reactions(Protocol):
    """本模块需要的全部平台能力。`client.py` 提供实现。"""

    async def add_reaction(self, message_id: str, emoji: str) -> str | None:
        """打一个反应，返回 `reaction_id`；失败返回 `None`。"""
        ...

    async def remove_reaction(self, message_id: str, reaction_id: str) -> None: ...


@dataclass(slots=True)
class Indicators:
    """按 conversation 维护「收到了」指示器。`start()` / `stop()` 各调用一次。"""

    reactions: Reactions
    react_emoji: str = "THUMBSUP"
    done_emoji: str = ""
    #: `conversation_id` → 这一轮打反应的那条消息。
    _live: dict[str, str] = field(default_factory=dict)
    #: `message_id` → `reaction_id`，有界。
    _ids: OrderedDict[str, str] = field(default_factory=OrderedDict)

    def active(self) -> int:
        """当前有指示器的 conversation 数。"""
        return len(self._live)

    async def start(self, conversation_id: str, message_id: str) -> None:
        """收到一条消息：打上「收到了」反应。

        同一 conversation 再次 `start()` 会先把上一次清干净——否则一条接一条时会在会话里
        堆出一串没人清理的反应。
        """
        await self.stop(conversation_id)
        if not self.react_emoji:
            return
        self._live[conversation_id] = message_id
        reaction_id = await self._quietly(self.reactions.add_reaction(message_id, self.react_emoji))
        if isinstance(reaction_id, str) and reaction_id:
            self._remember(message_id, reaction_id)

    async def stop(self, conversation_id: str) -> None:
        """终态：移除反应，可选补一个「完成」反应。**只在终态调用。**

        一条 `DELTA` 调它会让指示器在模型说话的中途闪一下又回来——判断留给调用方
        （`channel.py` 的 `_TERMINAL`），本模块不猜什么算「说完了」。
        """
        message_id = self._live.pop(conversation_id, None)
        if message_id is None:
            return
        reaction_id = self._ids.pop(message_id, None)
        if reaction_id is not None:
            await self._quietly(self.reactions.remove_reaction(message_id, reaction_id))
        if self.done_emoji:
            await self._quietly(self.reactions.add_reaction(message_id, self.done_emoji))

    async def shutdown(self) -> None:
        """把所有 conversation 的指示器清干净。Channel 停止时调用一次。"""
        for conversation_id in list(self._live):
            await self.stop(conversation_id)

    # ------------------------------------------------------------------ 内部

    def _remember(self, message_id: str, reaction_id: str) -> None:
        self._ids[message_id] = reaction_id
        while len(self._ids) > REACTION_TABLE_CAPACITY:
            self._ids.popitem(last=False)

    @staticmethod
    # boundary: 平台调用的返回类型随方法而异，这里只负责跑它并吞异常
    async def _quietly(awaitable: Coroutine[Any, Any, Any]) -> Any:
        """跑一个平台动作，失败只吞不抛。见模块 docstring：反应是装饰不是内容。"""
        with suppress(Exception):
            return await awaitable
        return None
