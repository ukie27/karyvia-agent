"""分区：`FragmentScope` × `SessionKey` -> 一个存储分区。**唯一的映射点。**

职责：决定一条记忆存到哪个分区、一次召回该查哪几个分区，以及记录 id 与分区之间的编解码。
不负责：任何 IO（`store.py`）、打分（`scoring.py`）、片段的形状（`record.py`）。

**本模块的全部难点是「召回路径拿得到哪些身份」，而答案比直觉窄得多。**
`ContextProvider.provide(snapshot, correlation, cancel)` 只拿得到 `SessionSnapshot`，
而 `SessionMessage` 一个发送者字段都没有（`contracts/session.py`）；工具那一侧同样只有
`ToolInvocation.correlation.session_key`。因此四个 scope 这样落地：

| scope | 分区 | 依据 |
| --- | --- | --- |
| `SESSION` | `session/<SessionKey.storage_id()>` | 复用已发布的编码契约 |
| `WORKSPACE` | `workspace/<SessionKey.scope>` | `scope` 的契约定义就是「项目/工作区维度」 |
| `AGENT` | `agent` | 实例级单份 |
| `USER` | **拒绝** | 召回路径拿不到用户身份 |

`USER` 写入抛 `INPUT_MALFORMED` 而不是折成「按 conversation 存」：后者会让群聊里 A 的
用户记忆被召回给 B——那是真实的隐私泄漏，静默降级比报错危险得多（`AGENTS.md` 原则 7）。
要支持它，得先让发送者身份到得了召回路径，那是一次契约变更。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from karyvia.contracts import ErrorCode, FragmentScope, KaryviaError, SessionKey

__all__ = [
    "AGENT_PARTITION",
    "RECALL_ORDER",
    "RECORD_ID_SEPARATOR",
    "Partition",
    "partition_for",
    "parse_record_id",
    "partitions_for",
    "record_id",
]

#: 实例级分区的固定 token。它不含 `RECORD_ID_SEPARATOR`，与另两类同样如此。
AGENT_PARTITION: Final = "agent"

#: 记录 id 的分隔符。`#` **不在** `SessionKey.storage_id()` 的安全字符集
#: （`[A-Za-z0-9._-]` + `~`）里，也不在 `validate_identifier` 允许出现在 scope 里的
#: 常见字符里已被编码的那一批之外——因此 `partition#seq` 的切分不可能切错位置。
#: 这与 `storage_id()` 用 `~` 做分隔符是同一条推理。
RECORD_ID_SEPARATOR: Final = "#"

#: 自动召回时查询的分区顺序：**由窄到宽**。顺序本身不决定排序（排序在 `scoring.py`），
#: 它只决定同分之下谁先出现——会话级的记忆比实例级的更贴近眼前这段对话。
RECALL_ORDER: Final[tuple[FragmentScope, ...]] = (
    FragmentScope.SESSION,
    FragmentScope.WORKSPACE,
    FragmentScope.AGENT,
)

_UNSUPPORTED_SCOPE: Final = (
    "user 范围的记忆暂不支持：召回路径拿不到发送者身份，"
    "按会话存会让群聊里其他人读到它。请改用 session / workspace / agent。"
)
_MALFORMED_RECORD_ID: Final = "记忆记录标识的形状非法。"


@dataclass(frozen=True, slots=True)
class Partition:
    """一个存储分区：一个 scope 加上它在该 scope 内的 token。

    `token` 直接当文件名用——三类 token 的字符集都已由契约层保证安全
    （`storage_id()` 的编码结果 / `validate_identifier` 过的 scope / 字面量 `agent`）。
    """

    scope: FragmentScope
    token: str

    @property
    def filename(self) -> str:
        """分区文件的基名（不含扩展名）。scope 前缀让三类分区在一个目录里也分得开。"""
        return f"{self.scope.value}-{self.token}"


def partition_for(scope: FragmentScope, key: SessionKey) -> Partition:
    """一条记忆该存到哪个分区。**异常约定**：`USER` 抛 `INPUT_MALFORMED`。"""
    if scope is FragmentScope.SESSION:
        return Partition(scope, key.storage_id())
    if scope is FragmentScope.WORKSPACE:
        return Partition(scope, key.scope)
    if scope is FragmentScope.AGENT:
        return Partition(scope, AGENT_PARTITION)
    # 用 `INPUT_MALFORMED` 而不是新增一个「本实现不支持」的码：调用方给了一个本实现
    # 服务不了的取值，那就是一次非法输入。`INPUT_UNSUPPORTED_MEDIA` 字面上是媒体类型，
    # 借它会让诊断里「不支持的图片格式」与这件事混在一起。
    raise KaryviaError(
        ErrorCode.INPUT_MALFORMED,
        _UNSUPPORTED_SCOPE,
        detail={"scope": scope.value, "supported": [s.value for s in RECALL_ORDER]},
    )


def partitions_for(scopes: tuple[FragmentScope, ...], key: SessionKey) -> tuple[Partition, ...]:
    """一次召回该查哪几个分区，按 `RECALL_ORDER` 排列且去重。

    传进来的 `scopes` 来自配置（`enabled_scopes`），因此顺序由本模块统一，不由配置的
    书写顺序决定——那会让「改一下配置里的顺序」变成一次行为变更。
    """
    wanted = set(scopes)
    return tuple(partition_for(scope, key) for scope in RECALL_ORDER if scope in wanted)


def record_id(partition: Partition, sequence: int) -> str:
    """记录标识 = `<scope>-<token>#<seq>`。

    把分区编进 id 里，`forget(record_id)` 才能直接打开那一个文件，而不必扫全部分区——
    契约说 `forget` 返回「是否真的存在过」，扫全部分区同样答得出来，但代价是每次删除都要
    读一遍所有记忆。
    """
    return f"{partition.filename}{RECORD_ID_SEPARATOR}{sequence}"


def parse_record_id(value: str) -> tuple[Partition, int]:
    """`record_id()` 的逆运算。**异常约定**：形状不符抛 `INPUT_MALFORMED`。

    这里刻意**不**校验分区是否真的存在——那是 IO 层的事，而「解析得动但文件里没有」
    正是 `forget()` 要返回 `False` 的那种情况。
    """
    head, separator, tail = value.rpartition(RECORD_ID_SEPARATOR)
    if not separator or not head or not tail.isdigit():
        raise _malformed(value)
    scope_value, dash, token = head.partition("-")
    if not dash or not token:
        raise _malformed(value)
    try:
        scope = FragmentScope(scope_value)
    except ValueError as error:
        raise _malformed(value) from error
    if scope not in RECALL_ORDER:
        raise _malformed(value)
    return Partition(scope, token), int(tail)


def _malformed(value: str) -> KaryviaError:
    # 原始串照放：它由用户或模型给出，不含宿主机信息。截断是为了挡住一条超长参数。
    return KaryviaError(
        ErrorCode.INPUT_MALFORMED, _MALFORMED_RECORD_ID, detail={"record_id": value[:200]}
    )
