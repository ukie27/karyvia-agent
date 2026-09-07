"""第 3 层：插件 SDK，插件的唯一依赖面。

职责：暴露注册面 `KaryviaAPI`、受限运行时 `PluginContext`、manifest 数据类型与
`SDK_VERSION`；`__all__` 是对外的**规范性稳定清单**。
不负责：嵌入式调用（见 `karyvia.embed`）、宿主侧实现（见 `kernel/plugins/`）、
提供任何能力实现（那是 `builtins/` 与 `plugins/`）。

两条使用约定：

- **不在 `__all__` 里的名字不提供兼容承诺**。
  `tests/sdk/test_public_surface.py` 对它做快照断言，任何增删都会让测试失败，强制走评审。
- **契约类型从 `karyvia.contracts` 导入，本模块不转发**。`ToolSpec`、`ModelRequest`
  这些是插件与 Kernel 共享的数据契约，`R4` 明确允许插件直接依赖 `contracts/`；
  再转发一份只会制造「同一个类型有两个进口」的歧义，还会让本模块的快照跟着契约层漂移。
  `SecretStr` 也属于共享契约：配置层需要创建它，而 Kernel 不允许依赖 SDK。

`karyvia.sdk.testing` 是给插件作者的验收工具（Fake 与契约测试基类），**刻意不在这里
导入**：它只在测试期需要，让 `import karyvia.sdk` 顺带拉起一堆夹具没有道理
。用 `from karyvia.sdk.testing import ...` 显式获取。
"""

from __future__ import annotations

from .api import (
    EventHandler,
    EventSubscriber,
    FileAccess,
    HttpAccess,
    HttpResponse,
    KaryviaAPI,
    PluginContext,
    ShellAccess,
    ShellResult,
)
from .manifest import (
    CapabilityDecl,
    ManifestJsonSchema,
    PluginManifest,
    parse_manifest,
)
from .version import SDK_VERSION, is_compatible

__all__ = [
    "SDK_VERSION",
    "CapabilityDecl",
    "EventHandler",
    "EventSubscriber",
    "FileAccess",
    "HttpAccess",
    "HttpResponse",
    "KaryviaAPI",
    "ManifestJsonSchema",
    "PluginContext",
    "PluginManifest",
    "ShellAccess",
    "ShellResult",
    "is_compatible",
    "parse_manifest",
]
