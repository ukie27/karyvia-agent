"""Skill 目录配置的纯解析。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from karyvia.contracts import ErrorCode, JsonValue, KaryviaError

__all__ = ["CONFIG_ROOTS_KEY", "MAX_DESCRIPTION_LENGTH", "MAX_NAME_LENGTH", "read_roots"]

CONFIG_ROOTS_KEY: Final = "roots"
MAX_NAME_LENGTH: Final = 64
MAX_DESCRIPTION_LENGTH: Final = 1024


def read_roots(config: Mapping[str, JsonValue]) -> tuple[Path, ...]:
    value = config.get(CONFIG_ROOTS_KEY)
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise KaryviaError(
            ErrorCode.CONFIG_INVALID,
            "Skill roots 必须是字符串数组。",
            detail={"key": CONFIG_ROOTS_KEY},
        )
    roots = [Path(item).expanduser() for item in value if isinstance(item, str) and item.strip()]
    if len(roots) != len(value):
        raise KaryviaError(
            ErrorCode.CONFIG_INVALID,
            "Skill roots 的每一项都必须是非空字符串。",
            detail={"key": CONFIG_ROOTS_KEY},
        )
    return tuple(roots)
