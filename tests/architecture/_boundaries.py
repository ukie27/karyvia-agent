"""依赖规则 `R1`–`R5` 的可复用检查器（技术方案 §3.1）。

职责：对任意源码树做 AST 扫描，收集 import 目标并判定其违反了哪条规则。
不负责：断言与夹具——检查器必须能作用于临时树，否则「注入违规样例必须失败」的
反向测试无从构造。

规则速查（依赖只允许自上而下）：

    R1  contracts/  不 import 本项目任何其他模块
    R2  kernel/     只能 import contracts/ 与 kernel/ 自身
    R3  sdk/        只 import contracts/
    R4  builtins/ 与外部插件  只能 import sdk/ 与 contracts/
    R5  只有 runtime/ 可同时 import kernel/ 与 builtins/（唯一组装根）

`R6`（新层禁止 import legacy/）随 `D35` 删掉 `legacy/` 一并退休：没有隔离区了，
规则也就没有可判定的对象。它服役期间只拦下过一处例外（`D31` 删掉的 legacy_entry）。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from ._common import IGNORED_DIRS, NEW_LAYERS, dotted_path, iter_modules, rel

PACKAGE = "karyvia"

#: 全部依赖规则编号。每条都必须有一个反向用例（见 `test_guard_integrity.py`）。
RULES: tuple[str, ...] = ("R1", "R2", "R3", "R4", "R5")

#: 各层允许 import 的内部层。键是源层，值是允许的目标层集合（含自身）。
_ALLOWED_TARGETS: dict[str, frozenset[str]] = {
    "contracts": frozenset({"contracts"}),
    "kernel": frozenset({"contracts", "kernel"}),
    "sdk": frozenset({"contracts", "sdk"}),
    "builtins": frozenset({"contracts", "sdk", "builtins"}),
    "runtime": frozenset({"contracts", "kernel", "sdk", "builtins", "runtime"}),
    "embed": frozenset({"contracts", "runtime", "embed"}),
}

#: 外部插件（`plugins/`、`examples/plugins/`）与 `builtins/` 同规则（`R4`）。
_PLUGIN_ALLOWED_TARGETS = frozenset({"contracts", "sdk"})

#: 违规归属：源层 -> 规则号。`R5` 单独处理（见 `_classify`）。
_RULE_BY_SOURCE_LAYER = {
    "contracts": "R1",
    "kernel": "R2",
    "sdk": "R3",
    "builtins": "R4",
    "runtime": "R5",
    "embed": "R5",
}


@dataclass(frozen=True)
class Violation:
    """一条依赖违规。`rule` 是 `R1`–`R5` 之一。"""

    rule: str
    path: str
    line: int
    imported: str
    reason: str

    def __str__(self) -> str:
        return f"{self.rule}  {self.path}:{self.line}  import {self.imported}  -- {self.reason}"


def _imported_modules(tree: ast.Module, *, module_dotted: str) -> list[tuple[str, int]]:
    """收集模块内所有 import 目标的绝对导入路径与行号。

    相对导入按当前模块所在包解析为绝对路径，否则 `from ..kernel import x` 这类
    写法会绕过所有规则。
    """
    package_parts = module_dotted.split(".")[:-1] if "." in module_dotted else []
    results: list[tuple[str, int]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            results.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                # `__init__.py` 的 dotted_path 已经归属为包自身，因此 level=1 时
                # 基准包是 module_dotted 去掉最后一段（普通模块）或其自身（包）。
                anchor = package_parts[: len(package_parts) - (node.level - 1)]
                base = ".".join([*anchor, node.module] if node.module else anchor)
            if not base:
                continue
            results.append((base, node.lineno))
            # `from karyvia.kernel import turn` 的目标层由 base 决定，
            # 逐个 alias 再展开一次可覆盖 `from karyvia import kernel`。
            results.extend((f"{base}.{alias.name}", node.lineno) for alias in node.names)

    return results


def _layer_of(imported: str) -> str | None:
    """取出 import 目标所属的顶层（`karyvia.kernel.x` -> `kernel`）。"""
    if imported == PACKAGE or not imported.startswith(f"{PACKAGE}."):
        return None
    top = imported.split(".")[1]
    if top in NEW_LAYERS:
        return top
    return None


def _classify(
    *,
    source_layer: str | None,
    target_layer: str,
    module_imports: frozenset[str],
) -> tuple[str, str] | None:
    """判定一次 import 违反了哪条规则；合法返回 None。

    `module_imports` 是该模块的全部目标层集合，用于 `R5` 的「同时 import」判定。
    """
    if source_layer is None:  # 外部插件
        if target_layer in _PLUGIN_ALLOWED_TARGETS:
            return None
        return ("R4", f"插件只能 import sdk/ 与 contracts/，不得 import {target_layer}/")

    if target_layer in _ALLOWED_TARGETS[source_layer]:
        return None

    rule = _RULE_BY_SOURCE_LAYER[source_layer]
    if source_layer == "embed" and {"kernel", "builtins"} & module_imports:
        return (
            "R5",
            "只有 runtime/ 是组装根，可同时 import kernel/ 与 builtins/",
        )
    if source_layer == "contracts":
        return ("R1", "contracts/ 不得 import 本项目任何其他模块")
    return (
        rule,
        f"{source_layer}/ 不得 import {target_layer}/，"
        f"允许的目标是 {sorted(_ALLOWED_TARGETS[source_layer])}",
    )


def _check_module(
    path: Path,
    *,
    src_dir: Path,
    repo_root: Path,
    source_layer: str | None,
) -> list[Violation]:
    dotted = dotted_path(path, src_dir=src_dir) if source_layer is not None else path.stem
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:  # 语法错误交给 lint/类型检查报告，这里不遮蔽
        return [
            Violation("R0", rel(path, root=repo_root), exc.lineno or 0, "", f"无法解析：{exc.msg}")
        ]

    imports = _imported_modules(tree, module_dotted=dotted)
    target_layers = frozenset(
        layer for name, _ in imports if (layer := _layer_of(name)) is not None
    )
    relative = rel(path, root=repo_root)

    violations: list[Violation] = []
    seen: set[tuple[str, int]] = set()
    for imported, lineno in imports:
        target_layer = _layer_of(imported)
        if target_layer is None:
            continue
        verdict = _classify(
            source_layer=source_layer,
            target_layer=target_layer,
            module_imports=target_layers,
        )
        if verdict is None:
            continue
        rule, reason = verdict
        key = (rule, lineno)
        if key in seen:  # `from X import Y` 会展开成两条，同一行只报一次
            continue
        seen.add(key)
        violations.append(Violation(rule, relative, lineno, imported, reason))
    return violations


def collect_violations(
    *,
    src_dir: Path,
    repo_root: Path,
    plugin_roots: list[Path] | None = None,
) -> list[Violation]:
    """扫描新层与插件目录，返回全部依赖违规。

    目录不存在时返回空列表，空骨架必须通过。
    """
    violations: list[Violation] = []

    package_dir = src_dir / PACKAGE
    for layer in NEW_LAYERS:
        for path in iter_modules(package_dir / layer):
            violations.extend(
                _check_module(path, src_dir=src_dir, repo_root=repo_root, source_layer=layer)
            )

    for root in plugin_roots or []:
        for path in iter_modules(root):
            violations.extend(
                _check_module(path, src_dir=src_dir, repo_root=repo_root, source_layer=None)
            )

    return violations


__all__ = [
    "IGNORED_DIRS",
    "RULES",
    "Violation",
    "collect_violations",
]
