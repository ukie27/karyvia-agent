"""插件候选发现与 manifest 原始对象读取。

职责：把 Runtime 显式交入的 ``(plugin_id, module:attribute)`` 元数据变成候选，并在调用方
决定启用后读取 manifest 原始对象。
不负责：扫描 Python 环境或目录、安装插件、决定实例是否启用、解析 SDK manifest、导入
``setup`` 或注册能力。

候选 id 来自全局安装目录，而不是 manifest。Runtime 因此能在导入任何插件模块之前应用
``plugins.enabled``；未启用插件没有代码执行路径。重复 id 两边都不生效，避免让元数据顺序
暗中决定赢家。
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from karyvia.contracts import ErrorCode, KaryviaError

__all__ = [
    "ENTRY_POINT_GROUP",
    "Discovery",
    "EntryPointLister",
    "PluginCandidate",
    "discover",
    "read_candidate",
]

ENTRY_POINT_GROUP: Final = "karyvia.plugins"
_ATTRIBUTE_SEPARATOR: Final = ":"

EntryPointLister = Callable[[], Sequence[tuple[str, str]]]


def _no_entry_points() -> tuple[tuple[str, str], ...]:
    return ()


@dataclass(frozen=True, slots=True)
class PluginCandidate:
    """一条无需导入插件即可取得的全局安装目录记录。"""

    plugin_id: str
    location: str

    @property
    def origin(self) -> str:
        return f"{ENTRY_POINT_GROUP}:{self.plugin_id} -> {self.location}"


@dataclass(frozen=True, slots=True)
class Discovery:
    """候选清单与元数据冲突。"""

    candidates: tuple[PluginCandidate, ...] = ()
    failures: tuple[KaryviaError, ...] = ()


def discover(*, entry_points: EntryPointLister = _no_entry_points) -> Discovery:
    """枚举 Runtime 交入的全局候选；不读取或导入 manifest。"""
    found = [PluginCandidate(plugin_id=name, location=value) for name, value in entry_points()]
    counts: dict[str, int] = {}
    for candidate in found:
        counts[candidate.plugin_id] = counts.get(candidate.plugin_id, 0) + 1

    failures = tuple(
        KaryviaError(
            ErrorCode.PLUGIN_REGISTRATION_CONFLICT,
            "全局安装目录含有重复插件 id；重复项都不会被加载。",
            detail={
                "plugin_id": plugin_id,
                "origins": [item.origin for item in found if item.plugin_id == plugin_id],
            },
        )
        for plugin_id, count in sorted(counts.items())
        if count > 1
    )
    candidates = tuple(item for item in found if counts[item.plugin_id] == 1)
    return Discovery(candidates=candidates, failures=failures)


def read_candidate(candidate: PluginCandidate) -> object:
    """导入并读取候选的 manifest 原始对象；Kernel 不解释 SDK 数据类型。"""
    module_name, separator, attribute = candidate.location.partition(_ATTRIBUTE_SEPARATOR)
    if not separator or not module_name or not attribute:
        raise _failure(candidate, 'entry point 必须写成 "pkg.module:MANIFEST"。')
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise _failure(
            candidate,
            "导入插件 manifest 模块失败。",
            module=module_name,
            exception=type(exc).__name__,
        ) from exc
    found = getattr(module, attribute, None)
    if found is None:
        raise _failure(candidate, "插件模块里没有 manifest 对象。", attribute=attribute)
    return found


def _failure(candidate: PluginCandidate, message: str, **detail: object) -> KaryviaError:
    return KaryviaError(
        ErrorCode.PLUGIN_LOAD_FAILED,
        message,
        detail={
            "plugin_id": candidate.plugin_id,
            "origin": candidate.origin,
            "source": "global_entry_point",
            **detail,
        },
    )
