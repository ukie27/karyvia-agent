"""模块头部「职责/不负责」两行的检查（技术方案 §4.6、§12.1）。

职责：断言 `contracts/`、`kernel/`、`sdk/`、`runtime/` 的每个模块首个 docstring
含「职责：」与「不负责：」两行，并用注入样例证明缺行会被拦。
不负责：检查 docstring 的内容是否属实——那是评审的事，守卫只保证抓手存在。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ._common import PACKAGE_DIR, REPO_ROOT, iter_modules, rel, write_module

#: 强制范围。`builtins/` 与 `embed/` 不在内（技术方案 §4.6 只列这四层）。
ENFORCED_LAYERS: tuple[str, ...] = ("contracts", "kernel", "sdk", "runtime")

RESPONSIBILITY_PREFIX = "职责："
NON_RESPONSIBILITY_PREFIX = "不负责："


def missing_header_lines(source: str) -> list[str]:
    """返回缺失的头部行前缀；合规时返回空列表。"""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"无法解析：{exc.msg}"]

    docstring = ast.get_docstring(tree, clean=True)
    if not docstring:
        return ["缺少模块 docstring"]

    lines = [line.strip() for line in docstring.splitlines()]
    missing: list[str] = []
    if not any(line.startswith(RESPONSIBILITY_PREFIX) for line in lines):
        missing.append(RESPONSIBILITY_PREFIX)
    if not any(line.startswith(NON_RESPONSIBILITY_PREFIX) for line in lines):
        missing.append(NON_RESPONSIBILITY_PREFIX)
    return missing


def check_tree(package_dir: Path, *, repo_root: Path) -> dict[str, list[str]]:
    """扫描强制范围内的全部模块，返回 {文件: 缺失项}。空目录返回空字典。"""
    offenders: dict[str, list[str]] = {}
    for layer in ENFORCED_LAYERS:
        for path in iter_modules(package_dir / layer):
            missing = missing_header_lines(path.read_text(encoding="utf-8"))
            if missing:
                offenders[rel(path, root=repo_root)] = missing
    return offenders


def test_enforced_layers_declare_responsibilities() -> None:
    offenders = check_tree(PACKAGE_DIR, repo_root=REPO_ROOT)
    assert not offenders, "模块头部缺少「职责/不负责」两行：\n" + "\n".join(
        f"  {path}  缺 {missing}" for path, missing in sorted(offenders.items())
    )


def test_empty_layer_passes(tmp_path: Path) -> None:
    """目标目录尚不存在时返回通过而非报错，否则 D01 自身无法验收。"""
    assert check_tree(tmp_path / "karyvia", repo_root=tmp_path) == {}


@pytest.mark.parametrize(
    ("label", "docstring"),
    [
        ("无 docstring", ""),
        ("只有摘要", '"""能力选择与覆盖解析。"""\n'),
        ("缺不负责", '"""摘要。\n\n职责：解析注册批次。\n"""\n'),
        ("缺职责", '"""摘要。\n\n不负责：加载插件。\n"""\n'),
    ],
)
def test_inject_missing_header_is_rejected(label: str, docstring: str, tmp_path: Path) -> None:
    """注入缺行样例必须失败——证明守卫真的会拦。"""
    package = tmp_path / "karyvia"
    write_module(package, "kernel/registry/resolve.py", docstring + "value = 1\n")

    offenders = check_tree(package, repo_root=tmp_path)
    assert offenders, f"{label} 的样例没有被拦下"


def test_compliant_header_passes(tmp_path: Path) -> None:
    package = tmp_path / "karyvia"
    write_module(
        package,
        "kernel/registry/resolve.py",
        '"""能力选择与覆盖解析。\n\n'
        "职责：把注册批次解析为最终生效实现，并产出可诊断报告。\n"
        "不负责：加载插件、执行能力、决定能力语义。\n"
        '"""\n',
    )
    assert check_tree(package, repo_root=tmp_path) == {}
