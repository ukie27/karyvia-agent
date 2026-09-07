"""SDK 公开表面的快照测试。

`sdk.__all__` 是**规范性清单**：不在其中的名字不提供兼容承诺。这里对它做字面量快照，
手动增删一个导出就会失败，从而强制走评审——这正是 §12.3 点名的三个架构测试之一。
"""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

import karyvia.sdk as sdk
import karyvia.sdk.testing as sdk_testing
from karyvia.contracts import CAPABILITY_ARITY, CapabilityKind
from karyvia.sdk import KaryviaAPI, PluginContext
from karyvia.sdk.api import EventSubscriber, FileAccess, HttpAccess, ShellAccess

#: `karyvia.sdk` 的规范性清单。改这张表 = 改兼容承诺。
SDK_PUBLIC_NAMES: Final[tuple[str, ...]] = (
    "SDK_VERSION",
    "CapabilityDecl",
    "EventHandler",
    "EventSubscriber",
    "FileAccess",
    "HttpAccess",
    "HttpResponse",
    "KaryviaAPI",
    #  新增：manifest 的 `config_schema` 所用的 JSON Schema 类型。它**必须**在这张
    # 表里——`contracts.JsonSchema` 进不了 pydantic 模型（见 `sdk/manifest.py`），
    # 而插件写 `CONFIG_SCHEMA` 时需要一个有兼容承诺的名字可标注。
    "ManifestJsonSchema",
    "PluginContext",
    "PluginManifest",
    "ShellAccess",
    "ShellResult",
    "is_compatible",
    "parse_manifest",
)

#: `karyvia.sdk.testing` 同样是公开面：它是插件作者的验收工具（§12.3）。
SDK_TESTING_PUBLIC_NAMES: Final[tuple[str, ...]] = (
    "ECHO_SPEC",
    "FAKE_MODEL_ID",
    "ChannelContract",
    "ContextProviderContract",
    "EchoTool",
    "FakeCliEntry",
    "FakeInstanceView",
    "FakeMemoryProvider",
    "FakeModelProvider",
    "FakePluginContext",
    "FakeTurnControl",
    "InMemorySessionStore",
    "ManualCancel",
    "MemoryProviderContract",
    "ModelProviderContract",
    "NullChannel",
    "RecordingEventSubscriber",
    "RecordingHook",
    "SessionStoreContract",
    "StaticContextProvider",
    "StaticTurnContextCompactor",
    "ToolContract",
    "TurnContextCompactorContract",
    "make_correlation",
    "text_response",
    "tool_call_response",
)

#: 10 个注册方法与 `CapabilityKind` 的 10 个取值一一对应。
#: 用字面量写死而不是从实现反推：从实现反推的测试只能证明代码没改，
#: 证明不了公开契约保持稳定。
REGISTRATION_METHODS: Final[dict[CapabilityKind, str]] = {
    CapabilityKind.TOOL: "register_tool",
    CapabilityKind.COMMAND: "register_command",
    CapabilityKind.CONTEXT: "register_context_provider",
    CapabilityKind.TURN_COMPACTOR: "register_turn_compactor",
    CapabilityKind.MODEL: "register_model_provider",
    CapabilityKind.CHANNEL: "register_channel",
    CapabilityKind.MEMORY: "register_memory_provider",
    CapabilityKind.SESSION_STORE: "register_session_store",
    CapabilityKind.CLI_ENTRY: "register_cli_entry",
    CapabilityKind.HOOK: "on",
}

#: `sdk/api.py` 里的 Protocol 与其成员快照。
API_PROTOCOLS: Final[dict[type, frozenset[str]]] = {
    KaryviaAPI: frozenset({"ctx", *REGISTRATION_METHODS.values()}),
    PluginContext: frozenset(
        {"plugin_id", "config", "state_dir", "logger", "events", "on_start", "add_cleanup",
         "spawn_task", "fs", "net", "shell", "secret", "instance", "turns"}
    ),
    #  补上二进制读写：否则需要二进制的插件无法复用 Workspace 路径与错误语义。
    FileAccess: frozenset(
        {"read_text", "write_text", "read_bytes", "write_bytes", "list_dir"}
    ),
    HttpAccess: frozenset({"request"}),
    ShellAccess: frozenset({"run"}),
    EventSubscriber: frozenset({"subscribe"}),
}


def _members(protocol: type) -> frozenset[str]:
    return frozenset(name for name in vars(protocol) if not name.startswith("_"))


# ------------------------------------------------------------------------ __all__ 快照


def test_sdk_all_matches_the_snapshot() -> None:
    assert tuple(sdk.__all__) == SDK_PUBLIC_NAMES


def test_sdk_testing_all_matches_the_snapshot() -> None:
    assert tuple(sdk_testing.__all__) == SDK_TESTING_PUBLIC_NAMES


@pytest.mark.parametrize("name", SDK_PUBLIC_NAMES)
def test_every_exported_name_actually_exists(name: str) -> None:
    """`__all__` 里写了但导不出来的名字，比漏写还糟。"""
    assert hasattr(sdk, name)


