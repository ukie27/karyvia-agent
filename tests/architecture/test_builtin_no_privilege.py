"""内建能力不享受特权的 AST 断言（`BAS-005`、`SDK-007`，技术方案 §8.3、§14）。

职责：对真实源码树断言 `builtins/` 与外部插件受同一套约束——不 import `karyvia.kernel.*`、
不持有自己的注册通道；声明为只读的内建连持久化的语法途径都没有。并用注入违规样例证明
每条守卫都会拦。
不负责：判定逻辑本身（见 `_boundaries.py`），也不导入被测模块。

**为什么这条要单独立一个文件**：`R4` 已经禁止 `builtins/` import `kernel/`，
`test_import_boundaries.py` 也已覆盖。但 `BAS-005` 说的是一件更具体、也更容易悄悄破掉的
事——**内建没有插件拿不到的注册路径**。一个只 import `sdk/` 的 `builtins/` 模块仍然可以
自己造 `RegistrationBatch`（那是 `kernel.registry` 的东西，`R4` 会拦），或者更隐蔽地，
在 `builtins/` 里写一套「内建专用」的注册辅助函数绕开 Host。第二类不违反任何依赖规则，
只能靠本文件的符号扫描拦下来。

`D17` 起 `BUILTIN_MANIFESTS` 不再为空，「每一项都是普通 manifest」那条因此真的在跑；
每加一个内建能力都会被重新检查一遍（`D18`–`D22`）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ._boundaries import collect_violations
from ._common import (
    PACKAGE_DIR,
    REPO_ROOT,
    SRC_DIR,
    iter_modules,
    make_package_tree,
    rel,
    write_module,
)

#: 只有 Kernel 才该碰的注册机关。`builtins/` 出现它们，就意味着内建绕开了 Host
#: （`R4` 会先一步拦下 import，但这条扫描盯的是「有没有人试图这么做」）。
_KERNEL_ONLY_SYMBOLS = frozenset(
    {"RegistrationBatch", "CapabilityRegistry", "CapabilityHost", "resolve_into"}
)

#: 声明为**只读**的内建包（相对 `builtins/` 的目录名）。`D18` 的 `context_basic` 是第一个：
#: 技术方案 §14 把「Context Provider 只读不写」列为职责划分风险项——一个顺手把检索结果
#: 存下来的 Provider 会让「谁拥有持久化」这件事重新变得说不清（`BAS-005`）。
#:
#: 判据是模块**根本没有 IO 的语法途径**，而不是「看起来没写盘」：不 import 这些模块，
#: 也不出现裸 `open`。`session_jsonl` 那样确实要写盘的内建不在这张表里，它如实声明
#: 自己拥有持久化职责。
_READ_ONLY_BUILTIN_PACKAGES = frozenset({"context_basic"})

#: 只读内建不得 import 的模块（顶层名）。覆盖文件、进程、网络与数据库四类持久化途径。
_IO_MODULES = frozenset(
    {
        "io",
        "os",
        "pathlib",
        "shelve",
        "shutil",
        "socket",
        "sqlite3",
        "subprocess",
        "tempfile",
        "urllib",
    }
)

#: 不需要 import 就能写盘的那个名字。
_IO_BUILTIN_NAMES = frozenset({"open"})

_BUILTINS_DIR = PACKAGE_DIR / "builtins"


def _builtin_modules() -> list[Path]:
    return iter_modules(_BUILTINS_DIR)


# --------------------------------------------------------------------------
# 正向：真实源码树
# --------------------------------------------------------------------------


def test_builtins_do_not_import_kernel() -> None:
    """`BAS-005` 的第一层：内建只能经 `sdk/` 说话，与外部插件同型。"""
    violations = [
        violation
        for violation in collect_violations(src_dir=SRC_DIR, repo_root=REPO_ROOT)
        if violation.path.startswith("src/karyvia/builtins/")
    ]
    assert not violations, "builtins/ 违反依赖规则：\n" + "\n".join(
        str(violation) for violation in violations
    )


def test_no_builtin_module_names_a_kernel_registration_symbol() -> None:
    """`SDK-007`：不存在内建专用注册路径。

    比依赖规则更严一档——`builtins/` 不该**提到**这些名字，哪怕只是在类型注解或字符串
    以外的地方引用。真要注册能力，唯一的路是 `setup(api)` 拿到的那个 Host。
    """
    offenders: list[str] = []
    for path in _builtin_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in _KERNEL_ONLY_SYMBOLS:
                offenders.append(f"{rel(path)}:{node.lineno}  {node.id}")
            elif isinstance(node, ast.Attribute) and node.attr in _KERNEL_ONLY_SYMBOLS:
                offenders.append(f"{rel(path)}:{node.lineno}  {node.attr}")
    assert not offenders, "builtins/ 试图自己走注册通道：\n" + "\n".join(offenders)


def test_every_builtin_manifest_is_an_ordinary_plugin_manifest() -> None:
    """内建与插件同型：同样声明 `capabilities`，同样要过 manifest 校验。"""
    source = (_BUILTINS_DIR / "registry.py").read_text(encoding="utf-8")
    assert "BUILTIN_MANIFESTS" in source
    # 不导入被测模块（本包的既定职责），改用 AST 确认它确实标成了 manifest 元组。
    tree = ast.parse(source)
    annotations = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "BUILTIN_MANIFESTS"
    ]
    assert annotations, "BUILTIN_MANIFESTS 必须有显式类型标注"
    assert "PluginManifest" in ast.unparse(annotations[0].annotation)


def _io_offenders(path: Path) -> list[str]:
    """一个模块里出现的全部 IO 途径：import 了持久化模块，或用了裸 `open`。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders += [
                f"{rel(path)}:{node.lineno}  import {alias.name}"
                for alias in node.names
                if alias.name.split(".")[0] in _IO_MODULES
            ]
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in _IO_MODULES:
                offenders.append(f"{rel(path)}:{node.lineno}  from {node.module}")
        elif isinstance(node, ast.Name) and node.id in _IO_BUILTIN_NAMES:
            offenders.append(f"{rel(path)}:{node.lineno}  {node.id}()")
    return offenders


