"""SDK 版本与兼容判定。

职责：导出 `SDK_VERSION`，并按 PEP 440 判定插件声明的 `sdk_range` 是否兼容当前 SDK。
不负责：决定不兼容插件的处置方式，也不负责主程序版本；SDK 与发行包独立演进。

SDK 5.0 删除独立 Context Compactor，统一保留请求级 Turn Compactor 与按次模型门面。
外部插件不能决定宿主是否中断，其加载与运行故障进入诊断；宿主发布的内建基线错误仍由
Runtime 拒绝启动。插件应声明 `>=5.0,<6.0`。
"""

from __future__ import annotations

from typing import Final

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from karyvia.contracts import ErrorCode, KaryviaError

__all__ = ["SDK_VERSION", "is_compatible", "parse_sdk_range"]

#: 当前 SDK 版本（语义化版本，PEP 440 可解析）。插件用 `sdk_range` 声明兼容范围。
SDK_VERSION: Final = "5.0.0"

#: 预解析当前版本；插件校验会重复调用 `is_compatible()`，无需每次解析同一字面量。
_CURRENT: Final = Version(SDK_VERSION)


def parse_sdk_range(sdk_range: str) -> SpecifierSet:
    """把 `sdk_range` 解析为 PEP 440 specifier 集合。

    形状非法抛 `PLUGIN_MANIFEST_UNSUPPORTED` 并带上原始串—— 要求缺失或非法的
    兼容字段直接判定为校验失败并指出位置，不做兜底猜测（比如「解析不了就当全兼容」，
    那正好让最该被拦下的插件通过）。
    """
    try:
        return SpecifierSet(sdk_range)
    except InvalidSpecifier as exc:
        raise KaryviaError(
            ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED,
            "sdk_range 不是合法的 PEP 440 版本范围（例如 \">=1.0,<2.0\"）。",
            detail={"field": "sdk_range", "sdk_range": sdk_range},
        ) from exc


def is_compatible(sdk_range: str, *, sdk_version: str = SDK_VERSION) -> bool:
    """当前 SDK 是否落在插件声明的兼容范围内。

    `prereleases=True`：预发布版本必须能被 `>=1.0,<2.0` 这类范围接纳，否则用
    `1.1.0rc1` 跑一遍插件矩阵就得先改所有插件的 manifest。注意这不改变 PEP 440 的排序
    事实——`2.0.0rc1` 依然小于 `2.0.0`，因此它不满足 `>=2.0`。

    `sdk_version` 只为测试留出注入点——生产路径永远用当前 SDK 版本，不接受调用方
    「换一个版本再试试」。
    """
    specifier = parse_sdk_range(sdk_range)
    version = _CURRENT if sdk_version == SDK_VERSION else _parse_version(sdk_version)
    return specifier.contains(version, prereleases=True)


def _parse_version(value: str) -> Version:
    try:
        return Version(value)
    except InvalidVersion as exc:
        raise KaryviaError(
            ErrorCode.PLUGIN_MANIFEST_UNSUPPORTED,
            "版本号不符合 PEP 440。",
            detail={"field": "version", "version": value},
        ) from exc
