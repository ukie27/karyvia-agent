"""`session-memory` 的测试：与内建 JSONL 存储**共用同一套** `SessionStoreContract`。

职责：证明本插件的会话存储满足契约，以及 manifest 的覆盖声明写对了。
不负责：验证覆盖真的生效（那要装一个实例，在宿主仓库的 `tests/e2e/test_plugin_runtime.py`）。

`tests/builtins/test_session_jsonl.py` 里的内建实现继承的是**同一个基类**。两份实现被同一
批断言检查，这就是「可替换」的可执行形态——而不是一句承诺。
"""

from __future__ import annotations

from karyvia_plugin_session_memory import (
    CAPABILITY_NAME,
    MANIFEST,
    OVERRIDE_TARGET,
    MemorySessionStore,
)

from karyvia.contracts import CapabilityKind, SessionStore
from karyvia.sdk.testing import SessionStoreContract


class TestMemorySessionStore(SessionStoreContract):
    def make_store(self) -> SessionStore:
        return MemorySessionStore()


def test_the_manifest_declares_a_singleton_override() -> None:
    """覆盖必须写在 manifest 里，而且只能有这一条。"""
    (decl,) = MANIFEST.capabilities
    assert decl.kind is CapabilityKind.SESSION_STORE
    assert decl.name == CAPABILITY_NAME
    assert decl.overrides == OVERRIDE_TARGET


def test_the_override_target_decodes_to_the_builtin_store() -> None:
    """覆盖目标串只用 `parse_capability_target()` 解码，两侧共用同一个函数。"""
    from karyvia.contracts import Builtin, parse_capability_target

    provider, name = parse_capability_target(OVERRIDE_TARGET)
    assert provider == Builtin()
    assert name == "jsonl"
