"""配置解析与一次性校验（插件只看得见自己那一块）。

职责：把 `plugins.memory.config` 变成一个不可变设置对象，并在 `setup()` 里把能查的错一次查完。
不负责：存储（`store.py`）、召回（`provider.py`）、工具与命令的参数校验（各自模块）。

**没有「记忆后端」这个配置项。** 换后端的正规做法是装另一个声明 `overrides` 的插件，
而不是在这里长一张 `backend: "jsonl" | "sqlite" | "qdrant"` 的表——那张表会把每一种后端的
依赖都拖进本发行包，而的全部意义就是不必如此（ 拒过 `max_tokens_field`
slug 表、 拒过四张版本 gating 表，理由一个字没变）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from karyvia.contracts import ErrorCode, FragmentScope, JsonValue, KaryviaError
from karyvia.sdk import ManifestJsonSchema

from .partition import RECALL_ORDER

__all__ = [
    "CONFIG_SCHEMA",
    "DEFAULT_ENABLED_SCOPES",
    "DEFAULT_FRAGMENT_PRIORITY",
    "DEFAULT_RECALL_LIMIT",
    "MEMORY_DIR_NAME",
    "MemorySettings",
    "resolve_settings",
]

#: `ctx.state_dir` 下的默认子目录名。
MEMORY_DIR_NAME: Final = "memory"

#: 每轮自动召回几条。默认取小：召回内容按定义是 `UNTRUSTED` 的参考数据，一次塞十条
#: 只会稀释真正相关的那一条，还要占掉本可以留给历史的预算。
DEFAULT_RECALL_LIMIT: Final = 5

#: 召回片段的基准 `priority`。**必须 > 0**：`kernel/turn/context_builder.py` 的
#: `HISTORY_TRIM_PRIORITY` 是 0，而组装器按 priority **逆序**丢弃。记忆排在历史之前被丢
#: 是刻意的——记忆下一轮还能重新召回，历史丢了就是丢了。
DEFAULT_FRAGMENT_PRIORITY: Final = 50

#: 默认参与自动召回的范围。三个都开：`agent` 是跨会话记忆的意义所在，`session` 让眼前这段
#: 对话的要点不必等到被压缩才可用，`workspace` 让同一项目下的多个会话共享结论。
DEFAULT_ENABLED_SCOPES: Final[tuple[str, ...]] = tuple(scope.value for scope in RECALL_ORDER)

_DEFAULT_MIN_SCORE: Final = 0.0
_DEFAULT_MAX_RESULT_CHARS: Final = 4_000
_DEFAULT_LIST_LIMIT: Final = 20

_NOT_A_STRING: Final = "这个配置项必须是字符串。"
_NOT_A_BOOL: Final = "这个配置项必须是布尔值。"
_NOT_A_POSITIVE_INT: Final = "这个配置项必须是正整数。"
_NOT_A_NUMBER: Final = "这个配置项必须是非负数字。"
_NOT_A_STRING_LIST: Final = "这个配置项必须是字符串数组。"
_UNKNOWN_SCOPE: Final = "未知的记忆范围。"
_EMPTY_SCOPES: Final = "至少要启用一个记忆范围；不想要自动召回请把 auto_recall 设为 false。"

#: manifest 的 `config_schema`。它校验**形状**，`resolve_settings()` 校验它表达不了的那些
#: （枚举取值要给出「你可以写哪几个」、跨字段一致性）。
#: 标注成 `ManifestJsonSchema` 而不是 `contracts.JsonSchema`：契约那个类型进不了
#: pydantic 模型（会 `RecursionError`），细节见 `sdk/manifest.py::ManifestJsonValue`。
CONFIG_SCHEMA: Final[ManifestJsonSchema] = {
    "type": "object",
    "properties": {
        "dir": {
            "type": "string",
            "description": "记忆文件的落点。相对路径按插件状态目录解析，留空即 <state_dir>/memory。",
        },
        "auto_recall": {
            "type": "boolean",
            "description": "是否在每轮 turn 自动召回记忆并放进上下文。",
        },
        "enabled_scopes": {
            "type": "array",
            "items": {"type": "string", "enum": list(DEFAULT_ENABLED_SCOPES)},
            "description": "参与自动召回的范围。",
        },
        "recall_limit": {"type": "integer", "minimum": 1, "description": "每轮最多召回几条。"},
        "min_score": {"type": "number", "minimum": 0, "description": "低于这个相关度的记忆不召回。"},
        "fragment_priority": {
            "type": "integer",
            "minimum": 1,
            "description": "召回片段的基准优先级，必须大于 0（否则会排在会话历史之前被保留）。",
        },
        "list_limit": {"type": "integer", "minimum": 1, "description": "/memory list 默认列几条。"},
        "max_result_chars": {"type": "integer", "minimum": 1, "description": "工具与命令输出的字符上限。"},
    },
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class MemorySettings:
    """本插件的全部设置。"""

    directory: str = ""
    auto_recall: bool = True
    enabled_scopes: tuple[FragmentScope, ...] = RECALL_ORDER
    recall_limit: int = DEFAULT_RECALL_LIMIT
    min_score: float = _DEFAULT_MIN_SCORE
    fragment_priority: int = DEFAULT_FRAGMENT_PRIORITY
    list_limit: int = _DEFAULT_LIST_LIMIT
    max_result_chars: int = _DEFAULT_MAX_RESULT_CHARS


def resolve_settings(config: Mapping[str, JsonValue]) -> MemorySettings:
    """解析 `plugins.memory.config`。**异常约定**：任何问题一律 `CONFIG_INVALID` 并带键路径。"""
    return MemorySettings(
        directory=_string(config, "dir", "").strip(),
        auto_recall=_boolean(config, "auto_recall", True),
        enabled_scopes=_scopes(config),
        recall_limit=_positive_int(config, "recall_limit", DEFAULT_RECALL_LIMIT),
        min_score=_non_negative_number(config, "min_score", _DEFAULT_MIN_SCORE),
        fragment_priority=_positive_int(config, "fragment_priority", DEFAULT_FRAGMENT_PRIORITY),
        list_limit=_positive_int(config, "list_limit", _DEFAULT_LIST_LIMIT),
        max_result_chars=_positive_int(config, "max_result_chars", _DEFAULT_MAX_RESULT_CHARS),
    )


def _scopes(config: Mapping[str, JsonValue]) -> tuple[FragmentScope, ...]:
    """启用的范围。

    **顺序由 `partition.RECALL_ORDER` 统一**，不由配置的书写顺序决定——那会让「调一下
    数组里的顺序」变成一次行为变更，而用户没有理由认为那是一个开关。
    """
    value = config.get("enabled_scopes")
    if value is None:
        return RECALL_ORDER
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise _invalid(_NOT_A_STRING_LIST, "enabled_scopes")
    wanted: set[FragmentScope] = set()
    for item in value:
        if not isinstance(item, str):
            raise _invalid(_NOT_A_STRING_LIST, "enabled_scopes")
        try:
            scope = FragmentScope(item)
        except ValueError as error:
            raise _invalid(
                _UNKNOWN_SCOPE,
                "enabled_scopes",
                value=item,
                choices=list(DEFAULT_ENABLED_SCOPES),
            ) from error
        if scope not in RECALL_ORDER:
            # `user` 是合法的 `FragmentScope` 但本实现服务不了它，理由见 `partition.py`。
            raise _invalid(
                _UNKNOWN_SCOPE,
                "enabled_scopes",
                value=item,
                choices=list(DEFAULT_ENABLED_SCOPES),
            )
        wanted.add(scope)
    if not wanted:
        raise _invalid(_EMPTY_SCOPES, "enabled_scopes")
    return tuple(scope for scope in RECALL_ORDER if scope in wanted)


def _string(config: Mapping[str, JsonValue], key: str, default: str) -> str:
    value = config.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise _invalid(_NOT_A_STRING, key)
    return value


def _boolean(config: Mapping[str, JsonValue], key: str, default: bool) -> bool:
    value = config.get(key)
    if value is None:
        return default
    # `1` 不是 `True`：静默把它当成开启会让「我明明关掉了」查不出原因。
    if not isinstance(value, bool):
        raise _invalid(_NOT_A_BOOL, key)
    return value


def _positive_int(config: Mapping[str, JsonValue], key: str, default: int) -> int:
    value = config.get(key)
    if value is None:
        return default
    # `True` 是 `int` 的实例，放行它会让 `recall_limit: true` 变成 1。
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise _invalid(_NOT_A_POSITIVE_INT, key)
    return value


def _non_negative_number(config: Mapping[str, JsonValue], key: str, default: float) -> float:
    value = config.get(key)
    if value is None:
        return default
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise _invalid(_NOT_A_NUMBER, key)
    return float(value)


def _invalid(message: str, key: str, **detail: object) -> KaryviaError:
    return KaryviaError(
        ErrorCode.CONFIG_INVALID,
        message,
        detail={"key": f"plugins.memory.config.{key}", **detail},
    )
