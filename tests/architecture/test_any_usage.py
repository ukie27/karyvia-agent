"""`Any` 只允许出现在归一化边界。

职责：断言新层与插件中每处 `Any` 所在行或其上方注释带 `# boundary:` 说明，
并用注入样例证明无标注的 `Any` 会被拦。
不负责：判断该处 `Any` 是否**真的**位于边界——`# boundary:` 是评审抓手，
守卫只保证它存在且写明了理由。

`typing.Any` 向核心泄漏是类型系统失效的主要途径（AGENTS.md 约束 6）。
边界（channel wire payload、provider SDK 对象、持久化记录）确实需要它，
因此规则不是禁用，而是「必须显式标注为边界」。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ._common import (
    NEW_LAYERS,
    PACKAGE_DIR,
    REPO_ROOT,
    iter_modules,
    plugin_package_roots,
    rel,
    write_module,
)

BOUNDARY_MARKER = "# boundary:"


def _any_nodes(tree: ast.Module) -> list[int]:
    """收集所有 `Any` 引用的行号（`Any`、`typing.Any`、`t.Any` 均算）。"""
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "Any":
            lines.append(node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr == "Any":
            lines.append(node.lineno)
    return lines


def _is_annotated(source_lines: list[str], lineno: int) -> bool:
    """`# boundary:` 写在同一行行尾，或紧邻上方的注释行。"""
    index = lineno - 1
    if index >= len(source_lines):
        return False
    if BOUNDARY_MARKER in source_lines[index]:
        return True
    # 向上跳过连续注释行，任意一行带标记即认可（允许多行说明）。
    cursor = index - 1
    while cursor >= 0 and source_lines[cursor].lstrip().startswith("#"):
        if BOUNDARY_MARKER in source_lines[cursor]:
            return True
        cursor -= 1
    return False


def _import_of_any_only(source_lines: list[str], lineno: int) -> bool:
    """`from typing import Any` 这一行本身不是使用点，不要求标注。"""
    stripped = source_lines[lineno - 1].strip()
    return stripped.startswith(("import ", "from ")) and "import" in stripped


def unannotated_any(path: Path, *, repo_root: Path) -> list[tuple[str, int]]:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []
    source_lines = source.splitlines()
    relative = rel(path, root=repo_root)
    return [
        (relative, lineno)
        for lineno in sorted(set(_any_nodes(tree)))
        if not _import_of_any_only(source_lines, lineno)
        and not _is_annotated(source_lines, lineno)
    ]


def scan(
    package_dir: Path,
    *,
    repo_root: Path,
    plugin_roots: list[Path] | None = None,
) -> list[tuple[str, int]]:
    """返回 [(文件, 行号)]。空目录返回空列表。"""
    offenders: list[tuple[str, int]] = []
    for layer in NEW_LAYERS:
        for path in iter_modules(package_dir / layer):
            offenders.extend(unannotated_any(path, repo_root=repo_root))
    for root in plugin_roots or []:
        for path in iter_modules(root):
            offenders.extend(unannotated_any(path, repo_root=repo_root))
    return offenders


def test_new_layers_and_plugins_have_no_unannotated_any() -> None:
    offenders = scan(
        PACKAGE_DIR,
        repo_root=REPO_ROOT,
        plugin_roots=plugin_package_roots(REPO_ROOT),
    )
    assert not offenders, "`Any` 未标注为归一化边界（需 `# boundary: 理由`）：\n" + "\n".join(
        f"  {path}:{lineno}" for path, lineno in offenders
    )


def test_empty_tree_passes(tmp_path: Path) -> None:
    assert scan(tmp_path / "karyvia", repo_root=tmp_path) == []


@pytest.mark.parametrize(
    ("label", "source"),
    [
        ("裸 Any 注解", "from typing import Any\n\n\ndef f(payload: Any) -> None: ...\n"),
        ("限定名 Any", "import typing\n\n\ndef f(payload: typing.Any) -> None: ...\n"),
        ("容器内 Any", "from typing import Any\n\nrecord: dict[str, Any] = {}\n"),
    ],
)
def test_inject_unannotated_any_is_rejected(label: str, source: str, tmp_path: Path) -> None:
    """注入无标注 `Any` 必须失败——证明守卫真的会拦。"""
    package = tmp_path / "karyvia"
    write_module(package, "kernel/turn/engine.py", source)
    assert scan(package, repo_root=tmp_path), f"{label} 的样例没有被拦下"


@pytest.mark.parametrize(
    ("label", "source"),
    [
        (
            "行尾标注",
            "from typing import Any\n\n\n"
            "def parse(payload: Any) -> None:  # boundary: Telegram wire payload\n"
            "    ...\n",
        ),
        (
            "上方注释标注",
            "from typing import Any\n\n"
            "# boundary: openai SDK 返回的 chunk 在此归一化\n"
            "chunk: Any = None\n",
        ),
    ],
)
def test_annotated_any_passes(label: str, source: str, tmp_path: Path) -> None:
    package = tmp_path / "karyvia"
    write_module(package, "builtins/model_openai/stream.py", source)
    assert scan(package, repo_root=tmp_path) == [], label


def test_import_line_alone_needs_no_marker(tmp_path: Path) -> None:
    """`from typing import Any` 本身不是使用点。"""
    package = tmp_path / "karyvia"
    write_module(package, "kernel/x.py", "from typing import Any  # noqa: F401\n")
    assert scan(package, repo_root=tmp_path) == []
