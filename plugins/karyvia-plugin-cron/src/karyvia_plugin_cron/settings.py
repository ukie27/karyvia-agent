"""配置解析与一次性校验（插件只看得见自己那一块）。

职责：把 `plugins.cron.config` 变成一个不可变设置对象，并把时区名解析集中在这里。
不负责：调度（`schedule.py`）、存储（`store.py`）、能力注册（`__init__.py`）。

**时区名的解析只有这一处。** `expr.py` 与 `schedule.py` 只接受已经解析好的 `tzinfo`，
因此它们不依赖 tzdata，测试树也就不依赖 tzdata（Windows 上没有系统时区库）。
解析器是可注入的（`TzResolver`），DST 用例因此能用手写的 `tzinfo` 子类驱动，
验的是真的 DST 行为而不是「本机装没装 tzdata」。

**没有「任务存哪个后端」这个配置项。** 与 `plugins/…-memory` 拒掉 `backend:` 表是同一条
判断：换存储的正规做法是装另一个声明 `overrides` 的插件。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime, tzinfo
from typing import Final

from karyvia.contracts import ErrorCode, JsonValue, KaryviaError
from karyvia.sdk import ManifestJsonSchema

__all__ = [
    "CONFIG_SCHEMA",
    "DEFAULT_CATCH_UP_WINDOW_MS",
    "DEFAULT_INSTANCE_ID",
    "DEFAULT_MAX_JOBS",
    "DEFAULT_MIN_INTERVAL_MS",
    "DEFAULT_TICK_CEILING_MS",
    "JOBS_DIR_NAME",
    "CronSettings",
    "TzResolver",
    "resolve_settings",
    "resolve_zone",
    "system_timezone",
    "zoneinfo_resolver",
]

#: `ctx.state_dir` 下的默认子目录名。
JOBS_DIR_NAME: Final = "cron"

#: 睡眠上界。调度循环本来会精确睡到下一次到期时刻，这个上界只用来兜住系统时钟跳变、
#: 休眠唤醒与 DST——没有它，一次向前跳表会让循环睡过头。
DEFAULT_TICK_CEILING_MS: Final = 60_000

#: 间隔任务的下界。`every_seconds: 1` 会让实例每秒开一条 turn，把 session 队列打满之后
#: 连人敲的消息都进不来。
DEFAULT_MIN_INTERVAL_MS: Final = 10_000

#: 补跑窗口，默认 0 = 不补（见 `schedule.due_decision`）。
DEFAULT_CATCH_UP_WINDOW_MS: Final = 0

#: 任务条数上界。整份文件每次保存都重写，而任务表本该是几十条的量级。
DEFAULT_MAX_JOBS: Final = 100

#: 实例标识的默认值。与其他官方 Channel 使用相同基准。
DEFAULT_INSTANCE_ID: Final = "default"

_NOT_A_STRING: Final = "这个配置项必须是字符串。"
_NOT_A_POSITIVE_INT: Final = "这个配置项必须是正整数。"
_NOT_A_NON_NEGATIVE_INT: Final = "这个配置项必须是非负整数。"
_UNKNOWN_TIMEZONE: Final = "配置里的时区名无法解析；请使用 IANA 名（如 Asia/Shanghai）。"
_TICK_TOO_SMALL: Final = "tick_ceiling_ms 太小了，会让调度循环空转。"

#: `tick_ceiling_ms` 的下界。低于它的取值除了空转不产生任何收益。
_MIN_TICK_CEILING_MS: Final = 1_000

#: 名字 → 时区。默认实现是 `zoneinfo.ZoneInfo`；注入点见模块 docstring。
TzResolver = Callable[[str], tzinfo]

#: manifest 的 `config_schema`。它校验**形状**，`resolve_settings()` 校验它表达不了的
#: 那些（下界、时区名是否真的解析得出来）。
#: 标注成 `ManifestJsonSchema` 而不是 `contracts.JsonSchema`：契约那个类型进不了
#: pydantic 模型（会 `RecursionError`），细节见 `sdk/manifest.py::ManifestJsonValue`。
CONFIG_SCHEMA: Final[ManifestJsonSchema] = {
    "type": "object",
    "properties": {
        "dir": {
            "type": "string",
            "description": "jobs.json 的落点。相对路径按插件状态目录解析，留空即 <state_dir>/cron。",
        },
        "timezone": {
            "type": "string",
            "description": (
                "cron 表达式的默认时区（IANA 名，如 Asia/Shanghai）。"
                "留空即使用本机时区；单条任务可以自带 tz 覆盖它。"
            ),
        },
        "tick_ceiling_ms": {
            "type": "integer",
            "minimum": _MIN_TICK_CEILING_MS,
            "description": "调度循环的睡眠上界，用来兜住系统时钟跳变。",
        },
        "min_interval_ms": {
            "type": "integer",
            "minimum": 1,
            "description": "间隔任务允许的最小间隔。",
        },
        "catch_up_window_ms": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "停机期间错过的运行，落在这个窗口内的补跑一次（不逐次补齐）。0 即从不补跑。"
            ),
        },
        "max_jobs": {
            "type": "integer",
            "minimum": 1,
            "description": "任务条数上界。",
        },
        "instance_id": {
            "type": "string",
            "description": (
                "注入消息上标注的实例标识。与其他官方 Channel 使用同名同默认值"
                "——插件拿不到装配根的实例标识（`PluginContext` 没有这个成员）。"
            ),
        },
    },
    "additionalProperties": False,
}


def system_timezone() -> tzinfo:
    """本机时区。`datetime.now().astimezone()` 是标准库里唯一不需要 tzdata 的取法。"""
    zone = datetime.now().astimezone().tzinfo
    if zone is None:  # pragma: no cover - astimezone() 恒返回带时区的结果
        raise KaryviaError(ErrorCode.CONFIG_INVALID, _UNKNOWN_TIMEZONE)
    return zone


@dataclass(frozen=True, slots=True)
class CronSettings:
    """解析并校验过的配置。"""

    directory: str = ""
    #: 已经解析好的默认时区。**存 `tzinfo` 而不是名字**：名字解析失败要在 `setup()`
    #: 报出来，而不是等到某条任务第一次算下一次时刻。
    timezone: tzinfo = dataclass_field(default_factory=system_timezone)
    timezone_name: str = ""
    tick_ceiling_ms: int = DEFAULT_TICK_CEILING_MS
    min_interval_ms: int = DEFAULT_MIN_INTERVAL_MS
    catch_up_window_ms: int = DEFAULT_CATCH_UP_WINDOW_MS
    max_jobs: int = DEFAULT_MAX_JOBS
    #: 注入消息上标注的实例标识。默认值与其他官方 Channel 相同（`"default"`）——
    #: `PluginContext` 没有实例标识这个成员，因此各插件都从自己的配置里读。
    instance_id: str = DEFAULT_INSTANCE_ID


def zoneinfo_resolver(name: str) -> tzinfo:
    """默认解析器。**惰性 import `zoneinfo`**：没配时区的实例不该为它付启动开销。"""
    from zoneinfo import ZoneInfo

    return ZoneInfo(name)


def resolve_settings(
    config: Mapping[str, JsonValue], *, tz_resolver: TzResolver = zoneinfo_resolver
) -> CronSettings:
    """把插件配置变成 `CronSettings`。**异常约定**：不合法抛 `CONFIG_INVALID`。

    **时区在这里就解析**，因此「配置里写了个不存在的时区」表现为 `karyvia plugins` 里一行
    `PLUGIN_LOAD_FAILED`，而不是三天后某条任务安静地不再触发。
    """
    name = _text(config, "timezone")
    tick = _positive_int(config, "tick_ceiling_ms", DEFAULT_TICK_CEILING_MS)
    if tick < _MIN_TICK_CEILING_MS:
        raise KaryviaError(
            ErrorCode.CONFIG_INVALID,
            _TICK_TOO_SMALL,
            detail={"key": "tick_ceiling_ms", "minimum": _MIN_TICK_CEILING_MS},
        )
    return CronSettings(
        directory=_text(config, "dir"),
        timezone=_resolve_timezone(name, tz_resolver),
        timezone_name=name,
        tick_ceiling_ms=tick,
        min_interval_ms=_positive_int(config, "min_interval_ms", DEFAULT_MIN_INTERVAL_MS),
        catch_up_window_ms=_non_negative_int(
            config, "catch_up_window_ms", DEFAULT_CATCH_UP_WINDOW_MS
        ),
        max_jobs=_positive_int(config, "max_jobs", DEFAULT_MAX_JOBS),
        instance_id=_text(config, "instance_id") or DEFAULT_INSTANCE_ID,
    )


def resolve_zone(name: str, settings: CronSettings, *, tz_resolver: TzResolver) -> tzinfo:
    """一条任务该用哪个时区：自带的 `tz` 优先，没有就用配置里的默认。

    **异常约定**：名字解析不出来抛 `INPUT_MALFORMED`——这里的名字来自工具或命令参数，
    是输入问题而不是配置问题。
    """
    if not name:
        return settings.timezone
    try:
        return tz_resolver(name)
    except Exception as error:  # noqa: BLE001 - 第三方解析器的异常类型不在契约里
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            _UNKNOWN_TIMEZONE,
            detail={"timezone": name, "cause": type(error).__name__},
        ) from error


def _resolve_timezone(name: str, tz_resolver: TzResolver) -> tzinfo:
    if not name:
        return system_timezone()
    try:
        return tz_resolver(name)
    except Exception as error:  # noqa: BLE001 - 见 `resolve_zone`
        raise KaryviaError(
            ErrorCode.CONFIG_INVALID,
            _UNKNOWN_TIMEZONE,
            detail={"key": "timezone", "value": name, "cause": type(error).__name__},
        ) from error


def _text(config: Mapping[str, JsonValue], key: str) -> str:
    value = config.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise KaryviaError(ErrorCode.CONFIG_INVALID, _NOT_A_STRING, detail={"key": key})
    return value.strip()


def _positive_int(config: Mapping[str, JsonValue], key: str, default: int) -> int:
    value = config.get(key)
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise KaryviaError(ErrorCode.CONFIG_INVALID, _NOT_A_POSITIVE_INT, detail={"key": key})
    return value


def _non_negative_int(config: Mapping[str, JsonValue], key: str, default: int) -> int:
    value = config.get(key)
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise KaryviaError(
            ErrorCode.CONFIG_INVALID, _NOT_A_NON_NEGATIVE_INT, detail={"key": key}
        )
    return value
