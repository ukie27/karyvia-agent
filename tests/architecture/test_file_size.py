"""单文件行数阈值。

职责：断言新层与插件的单文件行数不超过阈值（`kernel/` ≤ 500，其他 ≤ 800），
并用注入超限样例证明守卫会拦。
不负责：函数行数与圈复杂度——那两项由 ruff 的 `PLR0915` 与 `C901` 检查。
"""

from __future__ import annotations

from pathlib import Path

from ._common import (
    NEW_LAYERS,
    PACKAGE_DIR,
    REPO_ROOT,
    iter_modules,
    plugin_package_roots,
    rel,
    write_module,
)

KERNEL_MAX_LINES = 500
DEFAULT_MAX_LINES = 800


def limit_for(layer: str) -> int:
    """`kernel/` 更严：它是机制层，长文件意味着机制在长成功能。"""
    return KERNEL_MAX_LINES if layer == "kernel" else DEFAULT_MAX_LINES


def _line_count(path: Path) -> int:
    return len(path.read_bytes().splitlines())


def oversized_modules(
    package_dir: Path,
    *,
    repo_root: Path,
    plugin_roots: list[Path] | None = None,
) -> list[tuple[str, int, int]]:
    """返回 [(文件, 行数, 阈值)]。空目录返回空列表。"""
    offenders: list[tuple[str, int, int]] = []

    for layer in NEW_LAYERS:
        limit = limit_for(layer)
        for path in iter_modules(package_dir / layer):
            count = _line_count(path)
            if count > limit:
                offenders.append((rel(path, root=repo_root), count, limit))

    for root in plugin_roots or []:
        for path in iter_modules(root):
            count = _line_count(path)
            if count > DEFAULT_MAX_LINES:
                offenders.append((rel(path, root=repo_root), count, DEFAULT_MAX_LINES))

    return offenders


def test_new_layers_and_plugins_are_within_line_limits() -> None:
    offenders = oversized_modules(
        PACKAGE_DIR,
        repo_root=REPO_ROOT,
        plugin_roots=plugin_package_roots(REPO_ROOT),
    )
    assert not offenders, "单文件行数超限：\n" + "\n".join(
        f"  {path}  {count} 行 > {limit}" for path, count, limit in offenders
    )


def test_empty_tree_passes(tmp_path: Path) -> None:
    assert oversized_modules(tmp_path / "karyvia", repo_root=tmp_path) == []


def test_inject_oversized_kernel_module_is_rejected(tmp_path: Path) -> None:
    """`kernel/` 的阈值是 500，注入 501 行必须被拦。"""
    package = tmp_path / "karyvia"
    write_module(package, "kernel/turn/engine.py", "x = 1\n" * (KERNEL_MAX_LINES + 1))

    offenders = oversized_modules(package, repo_root=tmp_path)
    assert [(count, limit) for _, count, limit in offenders] == [
        (KERNEL_MAX_LINES + 1, KERNEL_MAX_LINES)
    ]


def test_inject_oversized_other_layer_module_is_rejected(tmp_path: Path) -> None:
    """其他层阈值是 800；`kernel/` 的 500 不得误用到全部层。"""
    package = tmp_path / "karyvia"
    write_module(package, "builtins/tools_fs/read.py", "x = 1\n" * (DEFAULT_MAX_LINES + 1))
    write_module(package, "builtins/tools_fs/write.py", "x = 1\n" * (KERNEL_MAX_LINES + 1))

    offenders = oversized_modules(package, repo_root=tmp_path)
    assert [path for path, _, _ in offenders] == ["karyvia/builtins/tools_fs/read.py"]
