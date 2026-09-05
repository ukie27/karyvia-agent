"""自动召回：每轮 turn 把相关记忆变成上下文片段。注册为 `CONTEXT_PROVIDER:memory`。

职责：从会话快照里取出查询词，检索记忆，交出片段。
不负责：读写文件（`store.py`）、打分（`scoring.py`）、决定片段怎么进模型消息
（`kernel/turn/context_builder.py`）。

**这是记忆进到模型上下文的默认路径。** `D44` 给了 kernel 第二条：装配根按 `memory.provider`
挑一条 `MEMORY` 能力交给组装器（`kernel/turn/memory.py`），但那个键**默认不写**，因此默认
配置下这条 Context Provider 仍是唯一的入口。**两边同时开会让 `agent` 范围的记忆在一轮里
出现两次**——处置写在本包 `__init__.py` 的边界一节里。

**三条判定写在这里，因为它们都是「拿什么当查询」的问题：**

- **查询词取快照里最后一条 `role=USER` 的正文。** 用整段历史当查询会让每一轮都召回同一批
  「历史上出现最多」的记忆；用最后一条 assistant 正文则是拿模型自己说过的话去检索自己的
  记忆，会正反馈。
- **没有用户消息就贡献空元组**，那不是错误（契约原文）。刚开的会话、纯命令的 turn 都会
  走到这里。
- **`auto_recall=false` 时仍然注册、每轮返回空元组。** 外部插件用不上
  `runtime/bootstrap.py` 的 `keep` 声明过滤（`_ENABLED_NAMES` 按内建 id 索引），
  声明了几条能力就必须注册几条——「无贡献」与「未注册」在这里是两件事。
"""

from __future__ import annotations

from typing import Final

from karyvia.contracts import (
    CancelSignal,
    ContextFragment,
    Correlation,
    Role,
    SessionSnapshot,
)

from .record import to_fragment
from .settings import MemorySettings
from .store import MemoryStore

__all__ = ["PROVIDER_NAME", "MemoryContextProvider", "query_from"]

PROVIDER_NAME: Final = "memory"

#: 查询词的字符上限。一条几千字的粘贴内容当查询没有意义——命中的词太多，打分退化成
#: 「谁更长谁赢」，而 `scoring.py` 的长度归一压的是候选那一侧、压不住查询这一侧。
_MAX_QUERY_CHARS: Final = 1_000


def query_from(snapshot: SessionSnapshot) -> str:
    """从快照里取查询词：最后一条用户消息的正文，截断到 `_MAX_QUERY_CHARS`。

    **从后往前找而不是过滤再取最后一个**：一条长会话的消息元组可能有几千条，而我们要的
    那条通常就在末尾。
    """
    for message in reversed(snapshot.messages):
        if message.role is Role.USER and message.content.strip():
            return message.content.strip()[:_MAX_QUERY_CHARS]
    return ""


class MemoryContextProvider:
    """`contracts.ContextProvider` 的实现。"""

    __slots__ = ("_settings", "_store")

    def __init__(self, store: MemoryStore, settings: MemorySettings) -> None:
        self._store = store
        self._settings = settings

    async def provide(
        self,
        snapshot: SessionSnapshot,
        correlation: Correlation,
        cancel: CancelSignal,
    ) -> tuple[ContextFragment, ...]:
        """贡献片段。

        **异常约定**：可以抛 `KaryviaError`。读盘故障只会让这一次贡献被跳过并记录
        （`CTX-005`），不会让 turn 失败——那正是 `MEM-003`
        「Memory 不可用时降级为无长期记忆模式」的落地形态，**不需要**在这里 try/except
        把故障吞成空结果。吞掉它会让「记忆一直召不回来」查不出原因。
        **取消语义**：检索前检查一次（`store.search` 内部做），被取消时抛 `CANCELLED` 类。
        """
        del correlation  # 片段不带关联标识：整个 turn 只有一个，复制一份就有两个真相来源。
        settings = self._settings
        if not settings.auto_recall:
            return ()
        query = query_from(snapshot)
        if not query:
            return ()

        hits = await self._store.search(
            snapshot.session_key,
            query,
            scopes=settings.enabled_scopes,
            limit=settings.recall_limit,
            min_score=settings.min_score,
            cancel=cancel,
        )
        # **一条记录一个片段**，`priority = 基准 + 名次`：相关性最低的那条最先被组装器裁掉，
        # 而 `dropped` 的记账也精确到条。拼成一大块就只能整块留或整块丢。
        return tuple(
            to_fragment(hit.record, priority=settings.fragment_priority + rank)
            for rank, hit in enumerate(hits)
        )
