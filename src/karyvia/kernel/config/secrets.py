"""`${VAR}` 凭据引用的解析与写回保护。

职责：在配置文档里找出 `${VAR}` 引用、按环境变量解析成 `SecretStr`，并在写盘之前把明文
换回原始 `${VAR}` 字面量。
不负责：读环境变量之外的任何来源（`sources.py`）、校验字段形状（`schema.py`）、
真的写文件（`kernel/config/` 一个字节都不写，生成 `config.json` 是 ）、
决定插件是否使用某个密钥。

三条必须在这一层说清楚的事：

- **明文不进配置文档。** `resolve_secrets()` 不返回一份替换过的文档，而是返回按 JSON
  Pointer 索引的 `SecretMap`：配置树自始至终持有 `${VAR}` 字面量，明文只活在
  `SecretStr` 里。于是 （写回不得回写明文）不是一条要人记得遵守的流程，而是
  **没有别的东西可写**——加载路径根本不产生含明文的文档。
- **任何位置的引用都算密钥。** 字符串里只要出现 `${VAR}`（整串或内嵌 `Bearer ${TOKEN}`），
  整个值解析后就是 `SecretStr`。不提供「插值但不是密钥」的第二种语义，也没有
  `${VAR:-默认值}` 这类 shell 回退——缺变量是硬错误，静默降级只会把故障推到第一次调用。
  不支持 `$${VAR}` 转义：半个机制不如没有。
- **`prepare_for_write()` 是写盘前的最后一道闸。** 结构性保证挡不住「有人 `reveal()`
  之后把明文塞回文档」。这道闸把 `SecretStr` 与等于已知明文的裸字符串换回字面量，
  换不回去时抛错而不是写明文。

诊断视图不需要新函数：原始文档本身就是安全视图（值是 `${VAR}` 字面量），而且展示
`${OPENAI_API_KEY}` 比展示 `***` 更有用——它告诉用户去哪个变量里找。多造一个视图只会
多一条要被哨兵测试覆盖的输出路径。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Mapping, Sequence, cast

from ...contracts import ErrorCode, KaryviaError, SecretStr
from ...contracts.errors import MIN_SCRUB_LENGTH
from .merge import pointer_of

if TYPE_CHECKING:
    from ...contracts import JsonValue

__all__ = [
    "SECRET_REF_PATTERN",
    "SecretMap",
    "SecretRef",
    "contains_secret_ref",
    "prepare_for_write",
    "resolve_secrets",
    "resolve_text",
    "scan_secret_refs",
    "secret_ref_names",
]

#: `${VAR}` 引用。变量名按 POSIX 环境变量惯例限定为 `[A-Za-z_][A-Za-z0-9_]*`——放宽到
#: 任意字符会让 `${}`、`${a b}` 这类明显的笔误被当成合法引用，然后报「变量未设置」。
SECRET_REF_PATTERN: Final = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

#: 变量缺失的两种原因。分开记是为了让错误消息能说清「没导出」还是「导成了空串」，
#: 两者的修法不同；两者都**不含值**（空串也没有值可言）， 因此不受影响。
REASON_UNSET: Final = "unset"
REASON_EMPTY: Final = "empty"


def secret_ref_names(text: str) -> tuple[str, ...]:
    """按出现顺序列出串里引用到的变量名，重复只算一次。"""
    seen: dict[str, None] = {}
    for match in SECRET_REF_PATTERN.finditer(text):
        seen.setdefault(match.group(1), None)
    return tuple(seen)


def contains_secret_ref(text: str) -> bool:
    return SECRET_REF_PATTERN.search(text) is not None


@dataclass(frozen=True, slots=True)
class SecretRef:
    """配置里的一处 `${VAR}` 引用。

    `literal` 是**原始整串**（可能含 `${VAR}` 之外的字面文本），写回时原样放回去——
    只记变量名的话，`"Bearer ${TOKEN}"` 写回就会丢掉 `Bearer ` 那一截。
    """

    #: 该值在配置文档里的位置，RFC 6901 JSON Pointer。列表下标也是一段（`/a/paths/0`）。
    pointer: str
    #: 原始字面量，例如 `"${OPENAI_API_KEY}"` 或 `"Bearer ${TOKEN}"`。
    literal: str
    #: 引用到的变量名，按出现顺序。
    names: tuple[str, ...]


#: 空映射常量。用 `MappingProxyType` 而不是 `field(default_factory=dict)`：后者在严格类型
#: 检查下是 `dict[Unknown, Unknown]`，而这三个字段的类型正是本模块要保证的东西。
_NO_REFS: Final[Mapping[str, "SecretRef"]] = MappingProxyType({})
_NO_SECRETS: Final[Mapping[str, SecretStr]] = MappingProxyType({})


@dataclass(frozen=True, slots=True, repr=False)
class SecretMap:
    """一次解析的结果。**明文只存在于这里的 `SecretStr` 中，不在配置文档里。**

    `repr` 自己实现：dataclass 生成的那个会把三个 Mapping 全展开，其中 `SecretStr` 虽然
    各自渲染成掩码，但一个「看起来很详细」的 repr 出现在日志里，早晚会有人往里加一个
    带明文的字段。只报数量，不报内容。
    """

    #: pointer -> 引用。写回用。
    refs: Mapping[str, SecretRef] = _NO_REFS
    #: pointer -> 该位置解析后的**整值**。
    values: Mapping[str, SecretStr] = _NO_SECRETS
    #: 变量名 -> 明文。 /  按变量名取值时用它，不必再读一次环境。
    variables: Mapping[str, SecretStr] = _NO_SECRETS

    def __repr__(self) -> str:
        return f"SecretMap(refs={len(self.refs)}, variables={len(self.variables)})"

    def __len__(self) -> int:
        return len(self.refs)

    def __bool__(self) -> bool:
        return bool(self.refs)

    def at(self, *path: str) -> SecretStr | None:
        """按字段路径取解析后的值；该位置没有引用时返回 `None`。"""
        return self.values.get(pointer_of(path))

    def literal_at(self, *path: str) -> str | None:
        """按字段路径取原始 `${VAR}` 字面量；该位置没有引用时返回 `None`。"""
        ref = self.refs.get(pointer_of(path))
        return None if ref is None else ref.literal

    def variable(self, name: str) -> SecretStr | None:
        return self.variables.get(name)


def scan_secret_refs(data: Mapping[str, JsonValue]) -> tuple[SecretRef, ...]:
    """列出文档里的全部 `${VAR}` 引用。**纯扫描，不碰环境变量。**

     生成初始配置、`karyvia doctor` 检查「哪些变量需要被导出」都只需要这一步，不该因为
    某个变量还没导出就失败。
    """
    found: list[SecretRef] = []
    _scan(data, (), found)
    return tuple(found)


def _scan(value: JsonValue, path: tuple[str, ...], found: list[SecretRef]) -> None:
    if isinstance(value, str):
        names = secret_ref_names(value)
        if names:
            found.append(SecretRef(pointer=pointer_of(path), literal=value, names=names))
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _scan(item, (*path, key), found)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            _scan(item, (*path, str(index)), found)


def _lookup(
    names: Sequence[str], env: Mapping[str, str]
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """查一组变量名，返回 `(已取到的值, [(缺失的变量名, 原因)])`。空值按缺失处理。

    `OPENAI_API_KEY=` 几乎总是配错。静默接受一个空密钥只会把错误推迟到第一次模型调用，
    那时的报错是「服务端 401」，与真正的原因隔了三层。
    """
    resolved: dict[str, str] = {}
    missing: list[tuple[str, str]] = []
    for name in names:
        raw = env.get(name)
        if raw is None:
            missing.append((name, REASON_UNSET))
        elif not raw.strip():
            missing.append((name, REASON_EMPTY))
        else:
            resolved[name] = raw
    return (resolved, missing)


def _missing_error(
    entries: Sequence[tuple[str, str, str]], *, source: str = ""
) -> KaryviaError:
    """构造缺失变量的错误。**只有变量名、位置与原因，没有任何值**。

    `source` 是那份配置在磁盘上的位置。它由调用方传进来而不是本模块推导——`kernel/config/`
    的解析路径接的是一棵已经在内存里的树，它并不知道那棵树是从哪个文件读来的。
    带上它是的一半：「指出配置位置**和**字段名」，指针给的是后一半。
    """
    names = sorted({name for name, _, _ in entries})
    detail: list[JsonValue] = [
        {"name": name, "pointer": pointer, "reason": reason} for name, pointer, reason in entries
    ]
    payload: dict[str, JsonValue] = {
        "missing": detail,
        "suggestion": (
            "在启动 karyvia 的环境里导出这些变量；配置文件里保留 ${VAR} 引用，不要填明文。"
        ),
    }
    if source:
        payload["file"] = source
    return KaryviaError(
        ErrorCode.CONFIG_SECRET_MISSING,
        f"配置引用的环境变量不可用：{'、'.join(names)}。",
        detail=payload,
    )


def resolve_text(
    text: str,
    *,
    env: Mapping[str, str] | None = None,
    pointer: str = "",
    source: str = "",
) -> SecretStr | str:
    """解析单个字符串。**含引用则返回 `SecretStr`，不含则原样返回 `str`**。

    返回类型是联合而不是恒为 `SecretStr`：调用方拿到的可能是
    一个用户直接写在配置里的明文值，把它也包成 `SecretStr` 会让「这个值来自环境变量」
    这条信息消失，而那正是决定要不要提醒用户「别把密钥写进文件」的依据。

    **异常约定**：引用到的变量未设置或为空时抛 `KaryviaError(CONFIG_SECRET_MISSING)`，
    消息与 `detail` 只含变量名、指针与（给了的话）配置文件路径。
    """
    names = secret_ref_names(text)
    if not names:
        return text
    resolved, missing = _lookup(names, os.environ if env is None else env)
    if missing:
        raise _missing_error(
            [(name, pointer, reason) for name, reason in missing], source=source
        )
    return SecretStr(SECRET_REF_PATTERN.sub(lambda m: resolved[m.group(1)], text))


def resolve_secrets(
    data: Mapping[str, JsonValue],
    *,
    env: Mapping[str, str] | None = None,
    source: str = "",
) -> SecretMap:
    """解析整份文档里的全部引用。**不返回替换过的文档**，见模块 docstring。

    **异常约定**：任何一处变量缺失即抛 `KaryviaError(CONFIG_SECRET_MISSING)`，且**一次报
    全部缺失**——与 `validate_config()` 同构。逐条抛出会让用户导一个变量、重启、再看到
    下一个，而缺三个变量是首次配置的常态。
    """
    environ = os.environ if env is None else env
    refs = scan_secret_refs(data)

    missing: list[tuple[str, str, str]] = []
    variables: dict[str, SecretStr] = {}
    plain: dict[str, str] = {}
    for ref in refs:
        resolved, absent = _lookup(ref.names, environ)
        for name, reason in absent:
            missing.append((name, ref.pointer, reason))
        for name, value in resolved.items():
            plain[name] = value
            variables[name] = SecretStr(value)

    if missing:
        raise _missing_error(missing, source=source)

    values = {
        ref.pointer: SecretStr(SECRET_REF_PATTERN.sub(lambda m: plain[m.group(1)], ref.literal))
        for ref in refs
    }
    return SecretMap(
        refs={ref.pointer: ref for ref in refs},
        values=values,
        variables=variables,
    )


def prepare_for_write(
    document: Mapping[str, object],
    secrets: SecretMap,
) -> dict[str, JsonValue]:
    """把待写文档里的明文换回 `${VAR}` 字面量，返回可以安全落盘的副本。

    两条替换规则，按顺序：

    1. `SecretStr` -> 它对应的原始字面量。按位置找不到时按明文反查（值被搬过位置的情况）。
    2. 与某个变量明文**完全相等**的裸字符串 -> `${VAR}`。这条防的是「有人 `reveal()`
       之后把明文塞回文档」。只认完全相等：子串替换会把一个恰好等于密钥的普通短值
       改写成引用，那是在悄悄改用户的配置。

    两条**按明文反查**的规则都只对长度 ≥ `MIN_SCRUB_LENGTH` 的值生效（与 `scrub()` 同一
    条阈值）：一个 4 个字符的密钥值和用户随手写的 `"1234"` 无法区分，把后者改写成
    `${VAR}` 是在改用户的配置。按**位置**恢复不受此限——那条不需要猜。

    换不回去的 `SecretStr` 一律抛错。「找不到来源就写明文」是这条防线唯一不能有的行为。

    本函数**不写文件**：`kernel/config/` 全包不写文件，落盘是的事。

    **异常约定**：文档里的 `SecretStr` 无法对应到任何已知引用时抛
    `KaryviaError(KERNEL_INVARIANT_VIOLATED)`，`detail` 只含位置。
    """
    by_resolved = {
        secrets.values[pointer].reveal(): ref.literal
        for pointer, ref in secrets.refs.items()
        if pointer in secrets.values
        and len(secrets.values[pointer].reveal()) >= MIN_SCRUB_LENGTH
    }
    by_variable = {
        value.reveal(): f"${{{name}}}"
        for name, value in secrets.variables.items()
        if len(value.reveal()) >= MIN_SCRUB_LENGTH
    }
    result = _sanitize(document, (), secrets, by_resolved, by_variable)
    # `_sanitize` 对 Mapping 一定返回 dict；这里只是把类型收窄回签名承诺的形状。
    return result if isinstance(result, dict) else {}


def _sanitize(
    value: object,
    path: tuple[str, ...],
    secrets: SecretMap,
    by_resolved: Mapping[str, str],
    by_variable: Mapping[str, str],
) -> JsonValue:
    if isinstance(value, SecretStr):
        pointer = pointer_of(path)
        here = secrets.refs.get(pointer)
        literal = here.literal if here is not None else by_resolved.get(value.reveal())
        if literal is None:
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "待写配置里有一个无法表达为 ${VAR} 引用的密钥，拒绝写入。",
                detail={
                    "pointer": pointer,
                    "suggestion": "密钥只能以 ${VAR} 引用的形式进配置文件。",
                },
            )
        return literal
    if isinstance(value, str):
        replacement = by_resolved.get(value)
        if replacement is None:
            replacement = by_variable.get(value)
        return value if replacement is None else replacement
    if isinstance(value, Mapping):
        items = cast("Mapping[str, object]", value)
        return {
            key: _sanitize(item, (*path, key), secrets, by_resolved, by_variable)
            for key, item in items.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        entries = cast("Sequence[object]", value)
        return [
            _sanitize(item, (*path, str(index)), secrets, by_resolved, by_variable)
            for index, item in enumerate(entries)
        ]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    # 非 JSON 形状的值不该出现在待写文档里。不静默丢弃、也不 `str()` 它——`str()` 正是
    # 明文泄漏最爱走的那条路。
    raise KaryviaError(
        ErrorCode.KERNEL_INVARIANT_VIOLATED,
        "待写配置里出现了无法序列化为 JSON 的值。",
        detail={"pointer": pointer_of(path), "actual_type": type(value).__name__},
    )
