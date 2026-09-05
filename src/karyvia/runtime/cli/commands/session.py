"""`karyvia session`：列出会话与打印单个会话的摘要（需求 §16.1）。

职责：把生效的 `SessionStore`（可能被插件覆盖）里的会话列出来，或读一个会话的快照。
不负责：删除或压缩会话（那是有副作用的操作，要单独的确认流程）、装配完整实例。

**只装会话存储那一条能力**（`inspect.open_session_store`）：一条只读诊断不该因为模型
凭据没导出而失败，也不该跟正在跑的实例抢实例锁。
"""

from __future__ import annotations

import asyncio
import sys

from karyvia.contracts import ErrorCode, KaryviaError, SessionKey, SessionStore

from ...inspect import open_session_store
from ..main import Options

__all__ = ["session_command"]

_USAGE = """用法：karyvia session <list|show <会话 id>>

会话 id 即 SessionKey.storage_id()，形如 cli~local~default；`list` 会打印它。
"""


def session_command(options: Options) -> int:
    return asyncio.run(_session(options))


async def _session(options: Options) -> int:
    action = options.rest[0] if options.rest else ""
    if action in ("", "-h", "--help"):
        sys.stdout.write(_USAGE)
        return 0
    if action not in ("list", "show"):
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            f"未知的 session 子命令 {action!r}。",
            detail={"known": ["list", "show"]},
        )

    async with open_session_store(
        instance=options.instance, instance_dir=options.instance_dir
    ) as (_, sessions):
        if action == "list":
            return await _list(sessions)
        if len(options.rest) < 2:
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                "session show 需要一个会话 id。",
                detail={"usage": _USAGE},
            )
        return await _show(sessions, options.rest[1])


async def _list(sessions: SessionStore) -> int:
    keys = await sessions.list_keys()
    if not keys:
        sys.stdout.write("当前没有会话。\n")
        return 0
    for key in sorted(keys, key=lambda item: item.storage_id()):
        sys.stdout.write(
            f"{key.storage_id()}\t渠道={key.channel_id} 会话={key.conversation_id} "
            f"范围={key.scope}\n"
        )
    return 0


async def _show(sessions: SessionStore, storage_id: str) -> int:
    """打印一个会话的摘要。

    **`from_storage_id()` 是 `storage_id()` 的逆运算**（`D02` 的持久化契约），因此这里
    不自己拆字符串——那个编码里 `~` 是转义过的分隔符，手拆迟早在带 `~` 的会话上切错。
    """
    key = SessionKey.from_storage_id(storage_id)
    snapshot = await sessions.load(key)
    sys.stdout.write(f"会话：{storage_id}\n")
    sys.stdout.write(f"  记录数：{len(snapshot.messages)}（生效 {len(snapshot.live_messages)}）\n")
    sys.stdout.write(f"  压缩水位：{snapshot.compacted_through}\n")
    sys.stdout.write(f"  schema 版本：{snapshot.schema_version}\n")
    if snapshot.updated_at is not None:
        sys.stdout.write(f"  最后更新：{snapshot.updated_at.isoformat()}\n")
    return 0
