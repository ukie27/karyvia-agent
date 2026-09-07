"""配置的三个来源与它们的优先级。

职责：把 `config.json`、`KARYVIA_*` 环境变量、CLI 覆盖各自读成一层原始 JSON 映射，
并按优先级从低到高排好交给 `merge.py`。
不负责：合并（`merge.py`）、校验（`schema.py`）、定位实例目录（`layout.py`）。

优先级只在 `collect_layers()` 的返回顺序里定义一次：**文件 < 环境变量 < CLI**。

`config.json` **只读，绝不回写**：用户手写的文件里有注释式的键序和格式，
任何「顺手规范化一下」都会毁掉它。生成初始配置是 `karyvia init`，不是加载路径的事。

环境变量走 `KARYVIA_CFG_<路径>` 的通用形式，不为每个字段登记一个专名。字段是会长的，
而每加一个字段就要改一张映射表的设计，注定会漏。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Mapping, Sequence, cast

from ...contracts import ErrorCode, KaryviaError
from .merge import ConfigLayer
from .schema import defaults

if TYPE_CHECKING:
    from ...contracts import JsonValue

__all__ = [
    "CLI_ORIGIN",
    "DEFAULT_ORIGIN",
    "ENV_ORIGIN",
    "ENV_PREFIX",
    "FILE_ORIGIN",
    "collect_layers",
    "env_layer",
    "file_layer",
    "overrides_layer",
    "parse_override",
    "read_config_file",
]

#: 配置字段的环境变量前缀。`KARYVIA_CFG_TURN__MAX_ITERATIONS=32` -> `turn.max_iterations`。
ENV_PREFIX = "KARYVIA_CFG_"

#: 嵌套分隔符。用双下划线而不是单个：字段名本身就含下划线（`max_tool_calls_per_turn`），
#: 单下划线无法区分「层级」与「词间」。
ENV_NESTING_SEPARATOR = "__"

FILE_ORIGIN = "config.json"
ENV_ORIGIN = "env"
CLI_ORIGIN = "cli"

#: 内置默认值层的来源名。物化成一层而不是留白，是 当前约束：
#: 「这个值取自默认值」必须是一个查得到的答案，而不是「查不到来源」的兜底解释。
DEFAULT_ORIGIN = "default"

#: 配置文件大小上限。一份配置是几 KB；上限只为挡住误指到大文件时把它整个读进内存。
MAX_CONFIG_BYTES = 1_048_576


def read_config_file(path: Path) -> Mapping[str, JsonValue]:
    """读并解析 `config.json`。**文件不存在时返回空映射**，那不是错误。

    默认值本身构成一份完整合法的配置（`KaryviaConfig` 的每个小节都有默认值），所以首次
    运行不该因为缺文件而失败。
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise KaryviaError(
            ErrorCode.PERSISTENCE_READ_FAILED,
            "无法读取配置文件。",
            detail={"path": str(path), "errno": exc.errno},
        ) from exc

    if len(raw) > MAX_CONFIG_BYTES:
        raise KaryviaError(
            ErrorCode.CONFIG_INVALID,
            "配置文件过大，请确认路径是否指向了正确的文件。",
            detail={"path": str(path), "size": len(raw), "limit": MAX_CONFIG_BYTES},
        )

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise KaryviaError(
            ErrorCode.CONFIG_INVALID,
            "配置文件不是合法的 UTF-8 文本。",
            detail={"path": str(path), "reason": exc.reason},
        ) from exc
    except json.JSONDecodeError as exc:
        # 行列号是这里唯一能给出的「位置」——JSON 坏掉时还没有字段可言。
        raise KaryviaError(
            ErrorCode.CONFIG_INVALID,
            "配置文件不是合法的 JSON。",
            detail={
                "path": str(path),
                "line": exc.lineno,
                "column": exc.colno,
                "reason": exc.msg,
                "suggestion": "检查该位置附近是否有多余逗号、缺少引号或未闭合的括号。",
            },
        ) from exc

    if not isinstance(parsed, dict):
        raise KaryviaError(
            ErrorCode.CONFIG_INVALID,
            "配置文件的顶层必须是 JSON 对象。",
            detail={"path": str(path), "actual_type": type(parsed).__name__},
        )
    # 边界窄化：`json.loads` 交出 `Any`，在这里定型成配置层的形状。往里的每一层都只认
    # `JsonValue`，未定型的值不允许流进 Kernel。
    return cast("Mapping[str, JsonValue]", parsed)


