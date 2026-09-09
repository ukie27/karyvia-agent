"""`tools_fs` 的配置：workspace 根、四项上限与单工具禁用清单。

职责：把 `ctx.config` 校验成一份不可变的 `FsToolSettings`，并回答「这次该注册哪几个
工具」。
不负责：读实例布局（`R4` 禁止内建 import `kernel/`，根由装配根经 `ctx.config["workspace"]`
交下来）、执行工具、判定路径边界（`paths.py`）。

**工具名清单是接口**：`TOOL_NAMES`
在此写死，配置里出现表外的名字一律 `CONFIG_INVALID` 而不是静默忽略——「配置写法合法但被
悄悄忽略」是本项目一贯拒绝的那类失败，尤其当那句配置的本意是**关掉一个能写盘的工具**时。

**单工具禁用为什么在这里而不在 manifest 里**： 要求 manifest 声明的每一项都真的被
注册（`CapabilityHost.finish`），而  要求被禁用的工具从 registry 里消失。静态
的 `BUILTIN_MANIFESTS` 无法按配置少声明一项，因此 `enabled_tool_names()` 同时喂给两处：
`registration.setup()` 用它决定注册谁，装配根用它过滤 manifest 声明（`runtime/wiring.py`
的 `keep` 参数）。两处同源于**同一次** `resolve_settings()`，「声明了但不可用」因此在结构
上不可能发生——那正是  想要的同源。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from karyvia.contracts import ErrorCode, JsonValue, KaryviaError

# 契约把它留在子模块里（`contracts/__init__.py` 只转发类型，不转发这个上界），
# `tests/contracts/test_tool.py` 也是这么取的。
from karyvia.contracts.tool import MAX_TOOL_RESULT_LENGTH
from karyvia.sdk import PluginContext

__all__ = [
    "CONFIG_DISABLE_KEY",
    "CONFIG_MAX_ENTRIES_KEY",
    "CONFIG_MAX_MATCHES_KEY",
    "CONFIG_MAX_READ_BYTES_KEY",
    "CONFIG_MAX_RESULT_CHARS_KEY",
    "CONFIG_SKILL_ROOTS_KEY",
    "CONFIG_WORKSPACE_KEY",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_MAX_MATCHES",
    "DEFAULT_MAX_READ_BYTES",
    "DEFAULT_MAX_RESULT_CHARS",
    "TOOL_NAMES",
    "FsToolSettings",
    "enabled_tool_names",
    "resolve_settings",
]

#: 本包交付的工具名，**顺序即注册顺序**。
TOOL_NAMES: Final[tuple[str, ...]] = ("fs.read", "fs.write", "fs.edit", "fs.list", "fs.grep")

#: 六个配置键。manifest 的 `config_schema` 与这里必须一致，由测试对照。
CONFIG_WORKSPACE_KEY: Final = "workspace"
CONFIG_SKILL_ROOTS_KEY: Final = "skill_roots"
CONFIG_DISABLE_KEY: Final = "disable"
CONFIG_MAX_READ_BYTES_KEY: Final = "max_read_bytes"
CONFIG_MAX_RESULT_CHARS_KEY: Final = "max_result_chars"
CONFIG_MAX_ENTRIES_KEY: Final = "max_entries"
CONFIG_MAX_MATCHES_KEY: Final = "max_matches"

#: 单次从磁盘读取的字节上限。超出不是失败，是截断。
DEFAULT_MAX_READ_BYTES: Final = 1024 * 1024

#: 单个工具结果的字符上限。默认取契约上限的一半：`MAX_TOOL_RESULT_LENGTH` 是「构造
#: `ToolResult` 时不许超」的硬线，而一次工具调用就吃掉 64 KiB 上下文对任何模型都过分。
DEFAULT_MAX_RESULT_CHARS: Final = MAX_TOOL_RESULT_LENGTH // 2

#: `fs.list` 单次返回的条目数上限。
DEFAULT_MAX_ENTRIES: Final = 500

#: `fs.grep` 单次返回的匹配数上限。
DEFAULT_MAX_MATCHES: Final = 200


def _invalid(message: str, **detail: object) -> KaryviaError:
    return KaryviaError(ErrorCode.CONFIG_INVALID, message, detail=detail)


def _read_positive_int(config: Mapping[str, JsonValue], key: str, *, default: int) -> int:
    value = config.get(key)
    if value is None:
        return default
    # `bool` 是 `int` 的子类，而 `"max_entries": true` 显然是写错了而不是「上限为 1」。
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise _invalid("该配置项必须是正整数。", key=key, actual_type=type(value).__name__)
    return value


class FsToolSettings:
    """一份已校验的配置。构造后不可变，工具实现每次调用直接读它。

    刻意不是 dataclass：`disabled` 已经在 `resolve_settings()` 里核对过表外名字，用普通类
    可以让「校验只发生一次」在类型上成立——没有一个能绕过校验的公开构造路径（与
    `context_basic.BasicContextSettings` 同一做法）。
    """

    __slots__ = (
        "_disabled",
        "_max_entries",
        "_max_matches",
        "_max_read_bytes",
        "_max_result_chars",
        "_skill_roots",
        "_workspace",
    )

    def __init__(
        self,
        *,
        workspace: Path,
        disabled: frozenset[str],
        max_read_bytes: int,
        max_result_chars: int,
        max_entries: int,
        max_matches: int,
        skill_roots: tuple[Path, ...] = (),
    ) -> None:
        self._workspace = workspace
        self._disabled = disabled
        self._max_read_bytes = max_read_bytes
        self._max_result_chars = max_result_chars
        self._max_entries = max_entries
        self._max_matches = max_matches
        self._skill_roots = skill_roots

    @property
    def workspace(self) -> Path:
        """全部路径解析的根。"""
        return self._workspace

    @property
    def disabled(self) -> frozenset[str]:
        """被运维关掉的工具名（已核对过在 `TOOL_NAMES` 内）。"""
        return self._disabled

    @property
    def max_read_bytes(self) -> int:
        return self._max_read_bytes

    @property
    def max_result_chars(self) -> int:
        return self._max_result_chars

    @property
    def max_entries(self) -> int:
        return self._max_entries

    @property
    def max_matches(self) -> int:
        return self._max_matches

    @property
    def skill_roots(self) -> tuple[Path, ...]:
        """按优先级排列的 Skill 容器目录。"""
        return self._skill_roots

    @property
    def enabled(self) -> tuple[str, ...]:
        """本次该注册的工具名，保持 `TOOL_NAMES` 的顺序。"""
        return tuple(name for name in TOOL_NAMES if name not in self._disabled)


def _read_workspace(config: Mapping[str, JsonValue], ctx: PluginContext) -> Path:
    """决定 workspace 根：配置里的 `workspace`，否则退回插件私有状态目录。

    **本内建不知道实例布局**（`R4`），根只能由装配根交下来： 会把
    `ConfigDocument.workspace.root` 放进本插件的配置块。没配时退回 `ctx.state_dir`——那是
    每个插件都必然拥有的私有目录，比抛错更符合「插件在没有配置时也该能工作」，也比默默
    用进程 cwd 安全得多（与 `session_jsonl.resolve_directory` 相同原则）。
    """
    configured = config.get(CONFIG_WORKSPACE_KEY)
    if configured is None:
        return ctx.state_dir
    if not isinstance(configured, str) or not configured.strip():
        raise _invalid(
            "「workspace」必须是非空字符串。",
            key=CONFIG_WORKSPACE_KEY,
            actual_type=type(configured).__name__,
        )
    return Path(configured).expanduser()


def _read_disabled(config: Mapping[str, JsonValue]) -> frozenset[str]:
    """读 `disable`：一组工具名。表外的名字是错误，不是无害的多余配置。"""
    value = config.get(CONFIG_DISABLE_KEY)
    if value is None:
        return frozenset()
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise _invalid(
            "「disable」必须是字符串数组。",
            key=CONFIG_DISABLE_KEY,
            actual_type=type(value).__name__,
        )
    names = [item for item in value if isinstance(item, str)]
    if len(names) != len(value):
        raise _invalid("「disable」的每一项都必须是字符串。", key=CONFIG_DISABLE_KEY)
    unknown = sorted(set(names) - set(TOOL_NAMES))
    if unknown:
        raise _invalid(
            "「disable」里出现了本内建没有的工具名。",
            key=CONFIG_DISABLE_KEY,
            unknown=unknown,
            known=list(TOOL_NAMES),
        )
    return frozenset(names)


def resolve_settings(ctx: PluginContext) -> FsToolSettings:
    """把 `ctx.config` 校验成一份设置（只看得到自己那一块）。

    **异常约定**：类型不对、上限非正、`disable` 里有表外名字，一律抛 `CONFIG_INVALID`。
    `max_result_chars` 超过契约的 `MAX_TOOL_RESULT_LENGTH` 同样拒绝——放行它只会让每次
    调用都在构造 `ToolResult` 时才炸，而那时错误信息指向的是 kernel 而不是这行配置。
    """
    config = ctx.config
    max_result_chars = _read_positive_int(
        config, CONFIG_MAX_RESULT_CHARS_KEY, default=DEFAULT_MAX_RESULT_CHARS
    )
    if max_result_chars > MAX_TOOL_RESULT_LENGTH:
        raise _invalid(
            "结果上限不得超过契约允许的工具结果长度。",
            key=CONFIG_MAX_RESULT_CHARS_KEY,
            limit=MAX_TOOL_RESULT_LENGTH,
        )
    return FsToolSettings(
        workspace=_read_workspace(config, ctx),
        disabled=_read_disabled(config),
        max_read_bytes=_read_positive_int(
            config, CONFIG_MAX_READ_BYTES_KEY, default=DEFAULT_MAX_READ_BYTES
        ),
        max_result_chars=max_result_chars,
        max_entries=_read_positive_int(
            config, CONFIG_MAX_ENTRIES_KEY, default=DEFAULT_MAX_ENTRIES
        ),
        max_matches=_read_positive_int(
            config, CONFIG_MAX_MATCHES_KEY, default=DEFAULT_MAX_MATCHES
        ),
        skill_roots=_read_skill_roots(config),
    )


def _read_skill_roots(config: Mapping[str, JsonValue]) -> tuple[Path, ...]:
    value = config.get(CONFIG_SKILL_ROOTS_KEY)
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise _invalid("「skill_roots」必须是字符串数组。", key=CONFIG_SKILL_ROOTS_KEY)
    roots = [Path(item).expanduser() for item in value if isinstance(item, str) and item.strip()]
    if len(roots) != len(value):
        raise _invalid("「skill_roots」的每一项都必须是非空字符串。", key=CONFIG_SKILL_ROOTS_KEY)
    return tuple(roots)


def enabled_tool_names(config: Mapping[str, JsonValue]) -> tuple[str, ...]:
    """本次配置下该生效的工具名。装配根用它过滤 manifest 声明。

    只看配置、不碰 `ctx`：装配根在构造 `PluginContext` **之前**就要知道该声明哪几个能力，
    而这个问题只依赖 `disable` 一个键。`resolve_settings()` 与它读的是同一个函数
    （`_read_disabled`），因此两处不可能给出不同答案。

    **异常约定**：同 `resolve_settings()` 对 `disable` 的校验。
    """
    disabled = _read_disabled(config)
    return tuple(name for name in TOOL_NAMES if name not in disabled)
