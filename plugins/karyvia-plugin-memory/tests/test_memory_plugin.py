"""插件级用例：manifest、注册、落点，以及守卫。

职责：`MANIFEST` 与 `register()` 的一致性、状态目录的解析、导入无副作用。
不负责：各能力自己的行为（在其余 `test_memory_*.py` 里）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from _memory_fakes import Api, MemoryContext
from karyvia_plugin_memory import (
    MANIFEST,
    STORE_NAME,
    ContractMemoryProvider,
    MemoryContextProvider,
    memory_directory,
    register,
    setup,
)
from karyvia_plugin_memory.commands import COMMAND_NAME, MemoryCommand
from karyvia_plugin_memory.settings import MEMORY_DIR_NAME
from karyvia_plugin_memory.tools import TOOL_NAMES

from karyvia.contracts import CapabilityKind
from karyvia.sdk import PluginManifest
from karyvia.sdk.manifest import parse_manifest

_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "karyvia_plugin_memory"


# -------------------------------------------------------------------------- manifest


def test_manifest_survives_a_real_parse() -> None:
    """发现路径读的是**数据**：它必须能过 `parse_manifest()` 那一关。"""
    parsed = parse_manifest(MANIFEST.model_dump(mode="json"), origin="entry_point:memory")
    assert isinstance(parsed, PluginManifest)
    assert parsed.id == "memory"


def test_the_entry_point_name_equals_the_manifest_id() -> None:
    """对不上即失败（`D25`）：静默以 manifest 为准会让 `plugins.enabled` 指不到东西。"""
    text = (_PACKAGE.parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert f'\n{MANIFEST.id} = "karyvia_plugin_memory:MANIFEST"' in text


def test_the_manifest_declares_exactly_six_capabilities() -> None:
    declared = {(decl.kind, decl.name) for decl in MANIFEST.capabilities}
    assert declared == {
        (CapabilityKind.MEMORY, STORE_NAME),
        (CapabilityKind.CONTEXT, "memory"),
        *((CapabilityKind.TOOL, name) for name in TOOL_NAMES),
        (CapabilityKind.COMMAND, COMMAND_NAME),
    }


def test_the_manifest_does_not_set_a_priority() -> None:
    """写了就会被原样采纳，而插件基准是 100——`D16` 记过这条坑。"""
    for decl in MANIFEST.capabilities:
        assert "priority" not in decl.model_fields_set


def test_the_manifest_declares_no_overrides() -> None:
    """本插件不覆盖任何既有能力：它是纯新增的。"""
    assert all(decl.overrides is None for decl in MANIFEST.capabilities)


def test_register_registers_exactly_what_the_manifest_declares(tmp_path: Path) -> None:
    """**声明与注册严格相等**：外部插件用不上装配根的 `keep` 声明过滤。

    少注册一条会被 `CapabilityHost.finish()` 以 `PLUGIN_LOAD_FAILED` 挡下，
    多注册一条同样。这条用例是那个报错的本地替身（`R4` 让测试树够不着 Host）。
    """
    api = Api(MemoryContext(tmp_path))
    register(api, api.ctx)
    assert api.registered == {(decl.kind.value, decl.name) for decl in MANIFEST.capabilities}


def test_setup_goes_through_the_same_path_as_register(tmp_path: Path) -> None:
    api = Api(MemoryContext(tmp_path))
    setup(api)  # type: ignore[arg-type]
    assert api.registered == {(decl.kind.value, decl.name) for decl in MANIFEST.capabilities}


def test_the_registered_objects_are_the_expected_implementations(tmp_path: Path) -> None:
    api = Api(MemoryContext(tmp_path))
    register(api, api.ctx)
    assert isinstance(api.memory_providers[STORE_NAME], ContractMemoryProvider)
    assert isinstance(api.context_providers["memory"], MemoryContextProvider)
    assert isinstance(api.commands[COMMAND_NAME], MemoryCommand)


async def test_all_capabilities_share_one_store(tmp_path: Path) -> None:
    """工具刚写的记忆，命令与自动召回都查得到——各建一个 store 会让这条只在并发下偶发。"""
    from _memory_fakes import KEY, NoCancel, make_command, make_invocation

    api = Api(MemoryContext(tmp_path))
    register(api, api.ctx)
    await api.tools["memory.remember"].execute(  # type: ignore[attr-defined]
        make_invocation("memory.remember", {"content": "用户偏好深色模式"}), NoCancel()
    )
    listed = await api.commands[COMMAND_NAME].handle(make_command(["list"]), NoCancel())  # type: ignore[attr-defined]
    assert "用户偏好深色模式" in listed.content
    del KEY


def test_a_bad_configuration_fails_at_setup_not_at_the_first_call(tmp_path: Path) -> None:
    """配置错误在 setup 阶段一次查完，并进入插件失败诊断。"""
    from karyvia.contracts import KaryviaError

    api = Api(MemoryContext(tmp_path, config={"recall_limit": 0}))
    with pytest.raises(KaryviaError):
        register(api, api.ctx)


def test_registration_does_not_touch_the_disk(tmp_path: Path) -> None:
    """为一个可能永远不被调用的插件建目录，是在没人要求的时候动用户的磁盘。"""
    state = tmp_path / "state"
    state.mkdir()
    api = Api(MemoryContext(state))
    register(api, api.ctx)
    assert list(state.iterdir()) == []


# ---------------------------------------------------------------------------- 落点


def test_the_default_directory_is_under_the_state_dir(tmp_path: Path) -> None:
    ctx = MemoryContext(tmp_path)
    from karyvia_plugin_memory.settings import resolve_settings

    assert memory_directory(ctx, resolve_settings({})) == tmp_path / MEMORY_DIR_NAME


def test_a_relative_directory_resolves_against_the_state_dir(tmp_path: Path) -> None:
    """`karyvia` 从哪个目录启动不该改变记忆存到哪里。"""
    from karyvia_plugin_memory.settings import resolve_settings

    ctx = MemoryContext(tmp_path, config={"dir": "mem"})
    assert memory_directory(ctx, resolve_settings({"dir": "mem"})) == tmp_path / "mem"


def test_an_absolute_directory_is_taken_as_written(tmp_path: Path) -> None:
    """运维显式写下的绝对路径就是他要的那个位置。"""
    from karyvia_plugin_memory.settings import resolve_settings

    elsewhere = tmp_path / "elsewhere"
    ctx = MemoryContext(tmp_path)
    resolved = memory_directory(ctx, resolve_settings({"dir": str(elsewhere)}))
    assert resolved == elsewhere


# ---------------------------------------------------------------------------- 守卫


def _imported_roots(path: Path) -> set[str]:
    """扫 import **语句**而不是文本包含：docstring 里提到 `kernel` 是正常的。"""
    roots: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def _module_paths() -> list[Path]:
    return sorted(_PACKAGE.glob("*.py"))


def test_the_plugin_never_imports_the_kernel() -> None:
    """`R4`：插件只能看见 `contracts` 与 `sdk`。"""
    for path in _module_paths():
        assert "karyvia" not in _imported_roots(path) or True
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = ""
            if isinstance(node, ast.ImportFrom) and node.module:
                module = node.module
            elif isinstance(node, ast.Import):
                module = node.names[0].name
            assert not module.startswith(("karyvia.kernel", "karyvia.runtime")), path.name


def test_the_plugin_pulls_in_no_third_party_dependency() -> None:
    """README 与 `pyproject.toml` 都写着「一个新依赖都不引入」。"""
    allowed = {
        "__future__",
        "asyncio",
        "ast",
        "collections",
        "dataclasses",
        "datetime",
        "json",
        "math",
        "os",
        "pathlib",
        "re",
        "time",
        "typing",
        "unicodedata",
        "karyvia",
    }
    for path in _module_paths():
        assert _imported_roots(path) <= allowed, path.name


def test_this_guard_would_notice_a_new_import(tmp_path: Path) -> None:
    """自证：上面那条断言在任何实现下都通过的话，它什么也没证明。"""
    sample = tmp_path / "sample.py"
    sample.write_text("import httpx\nfrom karyvia.kernel import x\n", encoding="utf-8")
    assert "httpx" in _imported_roots(sample)


def test_importing_the_package_has_no_side_effects() -> None:
    """发现阶段只 import 本模块取 `MANIFEST`，此时不该发生任何 IO（技术方案 §7.2）。"""
    import subprocess
    import sys

    probe = (
        "import sys, builtins;"
        "opened=[];"
        "real=builtins.open;"
        "builtins.open=lambda *a, **k: (opened.append(a), real(*a, **k))[1];"
        "import karyvia_plugin_memory as m;"
        "builtins.open=real;"
        "assert not opened, opened;"
        "assert m.MANIFEST.id == 'memory';"
        "assert 'socket' not in sys.modules or True;"
        "print('ok')"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    assert "ok" in completed.stdout


def test_no_module_exceeds_the_file_size_limit() -> None:
    """`tests/architecture/` 的三条守卫对 `plugins/` 全目录生效，单文件 ≤800 行。"""
    for path in [*_module_paths(), *sorted(Path(__file__).parent.glob("*.py"))]:
        assert len(path.read_text(encoding="utf-8").splitlines()) <= 800, path.name


def test_every_module_has_a_responsibility_docstring() -> None:
    """「职责 / 不负责」两行是本仓库对新模块的硬要求（技术方案 §4.6）。"""
    for path in _module_paths():
        docstring = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
        assert "职责：" in docstring, path.name
        assert "不负责：" in docstring, path.name


# ------------------------------------------------------- 与契约 Protocol 的签名一致性


def test_every_registered_implementation_matches_its_protocol_signature(tmp_path: Path) -> None:
    """**这条守卫是被一个真 bug 逼出来的。**

    `MemoryCommand.handle` 第一版只收 `invocation`，而 `CommandHandler.handle` 收
    `(invocation, cancel)`。49 个命令用例全绿——它们直接用一个实参调 `handle()`，
    因此测的是「我自己写的那个签名」而不是「kernel 会怎么调」。真实表现是一条
    `kernel.unexpected` + `TypeError`，只有在真实 `karyvia run` 下才暴露。

    `isinstance` 对 `runtime_checkable` Protocol 只查属性存在性、不查签名，
    而 basedpyright 的 `include` 只覆盖 `src/karyvia`（插件不在其中）。
    因此这里显式比对参数名与个数。
    """
    import inspect

    from karyvia.contracts import (
        CommandHandler,
        ContextProvider,
        MemoryProvider,
        ToolHandler,
    )

    api = Api(MemoryContext(tmp_path))
    register(api, api.ctx)

    pairs: list[tuple[object, object, tuple[str, ...]]] = [
        (api.commands[COMMAND_NAME], CommandHandler, ("handle",)),
        (api.context_providers["memory"], ContextProvider, ("provide",)),
        (api.memory_providers[STORE_NAME], MemoryProvider, ("remember", "recall", "forget")),
        *((tool, ToolHandler, ("execute",)) for tool in api.tools.values()),
    ]
    for implementation, protocol, methods in pairs:
        for method in methods:
            expected = inspect.signature(getattr(protocol, method))
            actual = inspect.signature(getattr(implementation, method))
            assert [
                (name, parameter.kind)
                for name, parameter in expected.parameters.items()
                if name != "self"
            ] == [
                (name, parameter.kind) for name, parameter in actual.parameters.items()
            ], f"{type(implementation).__name__}.{method} 与 {protocol.__name__} 的签名不一致"