def file_layer(path: Path) -> ConfigLayer:
    return ConfigLayer(origin=FILE_ORIGIN, data=read_config_file(path))


def _assign(target: dict[str, JsonValue], path: Sequence[str], value: JsonValue, source: str) -> None:
    """按点分路径写入嵌套字典。

    中途撞上非字典（`turn=1` 又 `turn.max_iterations=2`）时报错而不是悄悄覆盖：那是
    用户的输入自相矛盾，静默选一个就等于替他猜。
    """
    cursor = target
    for index, token in enumerate(path[:-1]):
        existing = cursor.get(token)
        if existing is None:
            fresh: dict[str, JsonValue] = {}
            cursor[token] = fresh
            cursor = fresh
            continue
        if not isinstance(existing, dict):
            raise KaryviaError(
                ErrorCode.CONFIG_INVALID,
                "配置覆盖的路径与同来源的另一项冲突。",
                detail={"source": source, "path": ".".join(path[: index + 1])},
            )
        cursor = existing
    cursor[path[-1]] = value


def _decode_scalar(raw: str) -> JsonValue:
    """把字符串解成 JSON 标量，失败则按原字符串处理。

    `32` 要变成 int（schema 要 int），而 `openai` 必须留成字符串。先试 JSON 再退化成
    字符串是唯一不需要提前知道目标类型的做法——`sources.py` 不该认识 schema。
    """
    try:
        return cast("JsonValue", json.loads(raw))
    except ValueError:
        return raw


def env_layer(env: Mapping[str, str] | None = None) -> ConfigLayer:
    """收集 `KARYVIA_CFG_*`。

    键统一小写：环境变量惯例是大写，而配置字段是 snake_case（AGENTS.md：新层只读
    snake_case）。
    """
    environ = os.environ if env is None else env
    data: dict[str, JsonValue] = {}
    for name in sorted(environ):
        if not name.startswith(ENV_PREFIX):
            continue
        suffix = name[len(ENV_PREFIX) :]
        if not suffix:
            continue
        path = [token.lower() for token in suffix.split(ENV_NESTING_SEPARATOR) if token]
        if not path:
            continue
        _assign(data, path, _decode_scalar(environ[name]), f"{ENV_ORIGIN}:{name}")
    return ConfigLayer(origin=ENV_ORIGIN, data=data)


def parse_override(text: str) -> tuple[list[str], JsonValue]:
    """解析一条 CLI 覆盖：`turn.max_iterations=32` -> `(["turn", "max_iterations"], 32)`。"""
    key, separator, raw = text.partition("=")
    if not separator or not key.strip():
        raise KaryviaError(
            ErrorCode.CONFIG_INVALID,
            "配置覆盖必须写成 键=值 的形式。",
            detail={"override": text, "suggestion": "例如 --set turn.max_iterations=32"},
        )
    path = [token for token in key.strip().split(".") if token]
    if not path:
        raise KaryviaError(
            ErrorCode.CONFIG_INVALID,
            "配置覆盖的键为空。",
            detail={"override": text},
        )
    return (path, _decode_scalar(raw))


def overrides_layer(overrides: Iterable[str] | None = None) -> ConfigLayer:
    """收集 CLI 的 `--set 键=值`。优先级最高。"""
    data: dict[str, JsonValue] = {}
    for text in overrides or ():
        path, value = parse_override(text)
        _assign(data, path, value, f"{CLI_ORIGIN}:{text}")
    return ConfigLayer(origin=CLI_ORIGIN, data=data)


def collect_layers(
    config_path: Path,
    *,
    env: Mapping[str, str] | None = None,
    overrides: Iterable[str] | None = None,
) -> tuple[ConfigLayer, ...]:
    """按优先级**从低到高**返回四层。这个顺序就是 §6.7 的优先级定义。

    内置默认值是最低的一层。它必须在这里、而不是靠 dataclass 的字段默认值兜底：
    只有当默认值也是一层， 的「每个生效值可追溯来源」才对**所有**字段成立。
    """
    return (
        ConfigLayer(origin=DEFAULT_ORIGIN, data=defaults()),
        file_layer(config_path),
        env_layer(env),
        overrides_layer(overrides),
    )
