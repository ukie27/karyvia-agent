"""实例布局、配置加载与实例锁（技术方案 §6.7、§10.1 步骤 1–2、§11）。

职责：re-export 本包各模块的公开表面，使调用方只需 `from karyvia.kernel.config
import ...` 一条导入路径；`load_config()` 是这里的主入口。
不负责：获取实例锁（`InstanceLock` 由 `runtime/bootstrap.py` 在生命周期里持有）、
读写实例目录里除 `config.json` 之外的任何文件、加载插件（`D25`）。

**本包一个字节都不写**（`EDG-501`）。`D24` 的 `scaffold.py` / `json_schema.py` 也不例外：
它们只**渲染**首次运行的配置与那份派生 JSON Schema，落盘在 `runtime/first_run.py`。

模块间依赖是单向的：`layout` / `process` / `merge` 互不相识，`lock` 只用 `process`，
`fields` 谁都不认识，`schema` 用 `fields` 与 `merge` 的 pointer 工具、`secrets` 只用后者，`sources` 只产出 `merge` 的层，
`loader` 编排全部。配置的四层优先级只在 `sources.collect_layers()` 的返回顺序里定义一次。

`secrets` 刻意不接进 `load_config()`：`SecretStr` 不是 `JsonValue`，塞进合并后的文档会让
`validate_config()` 无从校验；`${VAR}` 在加载路径上就是一个普通字符串。接线在 `D19`
（provider 凭据）与 `D26`（`ctx.secret()`）。
"""

from __future__ import annotations

from .fields import FieldKind, FieldSpec
from .json_schema import JSON_SCHEMA_DIALECT, JSON_SCHEMA_FILENAME, config_json_schema
from .layout import (
    CONFIG_FILENAME,
    DEFAULT_INSTANCE_NAME,
    INSTANCE_DIR_ENV,
    INSTANCE_NAME_ENV,
    INSTANCES_DIRNAME,
    KARYVIA_HOME_ENV,
    LOCK_FILENAME,
    LOGS_DIRNAME,
    PLUGINS_DIRNAME,
    SESSIONS_DIRNAME,
    WORKSPACE_DIRNAME,
    InstanceLayout,
)
from .loader import LoadedConfig, load_config
from .lock import InstanceLock, LockInfo, StaleLockReclaimed
from .merge import ConfigLayer, MergeResult, escape_pointer_token, merge_layers, pointer_of
from .process import Liveness, process_is_alive, process_started_at
from .scaffold import InitialConfig, build_initial_config, render_json
from .schema import (
    IGNORED_TOP_LEVEL_KEYS,
    MEMORY_ON_FAILURE_CHOICES,
    SCHEMA_KEY,
    SECTION_SPECS,
    SESSION_CONCURRENCY_CHOICES,
    ContextSection,
    HooksSection,
    KaryviaConfig,
    LoggingSection,
    MemorySection,
    ModelSection,
    PluginEntry,
    PluginsSection,
    RoutingSection,
    TurnSection,
    WorkspaceSection,
    defaults,
    validate_config,
)
from .secrets import (
    SECRET_REF_PATTERN,
    SecretMap,
    SecretRef,
    contains_secret_ref,
    prepare_for_write,
    resolve_secrets,
    resolve_text,
    scan_secret_refs,
    secret_ref_names,
)
from .sources import (
    CLI_ORIGIN,
    DEFAULT_ORIGIN,
    ENV_ORIGIN,
    ENV_PREFIX,
    FILE_ORIGIN,
    collect_layers,
    env_layer,
    file_layer,
    overrides_layer,
    parse_override,
    read_config_file,
)

__all__ = [
    "CLI_ORIGIN",
    "CONFIG_FILENAME",
    "DEFAULT_INSTANCE_NAME",
    "DEFAULT_ORIGIN",
    "ENV_ORIGIN",
    "ENV_PREFIX",
    "FILE_ORIGIN",
    "IGNORED_TOP_LEVEL_KEYS",
    "INSTANCE_DIR_ENV",
    "INSTANCE_NAME_ENV",
    "INSTANCES_DIRNAME",
    "JSON_SCHEMA_DIALECT",
    "JSON_SCHEMA_FILENAME",
    "LOCK_FILENAME",
    "LOGS_DIRNAME",
    "KARYVIA_HOME_ENV",
    "PLUGINS_DIRNAME",
    "SCHEMA_KEY",
    "SECRET_REF_PATTERN",
    "SECTION_SPECS",
    "SESSIONS_DIRNAME",
    "WORKSPACE_DIRNAME",
    "ConfigLayer",
    "ContextSection",
    "MEMORY_ON_FAILURE_CHOICES",
    "SESSION_CONCURRENCY_CHOICES",
    "FieldKind",
    "FieldSpec",
    "HooksSection",
    "InitialConfig",
    "InstanceLayout",
    "InstanceLock",
    "Liveness",
    "LoadedConfig",
    "LockInfo",
    "LoggingSection",
    "MergeResult",
    "MemorySection",
    "ModelSection",
    "KaryviaConfig",
    "PluginEntry",
    "PluginsSection",
    "RoutingSection",
    "SecretMap",
    "SecretRef",
    "StaleLockReclaimed",
    "TurnSection",
    "WorkspaceSection",
    "build_initial_config",
    "collect_layers",
    "config_json_schema",
    "contains_secret_ref",
    "defaults",
    "env_layer",
    "escape_pointer_token",
    "file_layer",
    "load_config",
    "merge_layers",
    "overrides_layer",
    "parse_override",
    "pointer_of",
    "prepare_for_write",
    "process_is_alive",
    "process_started_at",
    "read_config_file",
    "render_json",
    "resolve_secrets",
    "resolve_text",
    "scan_secret_refs",
    "secret_ref_names",
    "validate_config",
]
