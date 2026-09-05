"""内建 Session 存储 `session_jsonl`：每 session 一个 JSONL + 一个 `meta.json`。

职责：作为本内建能力的公开门面，导出 `setup`（注册入口）、`JsonlSessionStore`（实现）
与格式编解码。
不负责：实现细节（在 `store.py` 与 `codec.py`）、声明自己（manifest 在
`builtins/registry.py`，那是内建能力唯一的发现来源）。

**这是内建能力的标准形态**：一份写在 `BUILTIN_MANIFESTS` 里的 `PluginManifest`，加一个
`setup(api)`。没有别的路——`tests/architecture/test_builtin_no_privilege.py` 会拦下任何
内建专用的注册通道（`BAS-005`、`SDK-007`）。

存储格式是**对外发布的契约**（`SES-006`），说明见
[`docs/session-storage.md`](../../../../docs/session-storage.md)。
"""

from __future__ import annotations

from .codec import (
    META_FIELDS,
    RECORD_FIELDS,
    SessionMeta,
    decode_meta,
    decode_record,
    encode_meta,
    encode_record,
)
from .store import (
    CAPABILITY_NAME,
    CONFIG_DIRECTORY_KEY,
    HISTORY_SUFFIX,
    META_SUFFIX,
    JsonlSessionStore,
    resolve_directory,
    setup,
)

__all__ = [
    "CAPABILITY_NAME",
    "CONFIG_DIRECTORY_KEY",
    "HISTORY_SUFFIX",
    "META_FIELDS",
    "META_SUFFIX",
    "RECORD_FIELDS",
    "JsonlSessionStore",
    "SessionMeta",
    "decode_meta",
    "decode_record",
    "encode_meta",
    "encode_record",
    "resolve_directory",
    "setup",
]