@pytest.mark.parametrize("package", sorted(_READ_ONLY_BUILTIN_PACKAGES))
def test_read_only_builtins_have_no_syntactic_route_to_persistence(package: str) -> None:
    """只读内建不得持久化任何东西（技术方案 §14、`CTX-006` 的前提）。

    断言的是「没有途径」而不是「没有写」：一个 Provider 只要 import 了 `pathlib`，
    「它到底存不存东西」就变成了每次评审都要重看一遍的问题。
    """
    modules = iter_modules(_BUILTINS_DIR / package)
    assert modules, f"builtins/{package}/ 不存在或没有模块——这条守卫会空转"
    offenders = [entry for path in modules for entry in _io_offenders(path)]
    assert not offenders, f"只读内建 {package} 出现了持久化途径：\n" + "\n".join(offenders)


# --------------------------------------------------------------------------
# 反向：注入违规样例必须被拦下
# --------------------------------------------------------------------------
def test_injected_kernel_import_in_builtins_is_rejected(tmp_path: Path) -> None:
    """守卫真的会拦，而不是恒返回通过。"""
    src = make_package_tree(tmp_path)
    write_module(
        src,
        "karyvia/builtins/sneaky.py",
        "from karyvia.kernel.registry import CapabilityRegistry\n",
    )
    violations = collect_violations(src_dir=src, repo_root=tmp_path)
    assert {violation.rule for violation in violations} == {"R4"}


@pytest.mark.parametrize("symbol", sorted(_KERNEL_ONLY_SYMBOLS))
def test_the_symbol_scan_catches_each_forbidden_name(symbol: str, tmp_path: Path) -> None:
    """符号扫描对每个名字都真的敏感——这是「内建专用注册路径」唯一的挡板。"""
    module = tmp_path / "sneaky.py"
    module.write_text(f"def go():\n    return {symbol}()\n", encoding="utf-8")
    tree = ast.parse(module.read_text(encoding="utf-8"))
    found = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id in _KERNEL_ONLY_SYMBOLS
    }
    assert found == {symbol}


@pytest.mark.parametrize(
    "source",
    [
        "import os\n",
        "import os.path\n",
        "from pathlib import Path\n",
        "import sqlite3\n",
        "from urllib.request import urlopen\n",
        "def save(text):\n    with open('notes.txt', 'w') as handle:\n        handle.write(text)\n",
    ],
    ids=["os", "os.path", "pathlib", "sqlite3", "urllib", "bare-open"],
)
def test_the_io_scan_catches_each_persistence_route(source: str, tmp_path: Path) -> None:
    """只读扫描对每一类途径都真的敏感，而不是只认得 `import os`。"""
    module = tmp_path / "leaky.py"
    module.write_text(source, encoding="utf-8")
    assert _io_offenders(module), f"未被拦下的持久化途径：\n{source}"


def test_the_io_scan_accepts_the_pure_module_it_is_meant_to_allow(tmp_path: Path) -> None:
    """反过来也要证明它不是恒失败：一个纯内存模块必须干净通过。"""
    module = tmp_path / "pure.py"
    module.write_text(
        "import math\n\n\ndef estimate(text):\n    return math.ceil(len(text) / 3)\n",
        encoding="utf-8",
    )
    assert _io_offenders(module) == []
