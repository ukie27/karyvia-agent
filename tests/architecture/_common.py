"""架构守卫的共享只读工具。

职责：定位源码树、枚举模块文件、把文件路径换算为导入路径，并为反向样例提供
临时源码树构造器。
不负责：定义任何具体规则——规则写在各 `test_*.py` 中，且必须能作用于任意根目录，
否则「注入违规样例必须失败」的反向测试无从构造。
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
PACKAGE_DIR = SRC_DIR / "karyvia"

#: 五层 + `embed`。`D35` 删掉 `legacy/` 之后，这就是包里的全部内容。
NEW_LAYERS: tuple[str, ...] = (
    "contracts",
    "kernel",
    "sdk",
    "builtins",
    "runtime",
    "embed",
)

#: 生成物与缓存目录，扫描时跳过。
IGNORED_DIRS = frozenset({"__pycache__", ".venv", "node_modules", "dist", "build", ".mypy_cache"})


def iter_modules(root: Path) -> list[Path]:
    """递归收集 `root` 下的 `.py` 文件；`root` 不存在时返回空列表。

    空目录返回空列表而不是报错，是 `D01` 的验收前提：新层此时多为空骨架。
    """
    if not root.is_dir():
        return []
    return [
        path
        for path in sorted(root.rglob("*.py"))
        if path.is_file()
        and not any(part in IGNORED_DIRS for part in path.relative_to(root).parts)
    ]


def dotted_path(path: Path, *, src_dir: Path) -> str:
    """把模块文件换算成导入路径，`__init__.py` 归属为包自身。

    `src/karyvia/kernel/turn/engine.py` -> `karyvia.kernel.turn.engine`
    `src/karyvia/kernel/__init__.py`    -> `karyvia.kernel`
    """
    parts = list(path.resolve().relative_to(src_dir.resolve()).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def rel(path: Path, *, root: Path = REPO_ROOT) -> str:
    """相对 `root` 的 POSIX 路径，消除 Windows 与 Linux 的分隔符差异。"""
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def plugin_package_roots(repo_root: Path = REPO_ROOT) -> list[Path]:
    """收集 `plugins/` 与 `examples/plugins/` 下的各插件目录。

    插件各自独立发行，规则与 `builtins/` 相同（`R4`）。目录尚不存在或只有说明
    文档时返回空列表。
    """
    roots: list[Path] = []
    for parent in (repo_root / "plugins", repo_root / "examples" / "plugins"):
        if not parent.is_dir():
            continue
        roots.extend(
            child
            for child in sorted(parent.iterdir())
            if child.is_dir() and child.name not in IGNORED_DIRS
        )
    return roots


def write_module(root: Path, relative: str, source: str) -> Path:
    """在临时树中写出一个模块，自动补齐父目录。反向样例专用。"""
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    return target


def make_package_tree(root: Path, layers: tuple[str, ...] = NEW_LAYERS) -> Path:
    """在 `root` 下构造 `src/karyvia/<layer>/__init__.py` 骨架，返回 `src/`。"""
    src = root / "src"
    write_module(src, "karyvia/__init__.py", '"""fixture package."""\n')
    for layer in layers:
        write_module(src, f"karyvia/{layer}/__init__.py", f'"""fixture {layer}."""\n')
    return src