def _convention_order(names: tuple[str, ...]) -> list[str]:
    """仓库既有的 `__all__` 排序：常量、类型、函数三组，组内按 ASCII 序。

    与 `contracts/__init__.py` 一致（`UNTRUSTED_DATA_PREFIX` 排在 `ArtifactRef` 之前，
    因此不是朴素的 `sorted()`）。
    """

    def group(name: str) -> int:
        if name.isupper():
            return 0
        return 1 if name[0].isupper() else 2

    return sorted(names, key=lambda name: (group(name), name))


def test_all_follows_the_repository_ordering_convention() -> None:
    """排序固定，否则 diff 里「加了一个导出」会淹没在重排里。"""
    assert list(sdk.__all__) == _convention_order(SDK_PUBLIC_NAMES)
    assert list(sdk_testing.__all__) == _convention_order(SDK_TESTING_PUBLIC_NAMES)


def test_contract_types_are_not_re_exported() -> None:
    """契约类型只有一个进口：`karyvia.contracts`（`R4` 允许插件直接依赖它）。

    转发一份会让「同一个类型有两个进口」，还会让 SDK 快照跟着契约层漂移。
    """
    leaked = [name for name in ("ToolSpec", "ModelRequest", "SessionKey") if hasattr(sdk, name)]
    assert not leaked, f"契约类型被转发到 sdk：{leaked}"


# -------------------------------------------------------------------------- KaryviaAPI


def test_registration_method_count_is_ten() -> None:
    """注册方法恰好 10 个，与 `CapabilityKind` 一一对应。"""
    assert len(REGISTRATION_METHODS) == 10
    assert set(REGISTRATION_METHODS) == set(CapabilityKind)
    assert set(REGISTRATION_METHODS) == set(CAPABILITY_ARITY)


def test_karyvia_api_surface_is_exactly_ctx_plus_ten_methods() -> None:
    assert _members(KaryviaAPI) == frozenset({"ctx", *REGISTRATION_METHODS.values()})


def test_register_cli_entry_exists() -> None:
    """CLI 入口可被插件覆盖，因此它必须有注册路径。"""
    assert hasattr(KaryviaAPI, "register_cli_entry")


@pytest.mark.parametrize(
    ("protocol", "expected"),
    list(API_PROTOCOLS.items()),
    ids=[protocol.__name__ for protocol in API_PROTOCOLS],
)
def test_api_protocol_surface_matches_snapshot(protocol: type, expected: frozenset[str]) -> None:
    assert _members(protocol) == expected


@pytest.mark.parametrize(
    "protocol", list(API_PROTOCOLS), ids=[protocol.__name__ for protocol in API_PROTOCOLS]
)
def test_api_protocols_are_runtime_checkable(protocol: type) -> None:
    assert getattr(protocol, "_is_runtime_protocol", False)


def test_every_api_method_documents_its_exception_contract() -> None:
    """与 `contracts/protocols.py` 同一条规则：方法必须写明异常约定（§12.1）。

    只读属性豁免——它们没有异常可抛，约定写在所属 Protocol 的 docstring 里。
    """
    readonly_properties = {
        "KaryviaAPI.ctx",
        "PluginContext.plugin_id",
        "PluginContext.config",
        "PluginContext.state_dir",
        "PluginContext.logger",
        "PluginContext.events",
        "PluginContext.fs",
        "PluginContext.net",
        "PluginContext.shell",
        # ：只读诊断视图与 turn 控制面也必须列入只读属性快照。
        "PluginContext.instance",
        "PluginContext.turns",
    }
    missing = [
        f"{protocol.__name__}.{name}"
        for protocol in API_PROTOCOLS
        for name in sorted(_members(protocol))
        if "**异常约定**" not in (inspect.getdoc(getattr(protocol, name)) or "")
    ]
    assert sorted(missing) == sorted(readonly_properties)


def test_api_module_contains_no_implementation() -> None:
    """`api.py` 只有签名：函数体一律是 docstring + `...`，**没有例外**。

    `SecretStr` 在  迁到 `contracts/errors.py` 之后，本模块剩下的两个纯数据类型
    （`HttpResponse` / `ShellResult`）都没有方法，因此白名单是空集——`allowed` 里再出现
    名字，就说明有实现漏进了这一层。
    """
    from karyvia.sdk import api

    allowed: set[str] = set()
    source = Path(inspect.getfile(api)).read_text(encoding="utf-8")
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name in allowed:
            continue
        body = [stmt for stmt in node.body if not _is_docstring(stmt)]
        if len(body) != 1 or not _is_ellipsis(body[0]):
            offenders.append(node.name)
    assert not offenders, f"api.py 中出现了实现：{offenders}"


def _is_docstring(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def _is_ellipsis(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and stmt.value.value is Ellipsis
    )


# ---------------------------------------------------------------------------- 导入形态


def test_importing_sdk_does_not_pull_in_the_testing_kit() -> None:
    """夹具只在测试期需要；包根导入它等于让每个插件启动都付这份开销。"""
    probe = (
        "import sys; import karyvia.sdk; "
        "print('karyvia.sdk.testing' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"
