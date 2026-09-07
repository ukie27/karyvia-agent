"""配置 schema 与校验。

职责：用一张声明式字段表定义**有哪些配置字段**、它们的默认值与所属小节，校验合并后的
JSON，并把每处问题连同 JSON Pointer 位置一起报出来；`TurnSection.to_limits()` 把 turn
小节转成 `TurnLimits`。
不负责：逐字段的类型校验积木（`fields.py`）、读取任何来源（`sources.py`）、分层合并
（`merge.py`）、决定 workspace 的最终绝对路径（`loader.py`）。

**不用 pydantic，手写校验。** 本模块仍实现未知字段拒绝和 JSON Pointer
错误位置，但避免在配置冷路径引入重型依赖。三个理由，按分量排序：

1.  要求每个生效值可追溯来源，这就要求默认值层**物化成一份 dict**（`defaults`）
   才能和其它层一样带上来源。默认值一旦是 dict，合并与来源追踪就已经全在 `merge.py` 里
   自己写了，pydantic 剩下的贡献只是校验十几个字段的类型。
2. pydantic 的 `ValidationError.loc` 是元组，仍要自己转成 RFC 6901；而 `sdk/manifest.py`
   的 `_format_location` 产出点分路径且**不能 import**（`R2`）。
3. 实测 `import pydantic` 约 90 ms、连带把 `kernel.config` 的导入推到 300 ms 以上，而
    给整个冷启动的预算就是 300 ms。配置加载在启动第 2 步、永远在路径上
   （`sdk/manifest.py` 只在真的要发现插件时才付这笔钱）。
   `test_loading_config_does_not_import_pydantic` 是这条约束的可执行形态。

**`detail` 里绝不放配置值**，只放指针、类型名与变量名：密钥可以出现在任何指针上
（`/plugins/acme/config/api_key`），而 `contracts.redact` 按**键名**判定，一个通用的
`{"value": ...}` 键正好绕过它。禁止回显原值让安全性不依赖字段名是否被识别为敏感键。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Mapping

from ...contracts import ErrorCode, KaryviaError
from . import plugin_blocks as blocks
from .defaults import (
    DEFAULT_CHANNEL_CONCURRENCY,
    DEFAULT_COMMAND_PREFIX,
    DEFAULT_CONTEXT_PROVIDER_TIMEOUT_MS,
    DEFAULT_DEDUP_CAPACITY,
    DEFAULT_DEDUP_TTL_MS,
    DEFAULT_INTERCEPTOR_TIMEOUT_MS,
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_MAX_TOOL_CALLS_PER_TURN,
    DEFAULT_MEMORY_FRAGMENT_PRIORITY,
    DEFAULT_MEMORY_ON_FAILURE,
    DEFAULT_MEMORY_RECALL_LIMIT,
    DEFAULT_MEMORY_RECALL_TIMEOUT_MS,
    DEFAULT_OBSERVER_TIMEOUT_MS,
    DEFAULT_PLUGIN_STOP_TIMEOUT_MS,
    DEFAULT_QUEUE_MAX_SIZE,
    DEFAULT_RETRY_BASE_DELAY_MS,
    DEFAULT_RETRY_EMPTY_RESPONSE,
    DEFAULT_RETRY_MAX_ATTEMPTS,
    DEFAULT_RETRY_MAX_DELAY_MS,
    DEFAULT_SESSION_CONCURRENCY,
    DEFAULT_TOOL_RESULT_MAX_BYTES,
    DEFAULT_TOOL_TIMEOUT_MS,
    DEFAULT_TURN_COMPACTOR,
    DEFAULT_TURN_COMPACTOR_TIMEOUT_MS,
    DEFAULT_TURN_TIMEOUT_MS,
    MEMORY_ON_FAILURE_CHOICES,
    SESSION_CONCURRENCY_CHOICES,
)
from .fields import (
    FieldKind,
    FieldSpec,
    bool_at,
    coerce_value,
    int_at,
    issue,
    opt_int_at,
    opt_str_at,
    str_at,
    str_tuple_at,
    suggest,
)
from .merge import pointer_of
from .plugin_blocks import PluginEntry

if TYPE_CHECKING:
    from ...contracts import JsonValue

__all__ = [
    "IGNORED_TOP_LEVEL_KEYS",
    "SCHEMA_KEY",
    "SECTION_SPECS",
    "MEMORY_ON_FAILURE_CHOICES",
    "SESSION_CONCURRENCY_CHOICES",
    "ContextSection",
    "HooksSection",
    "LoggingSection",
    "MemorySection",
    "ModelSection",
    "KaryviaConfig",
    "PluginEntry",
    "PluginsSection",
    "RetrySection",
    "RoutingSection",
    "TurnSection",
    "WorkspaceSection",
    "defaults",
    "validate_config",
]

#: 生成的 `config.json` 里那句 schema 引用。它**不是**配置字段：编辑器读它，
#: 运行期忽略它。
SCHEMA_KEY: Final = "$schema"

#: 顶层放行、但不参与校验的键。目前只有一个，而且它必须是**具名的一条**而不是
#: 「以 `$` 开头就放行」那种规则——后者会让任何拼错成 `$turn` 的小节静默消失。
#: 这是全项目第二处对未知键让路的地方，第一处是 `plugins` 小节里的插件 id。
IGNORED_TOP_LEVEL_KEYS: Final[tuple[str, ...]] = (SCHEMA_KEY,)

#: 全部已知字段。**这是 `extra="forbid"` 的唯一依据**：不在表里的键即未知字段。
#: 新字段只在此扩展，不要在别处另开一张表。
SECTION_SPECS: Final[Mapping[str, Mapping[str, FieldSpec]]] = {
    "turn": {
        "max_iterations": FieldSpec(FieldKind.POSITIVE_INT, DEFAULT_MAX_ITERATIONS),
        "max_tool_calls_per_turn": FieldSpec(
            FieldKind.POSITIVE_INT, DEFAULT_MAX_TOOL_CALLS_PER_TURN
        ),
        "tool_timeout_ms": FieldSpec(FieldKind.POSITIVE_INT, DEFAULT_TOOL_TIMEOUT_MS),
        "tool_result_max_bytes": FieldSpec(
            FieldKind.POSITIVE_INT, DEFAULT_TOOL_RESULT_MAX_BYTES
        ),
        "turn_timeout_ms": FieldSpec(FieldKind.POSITIVE_INT, DEFAULT_TURN_TIMEOUT_MS),
        "context_max_tokens": FieldSpec(FieldKind.OPTIONAL_POSITIVE_INT, None),
    },
    "workspace": {
        "root": FieldSpec(FieldKind.OPTIONAL_STR, None),
    },
    "routing": {
        "command_prefix": FieldSpec(FieldKind.STR, DEFAULT_COMMAND_PREFIX),
        "session_concurrency": FieldSpec(
            FieldKind.STR, DEFAULT_SESSION_CONCURRENCY, SESSION_CONCURRENCY_CHOICES
        ),
        "queue_max_size": FieldSpec(FieldKind.POSITIVE_INT, DEFAULT_QUEUE_MAX_SIZE),
        "dedup_capacity": FieldSpec(FieldKind.POSITIVE_INT, DEFAULT_DEDUP_CAPACITY),
        "dedup_ttl_ms": FieldSpec(FieldKind.POSITIVE_INT, DEFAULT_DEDUP_TTL_MS),
        "channel_concurrency": FieldSpec(FieldKind.POSITIVE_INT, DEFAULT_CHANNEL_CONCURRENCY),
    },
    "plugins": {
        "enabled": FieldSpec(FieldKind.STR_LIST, ()),
        "disable": FieldSpec(FieldKind.STR_LIST, ()),
        "stop_timeout_ms": FieldSpec(FieldKind.POSITIVE_INT, DEFAULT_PLUGIN_STOP_TIMEOUT_MS),
    },
    "hooks": {
        "observer_timeout_ms": FieldSpec(FieldKind.POSITIVE_INT, DEFAULT_OBSERVER_TIMEOUT_MS),
        "interceptor_timeout_ms": FieldSpec(
            FieldKind.POSITIVE_INT, DEFAULT_INTERCEPTOR_TIMEOUT_MS
        ),
    },
    "context": {
        "provider_timeout_ms": FieldSpec(
            FieldKind.POSITIVE_INT, DEFAULT_CONTEXT_PROVIDER_TIMEOUT_MS
        ),
        "turn_compactor": FieldSpec(FieldKind.STR, DEFAULT_TURN_COMPACTOR),
        "turn_compactor_timeout_ms": FieldSpec(
            FieldKind.POSITIVE_INT, DEFAULT_TURN_COMPACTOR_TIMEOUT_MS
        ),
    },
    "memory": {
        "provider": FieldSpec(FieldKind.OPTIONAL_STR, None),
        "recall_limit": FieldSpec(FieldKind.POSITIVE_INT, DEFAULT_MEMORY_RECALL_LIMIT),
        "recall_timeout_ms": FieldSpec(
            FieldKind.POSITIVE_INT, DEFAULT_MEMORY_RECALL_TIMEOUT_MS
        ),
        "fragment_priority": FieldSpec(
            FieldKind.POSITIVE_INT, DEFAULT_MEMORY_FRAGMENT_PRIORITY
        ),
        "on_failure": FieldSpec(
            FieldKind.STR, DEFAULT_MEMORY_ON_FAILURE, MEMORY_ON_FAILURE_CHOICES
        ),
    },
    "model": {
        "provider": FieldSpec(FieldKind.OPTIONAL_STR, None),
        "name": FieldSpec(FieldKind.OPTIONAL_STR, None),
    },
    "retry": {
        "max_attempts": FieldSpec(FieldKind.POSITIVE_INT, DEFAULT_RETRY_MAX_ATTEMPTS),
        "base_delay_ms": FieldSpec(FieldKind.POSITIVE_INT, DEFAULT_RETRY_BASE_DELAY_MS),
        "max_delay_ms": FieldSpec(FieldKind.POSITIVE_INT, DEFAULT_RETRY_MAX_DELAY_MS),
        "retry_empty_response": FieldSpec(FieldKind.BOOL, DEFAULT_RETRY_EMPTY_RESPONSE),
    },
    "logging": {
        "level": FieldSpec(FieldKind.STR, "info"),
        "file_enabled": FieldSpec(FieldKind.BOOL, True),
    },
}


#: 小节 dataclass 与 `KaryviaConfig` 位于 `sections.py`。这里原样再导出，保持配置包的稳定
#: 导入入口；字段声明的唯一来源仍是上面的 `SECTION_SPECS`。
from .sections import (  # noqa: E402 - 见上面那段注释，位置刻意在字段表之后
    ContextSection,
    HooksSection,
    KaryviaConfig,
    LoggingSection,
    MemorySection,
    ModelSection,
    PluginsSection,
    RetrySection,
    RoutingSection,
    TurnSection,
    WorkspaceSection,
)


def defaults() -> dict[str, JsonValue]:
    """把默认值物化成一层原始 JSON。

     的来源追踪要求默认值和其它层同形：只有这样「这个值取自默认值」才是一个
    可以回答的问题，而不是「查不到来源」的兜底解释。
    """
    return {
        section: {name: spec.default for name, spec in fields.items()}
        for section, fields in SECTION_SPECS.items()
    }


def _validate_section(
    name: str,
    raw: JsonValue,
    issues: list[KaryviaError],
) -> dict[str, JsonValue]:
    """校验一个小节，返回**已逐字段校验过**的取值映射（缺席的字段不出现在里面）。

    这里刻意不构造 dataclass：小节各有各的字段类型，泛型地 `**values` 展开会把每个字段
    都退化成 `JsonValue`。构造留给 `validate_config` 里五处具名调用，那里类型才收得回来。
    """
    specs = SECTION_SPECS[name]
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        issues.append(
            issue(ErrorCode.CONFIG_INVALID, "该小节必须是 JSON 对象。", pointer_of([name]))
        )
        return {}

    values: dict[str, JsonValue] = {}
    # `plugins` 小节里的未知键**不是**未知字段，而是插件 id。它们的形状由 `plugin_blocks.py` 校验，
    # 因此这里不能一并报成 `CONFIG_UNKNOWN_FIELD`。
    if name != blocks.PLUGINS_SECTION:
        for key in raw:
            if key not in specs:
                issues.append(
                    issue(
                        ErrorCode.CONFIG_UNKNOWN_FIELD,
                        f"未知配置字段。{suggest(key, list(specs))}",
                        pointer_of([name, key]),
                    )
                )
    for field_name, spec in specs.items():
        if field_name not in raw:
            continue
        adopted, problem = coerce_value(raw[field_name], spec, pointer_of([name, field_name]))
        if problem is not None:
            issues.append(problem)
            continue
        values[field_name] = adopted
    return values


def validate_config(data: Mapping[str, JsonValue]) -> KaryviaConfig:
    """校验合并后的配置。失败时抛错，**一次报出全部问题**。

    逐条抛出会让用户改一个键、重启、再看到下一个错误。因此先把全部问题收集起来，
    再用第一处的错误码抛出，完整清单挂在 `detail["errors"]`——单个未知字段因此仍然得到
    `CONFIG_UNKNOWN_FIELD`，而不是一个笼统的「配置无效」。

    **异常约定**：任何一处问题即抛 `KaryviaError`，`detail["errors"]` 的每项含
    `pointer` / `code` / `reason`；绝不含配置值本身。
    """
    issues: list[KaryviaError] = []

    for key in data:
        if key in IGNORED_TOP_LEVEL_KEYS:
            # `$schema` 是给编辑器的，不是配置字段。放行它是的显式决定：
            # 生成的初始配置引用一份派生 schema，若这里报未知字段，刚生成的文件下一次
            # 启动就会失败——那是最糟的首次体验。
            continue
        if key not in SECTION_SPECS:
            issues.append(
                issue(
                    ErrorCode.CONFIG_UNKNOWN_FIELD,
                    f"未知配置小节。{suggest(key, list(SECTION_SPECS))}",
                    pointer_of([key]),
                )
            )

    sections = {name: _validate_section(name, data.get(name), issues) for name in SECTION_SPECS}
    raw_plugins = data.get(blocks.PLUGINS_SECTION)
    plugin_entries = (
        blocks.validate_plugin_entries(raw_plugins, issues) if isinstance(raw_plugins, Mapping) else {}
    )

    if issues:
        errors: list[JsonValue] = [
            {
                "pointer": str(issue.detail.get("pointer", "")),
                "code": issue.code.value,
                "reason": issue.user_message,
            }
            for issue in issues
        ]
        raise KaryviaError(
            issues[0].code,
            f"配置校验失败，共 {len(errors)} 处问题。",
            detail={"errors": errors},
        )

    # 逐个具名构造而不是 `**sections`：小节的字段类型在这里才收窄回具体形状，
    # 展开一个 `dict[str, JsonValue]` 会让每个字段都退化成 `JsonValue`。
    turn = sections["turn"]
    routing = sections["routing"]
    hooks = sections["hooks"]
    plugins = sections["plugins"]
    memory = sections["memory"]
    model = sections["model"]
    retry = sections["retry"]
    logging_values = sections["logging"]
    return KaryviaConfig(
        turn=TurnSection(
            max_iterations=int_at(turn, "max_iterations", DEFAULT_MAX_ITERATIONS),
            max_tool_calls_per_turn=int_at(
                turn, "max_tool_calls_per_turn", DEFAULT_MAX_TOOL_CALLS_PER_TURN
            ),
            tool_timeout_ms=int_at(turn, "tool_timeout_ms", DEFAULT_TOOL_TIMEOUT_MS),
            tool_result_max_bytes=int_at(
                turn, "tool_result_max_bytes", DEFAULT_TOOL_RESULT_MAX_BYTES
            ),
            turn_timeout_ms=int_at(turn, "turn_timeout_ms", DEFAULT_TURN_TIMEOUT_MS),
            context_max_tokens=opt_int_at(turn, "context_max_tokens"),
        ),
        workspace=WorkspaceSection(root=opt_str_at(sections["workspace"], "root")),
        routing=RoutingSection(
            command_prefix=str_at(routing, "command_prefix", DEFAULT_COMMAND_PREFIX),
            session_concurrency=str_at(
                routing, "session_concurrency", DEFAULT_SESSION_CONCURRENCY
            ),
            queue_max_size=int_at(routing, "queue_max_size", DEFAULT_QUEUE_MAX_SIZE),
            dedup_capacity=int_at(routing, "dedup_capacity", DEFAULT_DEDUP_CAPACITY),
            dedup_ttl_ms=int_at(routing, "dedup_ttl_ms", DEFAULT_DEDUP_TTL_MS),
            channel_concurrency=int_at(
                routing, "channel_concurrency", DEFAULT_CHANNEL_CONCURRENCY
            ),
        ),
        hooks=HooksSection(
            observer_timeout_ms=int_at(
                hooks, "observer_timeout_ms", DEFAULT_OBSERVER_TIMEOUT_MS
            ),
            interceptor_timeout_ms=int_at(
                hooks, "interceptor_timeout_ms", DEFAULT_INTERCEPTOR_TIMEOUT_MS
            ),
        ),
        context=ContextSection(
            provider_timeout_ms=int_at(
                sections["context"], "provider_timeout_ms", DEFAULT_CONTEXT_PROVIDER_TIMEOUT_MS
            ),
            turn_compactor=str_at(
                sections["context"], "turn_compactor", DEFAULT_TURN_COMPACTOR
            ),
            turn_compactor_timeout_ms=int_at(
                sections["context"],
                "turn_compactor_timeout_ms",
                DEFAULT_TURN_COMPACTOR_TIMEOUT_MS,
            ),
        ),
        memory=MemorySection(
            provider=opt_str_at(memory, "provider"),
            recall_limit=int_at(memory, "recall_limit", DEFAULT_MEMORY_RECALL_LIMIT),
            recall_timeout_ms=int_at(
                memory, "recall_timeout_ms", DEFAULT_MEMORY_RECALL_TIMEOUT_MS
            ),
            fragment_priority=int_at(
                memory, "fragment_priority", DEFAULT_MEMORY_FRAGMENT_PRIORITY
            ),
            on_failure=str_at(memory, "on_failure", DEFAULT_MEMORY_ON_FAILURE),
        ),
        plugins=PluginsSection(
            enabled=str_tuple_at(plugins, "enabled"),
            disable=str_tuple_at(plugins, "disable"),
            stop_timeout_ms=int_at(plugins, "stop_timeout_ms", DEFAULT_PLUGIN_STOP_TIMEOUT_MS),
            entries=plugin_entries,
        ),
        model=ModelSection(
            provider=opt_str_at(model, "provider"),
            name=opt_str_at(model, "name"),
        ),
        retry=RetrySection(
            max_attempts=int_at(retry, "max_attempts", DEFAULT_RETRY_MAX_ATTEMPTS),
            base_delay_ms=int_at(retry, "base_delay_ms", DEFAULT_RETRY_BASE_DELAY_MS),
            max_delay_ms=int_at(retry, "max_delay_ms", DEFAULT_RETRY_MAX_DELAY_MS),
            retry_empty_response=bool_at(
                retry, "retry_empty_response", DEFAULT_RETRY_EMPTY_RESPONSE
            ),
        ),
        logging=LoggingSection(
            level=str_at(logging_values, "level", "info"),
            file_enabled=bool_at(logging_values, "file_enabled", True),
        ),
    )
