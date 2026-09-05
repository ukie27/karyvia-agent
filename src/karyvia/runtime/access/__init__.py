"""插件资源服务：`ctx.fs` / `ctx.net` / `ctx.shell` 的生产实现。

职责：re-export `paths`（路径守卫）、`files`（读写分离的文件门面）、`shell`（受限子进程）、
`net`（带 SSRF 守卫的出网）的公开表面。
不负责：给模型提供工具（`builtins/tools_fs`、`builtins/tools_shell`）或隔离插件进程。

**为什么落在 `runtime/` 而不是 `kernel/plugins/`**：这三个门面的返回类型
（`HttpResponse` / `ShellResult`）在 `sdk/api.py`，而 `R2` 禁止 `kernel/` import `sdk/`。
把它们下沉到 `contracts/` 是另一条路（`CliEntry`、`SecretStr` 的先例），但那两次下沉是因为
**kernel 要调用**那些类型；这里 kernel 一次也不碰它们，为一个只有 `runtime/` 用得到的
返回值去动已冻结的契约表面不划算。`runtime/` 本来就是全项目唯一同时看得见两边的层，
`plugin_context.py` 与 `introspection.py` 是同一档的先例。

**这些门面不是进程隔离**：同进程 Python 插件可以绕过它们直接 `import os` / `import httpx`。
它们的价值是复用安全、可测试的常用实现，而不是控制插件权限。
每个模块的 docstring 各自写明了自己挡不住什么（TOCTOU、DNS 重绑定、cwd 之外的绝对路径）。
"""

from __future__ import annotations

from .files import GuardedFileAccess
from .net import MAX_REDIRECTS, GuardedHttpAccess, address_is_blocked
from .paths import RESERVED_DEVICE_NAMES, PathGuard
from .shell import BASELINE_NAMES, DEFAULT_GRACE_MS, MAX_OUTPUT_CHARS, GuardedShellAccess

__all__ = [
    "BASELINE_NAMES",
    "DEFAULT_GRACE_MS",
    "MAX_OUTPUT_CHARS",
    "MAX_REDIRECTS",
    "RESERVED_DEVICE_NAMES",
    "GuardedFileAccess",
    "GuardedHttpAccess",
    "GuardedShellAccess",
    "PathGuard",
    "address_is_blocked",
]
