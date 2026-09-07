"""`tools_shell` 的配置：workspace 根、超时、输出上限、环境变量与单工具禁用
。

职责：把 `ctx.config` 校验成一份不可变的 `ShellToolSettings`，并回答「这次该注册
`shell.exec` 吗」。
不负责：读实例布局（`R4` 禁止内建 import `kernel/`，根由装配根经 `ctx.config["workspace"]`
交下来）、执行命令（`process.py`）、构造环境变量（`environ.py`）、构造 argv（`command.py`）。

**工具名是单个常量**：本包只交付
`shell.exec`。`enabled_tool_names` 仍然存在——与 `tools_fs` 是同一条机制，
装配根用它过滤 manifest 声明，`setup()` 用同一份设置决定注册谁，两处同源于同一份配置。

**环境变量默认全部不继承**（「默认执行环境必须保守；扩大范围必须显式配置」）：
子进程只拿到 `environ.py` 里那份平台基线，父进程里的 `OPENAI_API_KEY` 之类根本没有到达
子进程的路径。要多给什么，运维得在 `pass_env` 里逐个写出名字——那正是「显式操作」。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Final

from karyvia.contracts import ErrorCode, JsonValue, KaryviaError
from karyvia.contracts.tool import MAX_TOOL_RESULT_LENGTH
from karyvia.sdk import PluginContext

__all__ = [
    "CONFIG_DISABLE_KEY",
    "CONFIG_ENV_KEY",
    "CONFIG_MAX_OUTPUT_CHARS_KEY",
    "CONFIG_PASS_ENV_KEY",
    "CONFIG_SHELL_KEY",
    "CONFIG_TIMEOUT_KEY",
    "CONFIG_WORKSPACE_KEY",
    "DEFAULT_MAX_OUTPUT_CHARS",
    "DEFAULT_TIMEOUT_MS",
    "TOOL_NAME",
    "ShellToolSettings",
    "enabled_tool_names",
    "resolve_settings",
]

#: 本包交付的唯一工具名。
TOOL_NAME: Final = "shell.exec"

#: 七个配置键。manifest 的 `config_schema` 与这里必须一致，由测试对照。
CONFIG_WORKSPACE_KEY: Final = "workspace"
CONFIG_DISABLE_KEY: Final = "disable"
CONFIG_TIMEOUT_KEY: Final = "timeout_ms"
CONFIG_MAX_OUTPUT_CHARS_KEY: Final = "max_output_chars"
CONFIG_PASS_ENV_KEY: Final = "pass_env"
CONFIG_ENV_KEY: Final = "env"
CONFIG_SHELL_KEY: Final = "shell"

#: 单次命令执行的默认超时（毫秒）。实际生效值还要与 `ToolInvocation.timeout_ms` 取较小者
#: ——Kernel 那一侧已经把 turn 剩余预算压进去了（见 `process.effective_timeout_ms`）。
DEFAULT_TIMEOUT_MS: Final = 120_000

#: 单个工具结果的字符上限。与 `tools_fs` 取同一个默认值（契约上限的一半）：
#: `MAX_TOOL_RESULT_LENGTH` 是「构造 `ToolResult` 时不许超」的硬线，而一次命令输出就吃掉
#: 64 KiB 上下文对任何模型都过分。
DEFAULT_MAX_OUTPUT_CHARS: Final = MAX_TOOL_RESULT_LENGTH // 2


def _invalid(message: str, **detail: object) -> KaryviaError:
    return KaryviaError(ErrorCode.CONFIG_INVALID, message, detail=detail)


def _read_positive_int(config: Mapping[str, JsonValue], key: str, *, default: int) -> int:
    value = config.get(key)
    if value is None:
        return default
    # `bool` 是 `int` 的子类，而 `"timeout_ms": true` 显然是写错了而不是「超时 1 毫秒」。
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise _invalid("该配置项必须是正整数。", key=key, actual_type=type(value).__name__)
    return value


def _read_name_list(config: Mapping[str, JsonValue], key: str) -> tuple[str, ...]:
    """读一组环境变量名。顺序不重要，但去重并排序让诊断输出稳定。"""
    value = config.get(key)
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise _invalid(
            "该配置项必须是字符串数组。", key=key, actual_type=type(value).__name__
        )
    names = [item for item in value if isinstance(item, str) and item]
    if len(names) != len(value):
        raise _invalid("该配置项的每一项都必须是非空字符串。", key=key)
    return tuple(sorted(set(names)))


def _read_env_overrides(config: Mapping[str, JsonValue]) -> Mapping[str, str]:
    """读显式的 `名字 → 值` 覆盖。值必须是字符串——子进程的环境里没有别的类型。"""
    value = config.get(CONFIG_ENV_KEY)
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise _invalid(
            "「env」必须是对象。", key=CONFIG_ENV_KEY, actual_type=type(value).__name__
        )
    overrides: dict[str, str] = {}
    for name, raw in value.items():
        if not name or not isinstance(raw, str):
            raise _invalid(
                "「env」的每个值都必须是字符串。", key=CONFIG_ENV_KEY, name=name
            )
        overrides[name] = raw
    return MappingProxyType(overrides)


def _read_disabled(config: Mapping[str, JsonValue]) -> bool:
    """读 `disable`：一组工具名。表外的名字是错误，不是无害的多余配置。

    形态与 `tools_fs` 保持一致（字符串数组）而不是做成布尔：两个内建工具包的运维写法应当
    一样，而「本包只有一个工具」是实现细节，不该渗进配置形态里。
    """
    value = config.get(CONFIG_DISABLE_KEY)
    if value is None:
        return False
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise _invalid(
            "「disable」必须是字符串数组。",
            key=CONFIG_DISABLE_KEY,
            actual_type=type(value).__name__,
        )
    names = [item for item in value if isinstance(item, str)]
    if len(names) != len(value):
        raise _invalid("「disable」的每一项都必须是字符串。", key=CONFIG_DISABLE_KEY)
    unknown = sorted(set(names) - {TOOL_NAME})
    if unknown:
        raise _invalid(
            "「disable」里出现了本内建没有的工具名。",
            key=CONFIG_DISABLE_KEY,
            unknown=unknown,
            known=[TOOL_NAME],
        )
    return bool(names)


def _read_workspace(config: Mapping[str, JsonValue], ctx: PluginContext) -> Path:
    """决定 workspace 根：配置里的 `workspace`，否则退回插件私有状态目录。

    **本内建不知道实例布局**（`R4`），根只能由装配根交下来： 会把
    `ConfigDocument.workspace.root` 放进本插件的配置块。没配时退回 `ctx.state_dir`——那是
    每个插件都必然拥有的私有目录，比抛错更符合「插件在没有配置时也该能工作」，也比默默
    用进程 cwd 安全得多（与 `tools_fs.resolve_settings` 相同原则）。
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


