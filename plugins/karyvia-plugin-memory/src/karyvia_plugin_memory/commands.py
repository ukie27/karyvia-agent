"""`/memory` 命令：给**人**用的查询、检索与删除入口（`MEM-005`）。

职责：把一次 `/memory <子命令> ...` 变成一段文本回复。
不负责：存储（`store.py`）、给模型用的入口（`tools.py`）、自动召回（`provider.py`）。

**`MEM-005` 要的三件事在这里各有落点**：查询 = `list` / `show` / `search`，
删除 = `forget`，而「修正」**不单列子命令**——契约的 `MemoryProvider` 已经把这条定死了：
`forget()` + 重新写一条的组合语义明确，而原地修改会让「这条记忆是什么时候、由谁写的」
变得不可追溯。

**两条与权限有关的判定：**

- **命令整体 `operator_only=False`**：读自己的记忆不该要管理员。
- **但 `forget` 对 `agent` / `workspace` 分区额外校验 `sender.is_operator`**——那两个分区
  是全实例共享的，群聊里任何人都能删掉它们不合理。这不违反「`operator_only` 由 dispatcher
  前置校验、命令自己不要再抄一遍」：抄一遍指的是重复同一条判定，而这是一条更细的判定。

**`handle()` 约定不抛**，统一出口在 `MemoryCommand.handle`（`builtins/commands_core` 的
同一种做法）：`KaryviaError` 原样带出（实现方给的诊断比编出来的更准），其余异常折成
`KERNEL_INVARIANT_VIOLATED` 且**只放类型名不放异常消息**。捕 `Exception` 不捕
`BaseException`——取消与 Ctrl-C 要放行。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from karyvia.contracts import (
    CancelSignal,
    CommandInvocation,
    CommandParam,
    CommandResult,
    CommandSpec,
    ContextFragment,
    Disposition,
    ErrorCode,
    FragmentKind,
    FragmentScope,
    KaryviaError,
    SessionKey,
    TrustLevel,
)

from .partition import parse_record_id
from .record import MAX_CONTENT_CHARS, SOURCE, MemoryRecord, estimate_tokens
from .settings import MemorySettings
from .store import MemoryStore

__all__ = ["COMMAND_NAME", "SUBCOMMANDS", "MemoryCommand", "memory_spec"]

COMMAND_NAME: Final = "memory"

#: 子命令清单。它同时是 `/memory` 无参数时印出来的用法——两处各写一遍必然分叉。
SUBCOMMANDS: Final[tuple[tuple[str, str], ...]] = (
    ("list", "列出最近的记忆"),
    ("search", "按关键词检索：/memory search <关键词>"),
    ("show", "看一条的全文：/memory show <标识>"),
    ("add", "手动记一条：/memory add <内容>"),
    ("forget", "删除一条：/memory forget <标识>"),
)

#: 全实例共享的分区。删它们要管理员。
_SHARED_SCOPES: Final[frozenset[FragmentScope]] = frozenset(
    {FragmentScope.AGENT, FragmentScope.WORKSPACE}
)

_UNKNOWN_SUBCOMMAND: Final = "不认识这个子命令。"
_MISSING_ARGUMENT: Final = "这个子命令需要一个参数。"
_NOT_FOUND: Final = "没有这条记忆。"
_OPERATOR_ONLY_DELETE: Final = "删除 agent / workspace 范围的记忆需要管理员权限。"
_UNEXPECTED: Final = "命令执行时发生了未预期的错误。"


def memory_spec() -> CommandSpec:
    """`/memory` 的声明。

    尾参声明 `repeated=True`：`/memory search 深色 模式` 是完全正常的敲法，不声明的话
    dispatcher 会按「参数过多」把它拒掉。
    """
    return CommandSpec(
        name=COMMAND_NAME,
        description="查看、检索与删除长期记忆。不带参数时列出可用的子命令。",
        parameters=(
            CommandParam(
                name="subcommand",
                description=" / ".join(name for name, _ in SUBCOMMANDS),
                required=False,
            ),
            CommandParam(name="rest", description="子命令的参数。", required=False, repeated=True),
        ),
        operator_only=False,
        aliases=("mem",),
    )


class MemoryCommand:
    """`contracts.CommandHandler` 的实现。"""

    __slots__ = ("_settings", "_store")

    def __init__(self, store: MemoryStore, settings: MemorySettings) -> None:
        self._store = store
        self._settings = settings

    async def handle(
        self, invocation: CommandInvocation, cancel: CancelSignal
    ) -> CommandResult:
        """**约定不抛**（`CMD-003`）：一切失败折成 `REJECTED`，会话保持可用。

        **取消语义**：入口检查一次。每个子命令都是一次读盘或一次写盘，在这之后再插检查点
        只会得到一串必然为假的判断（`builtins/commands_core` 的同一条判定）。
        """
        try:
            cancel.raise_if_requested()
            return await self._dispatch(invocation)
        except KaryviaError as error:
            return CommandResult(disposition=Disposition.REJECTED, error=error)
        except Exception as error:  # noqa: BLE001 - 统一出口，见模块 docstring
            return CommandResult(
                disposition=Disposition.REJECTED,
                error=KaryviaError(
                    ErrorCode.KERNEL_INVARIANT_VIOLATED,
                    _UNEXPECTED,
                    # 只放类型名：第三方栈里的异常文本可能带着凭据或宿主机路径。
                    detail={"cause": type(error).__name__},
                ),
            )

    async def _dispatch(self, invocation: CommandInvocation) -> CommandResult:
        # `InboundMessage` 没有 `session_key`（它只有 channel_id + conversation_id，
        # 缺 scope），`Correlation` 才带着已经组装好的那一个——与工具侧同一个来源。
        key = invocation.correlation.session_key
        args = invocation.args
        if not args:
            return _handled(_usage())
        subcommand, rest = args[0].strip().lower(), args[1:]

        if subcommand == "list":
            return _handled(await self._list(key))
        if subcommand == "search":
            return _handled(await self._search(key, _joined(rest, "search")))
        if subcommand == "show":
            return _handled(await self._show(_single(rest, "show")))
        if subcommand == "add":
            return _handled(await self._add(key, _joined(rest, "add")))
        if subcommand == "forget":
            return _handled(await self._forget(invocation, _single(rest, "forget")))
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            _UNKNOWN_SUBCOMMAND,
            detail={"subcommand": subcommand[:64], "choices": [name for name, _ in SUBCOMMANDS]},
        )

    # ------------------------------------------------------------------ 子命令

    async def _list(self, key: SessionKey) -> str:
        records = await self._store.entries(key, scopes=self._settings.enabled_scopes)
        shown = records[: self._settings.list_limit]
        if not shown:
            return "还没有任何长期记忆。"
        header = f"共 {len(records)} 条记忆，最近 {len(shown)} 条："
        return "\n".join([header, *(_line(record) for record in shown)])

    async def _search(self, key: SessionKey, query: str) -> str:
        hits = await self._store.search(
            key,
            query,
            scopes=self._settings.enabled_scopes,
            limit=self._settings.list_limit,
            min_score=self._settings.min_score,
        )
        if not hits:
            return f"没有找到与「{query}」相关的记忆。"
        return "\n".join(
            [f"与「{query}」相关的 {len(hits)} 条记忆：", *(_line(hit.record) for hit in hits)]
        )

    async def _show(self, value: str) -> str:
        record = await self._store.get(value)
        if record is None:
            raise KaryviaError(ErrorCode.INPUT_MALFORMED, _NOT_FOUND, detail={"record_id": value[:200]})
        lines = [
            f"标识：{record.record_id}",
            f"范围：{record.scope.value}",
            f"写入：{record.created_at.isoformat()}" + (f"（来自 {record.origin}）" if record.origin else ""),
        ]
        if record.expires_at is not None:
            lines.append(f"过期：{record.expires_at.isoformat()}")
        if record.tags:
            lines.append(f"标签：{', '.join(record.tags)}")
        lines.extend(("", record.content))
        return "\n".join(lines)

    async def _add(self, key: SessionKey, content: str) -> str:
        # 走与工具完全相同的那条写入路径：`trust` 被统一成 `UNTRUSTED`，敲命令的人不会
        # 因此获得指令优先级（`record.py` 的模块 docstring）。
        if len(content) > MAX_CONTENT_CHARS:
            raise KaryviaError(
                ErrorCode.INPUT_TOO_LARGE,
                _TOO_LONG,
                detail={"length": len(content), "limit": MAX_CONTENT_CHARS},
            )
        fragment = ContextFragment(
            source=SOURCE,
            kind=FragmentKind.MEMORY,
            content=content,
            priority=self._settings.fragment_priority,
            estimated_tokens=estimate_tokens(content),
            scope=FragmentScope.AGENT,
            trust=TrustLevel.UNTRUSTED,
        )
        stored = await self._store.add(key, fragment, origin="command")
        return f"已记住（agent，标识 {stored}）。"

    async def _forget(self, invocation: CommandInvocation, value: str) -> str:
        partition, _ = parse_record_id(value)
        if partition.scope in _SHARED_SCOPES and not invocation.message.sender.is_operator:
            raise KaryviaError(
                ErrorCode.PERMISSION_DENIED,
                _OPERATOR_ONLY_DELETE,
                detail={"scope": partition.scope.value},
            )
        existed = await self._store.remove(value)
        return "已删除这条记忆。" if existed else _NOT_FOUND


# ------------------------------------------------------------------------------ 工具函数


def _handled(content: str) -> CommandResult:
    return CommandResult(disposition=Disposition.COMMAND_HANDLED, content=content)


def _usage() -> str:
    return "\n".join(
        ["/memory 的子命令：", *(f"  {name:<7}{description}" for name, description in SUBCOMMANDS)]
    )


def _line(record: MemoryRecord) -> str:
    """一条记忆的单行摘要。内容截断到一行能读完的长度，全文用 `/memory show`。"""
    content = record.content.replace("\n", " ")
    if len(content) > _SUMMARY_CHARS:
        content = content[:_SUMMARY_CHARS] + "…"
    return f"- [{record.scope.value}] {content}（{record.record_id}）"


def _single(rest: Sequence[str], subcommand: str) -> str:
    if len(rest) != 1 or not rest[0].strip():
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED, _MISSING_ARGUMENT, detail={"subcommand": subcommand}
        )
    return rest[0].strip()


def _joined(rest: Sequence[str], subcommand: str) -> str:
    """把尾参拼回一句话。

    dispatcher 按空白切分参数，而 `/memory search 深色 模式` 的意图是一个短语。
    拼回去用单个空格：原始空白（制表符、连续空格）在检索与记忆内容里都没有意义。
    """
    joined = " ".join(part for part in rest if part.strip()).strip()
    if not joined:
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED, _MISSING_ARGUMENT, detail={"subcommand": subcommand}
        )
    return joined


_SUMMARY_CHARS: Final = 80
_TOO_LONG: Final = "记忆内容超过单条上限，请先自行摘要。"
