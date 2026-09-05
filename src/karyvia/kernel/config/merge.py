"""带来源追踪的分层合并（技术方案 §6.7 的四层优先级）。

职责：把若干原始 JSON 映射按优先级从低到高合并成一个映射，并为**每个标量叶子**记录它
最终来自哪一层，位置用 JSON Pointer（RFC 6901）表达。
不负责：校验形状或类型（`schema.py`）、知道各层从哪来（`sources.py`）。

合并在**校验之前**、在原始 JSON 上进行，这是 §6.7 的两个要求同时成立的唯一次序：
校验错误要报在合并后的整份配置上（不然默认值填不进去，必填项会在每一层都报缺失），
而来源追踪只有在合并这一步才有信息可记（校验后的模型里已经看不出哪层写的了）。

合并规则只有两条：映射递归合并，**其它一切按值替换**。列表不做逐元素合并——
`plugins.disable` 这类列表如果按下标合并，用户在 CLI 上给一个短列表就会「部分覆盖」出
一个他从未写过的组合；整体替换是唯一可预测的语义。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Mapping, Sequence

if TYPE_CHECKING:
    from ...contracts import JsonValue

__all__ = ["ConfigLayer", "MergeResult", "escape_pointer_token", "merge_layers", "pointer_of"]


def escape_pointer_token(token: str) -> str:
    """按 RFC 6901 转义单个 pointer 分量：`~` -> `~0`，`/` -> `~1`。

    顺序不可交换：先换 `/` 会把它产出的 `~1` 里的 `~` 再转义成 `~01`。
    """
    return token.replace("~", "~0").replace("/", "~1")


def pointer_of(path: Sequence[str]) -> str:
    """把字段路径拼成 JSON Pointer。根是空串。"""
    if not path:
        return ""
    return "".join(f"/{escape_pointer_token(token)}" for token in path)


@dataclass(frozen=True, slots=True)
class ConfigLayer:
    """一层原始配置。`origin` 是给人看的来源名（如 `config.json`、`env`、`cli`）。"""

    origin: str
    data: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class MergeResult:
    """合并结果与来源索引。"""

    data: dict[str, JsonValue]
    #: JSON Pointer -> 胜出层的 `origin`。只记标量叶子与被整体替换的容器。
    origins: dict[str, str]

    def origin_of(self, *path: str) -> str | None:
        """查某个字段的来源。未被任何层设置（即取自默认值）时返回 `None`。"""
        return self.origins.get(pointer_of(path))


def merge_layers(layers: Iterable[ConfigLayer]) -> MergeResult:
    """按给定顺序合并，**后来者优先**。

    调用方负责把层按优先级从低到高排好（§6.7：文件 < 环境变量 < CLI）。这里不排序：
    优先级顺序属于「配置从哪来」的知识，归 `sources.py`，混进合并算法里就会有两处定义。
    """
    merged: dict[str, JsonValue] = {}
    origins: dict[str, str] = {}
    for layer in layers:
        _merge_into(merged, layer.data, layer.origin, (), origins)
    return MergeResult(data=merged, origins=origins)


def _merge_into(
    target: dict[str, JsonValue],
    incoming: Mapping[str, JsonValue],
    origin: str,
    path: tuple[str, ...],
    origins: dict[str, str],
) -> None:
    for key, value in incoming.items():
        child = (*path, key)
        existing = target.get(key)
        if isinstance(value, Mapping) and isinstance(existing, dict):
            _merge_into(existing, value, origin, child, origins)
            continue
        if isinstance(value, Mapping):
            # 新的子树，或是替换掉一个非映射的旧值。复制进去并把整棵子树标成本层来源。
            fresh: dict[str, JsonValue] = {}
            _merge_into(fresh, value, origin, child, origins)
            target[key] = fresh
            continue
        target[key] = value
        origins[pointer_of(child)] = origin
        # 这个位置以前可能是个映射，它的子孙来源记录已经失效了。
        _drop_descendants(origins, pointer_of(child))


def _drop_descendants(origins: dict[str, str], pointer: str) -> None:
    """删掉 `pointer` 之下的所有来源记录。

    JSON Pointer 的前缀关系必须按 `/` 边界判断：`/a/bc` 不是 `/a/b` 的子孙，纯字符串
    `startswith` 会把它误删。
    """
    prefix = f"{pointer}/"
    stale = [key for key in origins if key.startswith(prefix)]
    for key in stale:
        del origins[key]
