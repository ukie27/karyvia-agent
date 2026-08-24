"""插件配置块的形状校验：`plugins.<plugin_id>.{config,secrets}`（技术方案 §6.7）。

职责：把 `plugins` 小节里那些**不是保留键**的条目解析成 `PluginEntry`，并在形状不对时
给出带 JSON Pointer 的问题；顺带回答「哪些插件 id 在配置里出现过」。
不负责：逐字段校验插件配置（那要用 manifest 自带的 `config_schema`，属阶段 A）、
解析 `${VAR}`（`secrets.py`，且解析发生在 `ctx.secret()` 调用时而不是加载时）、
决定谁被加载。

**从 `schema.py` 拆出来的分界线与 `fields.py` 那次相同**：`schema.py` 只放具体字段表，
本模块一个字段名都不认识——它认识的是「插件条目长什么样」这个**形状**。独立模块也避免
让具体字段表和插件条目解析共同挤压 Kernel 的 500 行上限。

**为什么是 `plugins.<id>` 而不是 `plugins.config.<id>`**：技术方案 §6.7 写死的形状是
`plugins.<plugin_id>.config`，`schema.py` 的模块 docstring 举的例子（`/plugins/acme/config/
api_key`）也是它。代价是保留键与插件 id 共用一个命名空间，因此一个叫 `disable` 的插件
无法配置——那被显式拒绝（`CONFIG_INVALID`）而不是静默当成保留键。

**`secrets` 与 `config` 分开是 `CFG-003` 的结构性保证**：凭据不在插件
自己的配置块里，`model-openai` 的 `config_schema` 因此根本没有 `api_key` 这个键，
`ctx.config` 交给插件的那份东西里也就没有可泄漏的东西。`secrets` 的值只能是 `${VAR}`
形态的字符串字面量，明文由 `ctx.secret()` 在调用时从环境变量取。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Mapping

from ...contracts import ErrorCode, NucleaError
from .fields import issue
from .merge import pointer_of

if TYPE_CHECKING:
    from ...contracts import JsonValue

__all__ = [
    "CONFIG_KEY",
    "ENTRY_KEYS",
    "NO_PLUGIN_ENTRIES",
    "PLUGINS_SECTION",
    "RESERVED_PLUGIN_KEYS",
    "SECRETS_KEY",
    "PluginEntry",
    "entries_to_json",
    "validate_plugin_entries",
]

#: 小节名。写成常量是因为它同时是 pointer 的第一段与保留键判定的宿主。
PLUGINS_SECTION: Final = "plugins"

#: `plugins` 小节里**不是**插件 id 的键。它们在 `SECTION_SPECS["plugins"]` 里有字段声明。
#: 保留键与插件 id 共用一个命名空间，但撞不上：插件 id 只允许小写字母、数字与中划线
#: （`sdk/manifest.py` 的 `_ID_CHARS`），带下划线的键名因此永远不是一个合法的插件 id
#: ——新增保留键时请沿用这条形状。
RESERVED_PLUGIN_KEYS: Final = ("enabled", "disable", "stop_timeout_ms")

#: 一个插件条目里的两个键。写成常量是因为 `json_schema.py` 要按名字给它们各
#: 派生一段 schema——两处各写一个字面量就会在改名时安静地对不上。
CONFIG_KEY: Final = "config"
SECRETS_KEY: Final = "secrets"

#: 一个插件条目允许的键。
ENTRY_KEYS: Final = (CONFIG_KEY, SECRETS_KEY)


#: 空块的共享实例。`field(default_factory=dict)` 会让类型退化成 `dict[Unknown, Unknown]`，
#: 而这两个字段是只读的——共享一个冻结的空映射既省掉那次退化，也挡住「改默认值」。
_EMPTY_CONFIG: Final[Mapping[str, JsonValue]] = MappingProxyType({})
_EMPTY_SECRETS: Final[Mapping[str, str]] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class PluginEntry:
    """一个插件在配置里的那一条。

    `config` 原样交给 `ctx.config`（`CFG-002`：插件只看得见自己那一块）。
    `secrets` 是名字到 `${VAR}` **字面量**的映射，`ctx.secret()` 调用时才解析——
    加载期解析等于把明文提前搬进进程里一份，而 `EDG-502` 要的是「缺哪个变量」这条诊断
    在真正取用时给出。

    """

    config: Mapping[str, JsonValue] = _EMPTY_CONFIG
    secrets: Mapping[str, str] = _EMPTY_SECRETS


#: 「一个插件条目都没配」的共享空表，`PluginsSection.entries` 的默认值。放在这里而不是
#: `schema.py`：那边只放具体字段，而这是插件条目的形状。
NO_PLUGIN_ENTRIES: Final[Mapping[str, "PluginEntry"]] = MappingProxyType({})


def _validate_secrets(
    plugin_id: str, raw: JsonValue, issues: list[NucleaError]
) -> dict[str, str]:
    pointer = pointer_of([PLUGINS_SECTION, plugin_id, "secrets"])
    if not isinstance(raw, Mapping):
        issues.append(issue(ErrorCode.CONFIG_INVALID, "secrets 必须是 JSON 对象。", pointer))
        return {}
    values: dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(value, str):
            issues.append(
                issue(
                    ErrorCode.CONFIG_INVALID,
                    "凭据引用必须是字符串（形如 ${OPENAI_API_KEY}）。",
                    pointer_of([PLUGINS_SECTION, plugin_id, "secrets", name]),
                )
            )
            continue
        values[name] = value
    return values


def _validate_entry(
    plugin_id: str, raw: JsonValue, issues: list[NucleaError]
) -> PluginEntry | None:
    pointer = pointer_of([PLUGINS_SECTION, plugin_id])
    if not isinstance(raw, Mapping):
        issues.append(
            issue(
                ErrorCode.CONFIG_INVALID,
                "插件条目必须是 JSON 对象，形如 {\"config\": {...}}。",
                pointer,
            )
        )
        return None

    for key in raw:
        if key not in ENTRY_KEYS:
            issues.append(
                issue(
                    ErrorCode.CONFIG_UNKNOWN_FIELD,
                    f"插件条目只接受 {'、'.join(ENTRY_KEYS)}。",
                    pointer_of([PLUGINS_SECTION, plugin_id, key]),
                )
            )

    config: dict[str, JsonValue] = {}
    raw_config = raw.get("config")
    if raw_config is not None and not isinstance(raw_config, Mapping):
        issues.append(
            issue(
                ErrorCode.CONFIG_INVALID,
                "config 必须是 JSON 对象。",
                pointer_of([PLUGINS_SECTION, plugin_id, "config"]),
            )
        )
    elif isinstance(raw_config, Mapping):
        config = {str(key): value for key, value in raw_config.items()}

    secrets = _validate_secrets(plugin_id, raw["secrets"], issues) if "secrets" in raw else {}
    return PluginEntry(config=config, secrets=secrets)


def entries_to_json(
    entries: Mapping[str, PluginEntry],
) -> dict[str, JsonValue]:
    """诊断视图里的插件条目（`NucleaConfig.to_json()` 用）。

    `secrets` 原样交出 `${VAR}` 字面量——那里从来就没有明文，`/config` 的脱敏因此是
    结构性成立的。按 id 排序，让 `nm config show` 的输出可比对。

    """
    return {
        plugin_id: {
            "config": dict(entry.config),
            "secrets": dict(entry.secrets),
        }
        for plugin_id, entry in sorted(entries.items())
    }


def validate_plugin_entries(
    raw: Mapping[str, JsonValue], issues: list[NucleaError]
) -> dict[str, PluginEntry]:
    """从 `plugins` 小节的原始映射里摘出插件条目。

    保留键被跳过（它们由 `SECTION_SPECS` 那条路校验）。**问题追加进 `issues` 而不是抛出**，
    与 `validate_config()` 的「一次报全」同构：改一个键、重启、再看到下一个错误不是可接受的
    启动体验。
    """
    entries: dict[str, PluginEntry] = {}
    for key, value in raw.items():
        if key in RESERVED_PLUGIN_KEYS:
            continue
        if not key or key.strip() != key:
            issues.append(
                issue(
                    ErrorCode.CONFIG_INVALID,
                    "插件 id 不能为空或带首尾空白。",
                    pointer_of([PLUGINS_SECTION, key]),
                )
            )
            continue
        entry = _validate_entry(key, value, issues)
        if entry is not None:
            entries[key] = entry
    return entries
