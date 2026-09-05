"""依赖规则 `R1`–`R5` 的 AST 断言（技术方案 §3.1）。

职责：对真实源码树断言五条依赖规则，并用注入违规样例的反向测试证明守卫会拦。
不负责：规则的判定逻辑本身（见 `_boundaries.py`），也不导入被测模块。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ._boundaries import collect_violations
from ._common import (
    PACKAGE_DIR,
    REPO_ROOT,
    SRC_DIR,
    make_package_tree,
    plugin_package_roots,
    write_module,
)

# --------------------------------------------------------------------------
# 正向：真实源码树必须干净
# --------------------------------------------------------------------------


def test_real_tree_has_no_dependency_violations() -> None:
    violations = collect_violations(
        src_dir=SRC_DIR,
        repo_root=REPO_ROOT,
        plugin_roots=plugin_package_roots(REPO_ROOT),
    )
    assert not violations, "依赖规则违规：\n" + "\n".join(str(v) for v in violations)


def test_empty_layers_pass() -> None:
    """新层此时多为空骨架，守卫对空目录必须返回通过而不是报错。"""
    assert collect_violations(src_dir=SRC_DIR / "does-not-exist", repo_root=REPO_ROOT) == []


# --------------------------------------------------------------------------
# 反向：每条规则各有一个注入样例，必须被拦下
# --------------------------------------------------------------------------

# (规则号, 违规模块相对 src/ 的路径, 源码)
_INJECTED_VIOLATIONS: list[tuple[str, str, str]] = [
    (
        "R1",
        "karyvia/contracts/message.py",
        "from karyvia.kernel.registry import Registry\n",
    ),
    (
        "R2",
        "karyvia/kernel/turn/engine.py",
        "from karyvia.builtins.model_openai import Provider\n",
    ),
    (
        "R3",
        "karyvia/sdk/api.py",
        "from karyvia.kernel.config import load\n",
    ),
    (
        "R4",
        "karyvia/builtins/tools_fs/read.py",
        "from karyvia.kernel.registry import Registry\n",
    ),
    (
        "R5",
        "karyvia/embed/instance.py",
        "from karyvia.kernel import turn\nfrom karyvia.builtins import tools_fs\n",
    ),
]


@pytest.mark.parametrize(
    ("rule", "relative", "source"),
    _INJECTED_VIOLATIONS,
    ids=[rule for rule, _, _ in _INJECTED_VIOLATIONS],
)
def test_inject_violation_is_rejected(
    rule: str, relative: str, source: str, tmp_path: Path
) -> None:
    """注入违规样例必须失败——证明守卫真的会拦，而不是恒返回通过。"""
    src = make_package_tree(
        tmp_path,
        layers=("contracts", "kernel", "sdk", "builtins", "runtime", "embed"),
    )
    write_module(src, relative, source)

    violations = collect_violations(src_dir=src, repo_root=tmp_path)

    assert violations, f"{rule} 的违规样例没有被拦下：{relative}"
    assert rule in {v.rule for v in violations}, (
        f"样例应被判为 {rule}，实际判为 {sorted({v.rule for v in violations})}"
    )


def test_inject_plugin_importing_kernel_is_rejected(tmp_path: Path) -> None:
    """外部插件与 `builtins/` 同规则（`R4`）：只能 import sdk/ 与 contracts/。"""
    make_package_tree(tmp_path)
    plugin = tmp_path / "plugins" / "memory-sqlite"
    write_module(plugin, "karyvia_plugin_memory_sqlite/__init__.py", "")
    write_module(
        plugin,
        "karyvia_plugin_memory_sqlite/store.py",
        "from karyvia.kernel.registry import Registry\n",
    )

    violations = collect_violations(
        src_dir=tmp_path / "src", repo_root=tmp_path, plugin_roots=[plugin]
    )
    assert {v.rule for v in violations} == {"R4"}, violations


def test_relative_imports_are_resolved(tmp_path: Path) -> None:
    """相对导入必须解析为绝对路径，否则 `from ...builtins import x` 会绕过所有规则。"""
    src = make_package_tree(tmp_path, layers=("kernel", "builtins"))
    write_module(src, "karyvia/kernel/turn/__init__.py", "")
    write_module(src, "karyvia/kernel/turn/engine.py", "from ...builtins.tools_fs import read\n")

    violations = collect_violations(src_dir=src, repo_root=tmp_path)
    assert [v.rule for v in violations] == ["R2"], violations


def test_package_dir_is_where_the_guard_thinks_it_is() -> None:
    """路径常量走样会让所有断言静默变成空集合。"""
    assert PACKAGE_DIR.is_dir()
    assert (PACKAGE_DIR / "kernel").is_dir()