def _read_shell(config: Mapping[str, JsonValue]) -> str:
    """读 shell 程序覆盖。空串表示「按平台自动选」（见 `command.py`）。"""
    value = config.get(CONFIG_SHELL_KEY)
    if value is None:
        return ""
    if not isinstance(value, str) or not value.strip():
        raise _invalid(
            "「shell」必须是非空字符串。",
            key=CONFIG_SHELL_KEY,
            actual_type=type(value).__name__,
        )
    return value


class ShellToolSettings:
    """一份已校验的配置。构造后不可变，工具实现每次调用直接读它。

    刻意不是 dataclass：全部字段已经在 `resolve_settings()` 里校验过，用普通类可以让
    「校验只发生一次」在类型上成立——没有一个能绕过校验的公开构造路径（与
    `tools_fs.FsToolSettings` 同一做法）。
    """

    __slots__ = (
        "_disabled",
        "_env",
        "_max_output_chars",
        "_pass_env",
        "_shell",
        "_timeout_ms",
        "_workspace",
    )

    def __init__(
        self,
        *,
        workspace: Path,
        disabled: bool,
        timeout_ms: int,
        max_output_chars: int,
        pass_env: tuple[str, ...],
        env: Mapping[str, str],
        shell: str,
    ) -> None:
        self._workspace = workspace
        self._disabled = disabled
        self._timeout_ms = timeout_ms
        self._max_output_chars = max_output_chars
        self._pass_env = pass_env
        self._env = env
        self._shell = shell

    @property
    def workspace(self) -> Path:
        """命令的默认 cwd，也是 `cwd` 参数必须落在其内的根。"""
        return self._workspace

    @property
    def disabled(self) -> bool:
        """运维是否关掉了 `shell.exec`。"""
        return self._disabled

    @property
    def timeout_ms(self) -> int:
        return self._timeout_ms

    @property
    def max_output_chars(self) -> int:
        return self._max_output_chars

    @property
    def pass_env(self) -> tuple[str, ...]:
        """额外从父进程转发的环境变量名（运维显式列举）。"""
        return self._pass_env

    @property
    def env(self) -> Mapping[str, str]:
        """显式写死的环境变量。"""
        return self._env

    @property
    def shell(self) -> str:
        """shell 程序路径；空串表示按平台自动选。"""
        return self._shell

    @property
    def enabled(self) -> bool:
        """本次该注册 `shell.exec` 吗。"""
        return not self._disabled


def resolve_settings(ctx: PluginContext) -> ShellToolSettings:
    """把 `ctx.config` 校验成一份设置（只看得到自己那一块）。

    **异常约定**：类型不对、上限非正、`disable` 里有表外名字，一律抛 `CONFIG_INVALID`。
    `max_output_chars` 超过契约的 `MAX_TOOL_RESULT_LENGTH` 同样拒绝——放行它只会让每次
    调用都在构造 `ToolResult` 时才炸，而那时错误信息指向的是 kernel 而不是这行配置
    （与 `tools_fs` 同一条理由）。
    """
    config = ctx.config
    max_output_chars = _read_positive_int(
        config, CONFIG_MAX_OUTPUT_CHARS_KEY, default=DEFAULT_MAX_OUTPUT_CHARS
    )
    if max_output_chars > MAX_TOOL_RESULT_LENGTH:
        raise _invalid(
            "结果上限不得超过契约允许的工具结果长度。",
            key=CONFIG_MAX_OUTPUT_CHARS_KEY,
            limit=MAX_TOOL_RESULT_LENGTH,
        )
    return ShellToolSettings(
        workspace=_read_workspace(config, ctx),
        disabled=_read_disabled(config),
        timeout_ms=_read_positive_int(config, CONFIG_TIMEOUT_KEY, default=DEFAULT_TIMEOUT_MS),
        max_output_chars=max_output_chars,
        pass_env=_read_name_list(config, CONFIG_PASS_ENV_KEY),
        env=_read_env_overrides(config),
        shell=_read_shell(config),
    )


def enabled_tool_names(config: Mapping[str, JsonValue]) -> tuple[str, ...]:
    """本次配置下该生效的工具名。装配根用它过滤 manifest 声明。

    只看配置、不碰 `ctx`：装配根在构造 `PluginContext` **之前**就要知道该声明哪几个能力，
    而这个问题只依赖 `disable` 一个键。`resolve_settings()` 与它读的是同一个函数
    （`_read_disabled`），因此两处不可能给出不同答案。

    **异常约定**：同 `resolve_settings()` 对 `disable` 的校验。
    """
    return () if _read_disabled(config) else (TOOL_NAME,)
