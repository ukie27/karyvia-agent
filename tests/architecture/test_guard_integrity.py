"""守卫自身的完整性检查。

职责：断言架构守卫不可被静默绕过——本包内不得出现 `skip` / `xfail`，
且每条依赖规则 `R1`–`R6` 都有对应的反向用例。
不负责：检查具体规则内容——那是各 `test_*.py` 自己的事。

守卫的价值全部来自「失败即阻断」。一个被 `@pytest.mark.xfail` 标住的规则在
报表里是绿的，在实现上是关的；这类退让必须在提交时就被拦下，而不是等到
某次重构后才发现规则早已失效。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ._boundaries import RULES
from ._common import IGNORED_DIRS

_GUARD_DIR = Path(__file__).resolve().parent

#: 会让用例不真正执行的标记与调用。`importorskip` 同样禁止：守卫只做 AST 读取，
#: 不依赖任何可选依赖，出现它即说明有人在用可选依赖掩盖失败。
_FORBIDDEN_MARKERS = frozenset({"skip", "skipif", "xfail"})
_FORBIDDEN_CALLS = frozenset({"skip", "xfail", "importorskip"})


def _guard_modules() -> list[Path]:
    return [
        path
        for path in sorted(_GUARD_DIR.rglob("*.py"))
        if not any(part in IGNORED_DIRS for part in path.relative_to(_GUARD_DIR).parts)
    ]


def _evasions(path: Path) -> list[str]:
    """收集模块中所有 skip / xfail 形态，返回 `行号: 写法` 说明。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []

    for node in ast.walk(tree):
        # 装饰器形态：@pytest.mark.skip / @pytest.mark.xfail(...)
        if isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_MARKERS:
            owner = node.value
            if isinstance(owner, ast.Attribute) and owner.attr == "mark":
                found.append(f"{node.lineno}: pytest.mark.{node.attr}")
        # 调用形态：pytest.skip(...) / pytest.importorskip(...)
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in _FORBIDDEN_CALLS
                and isinstance(func.value, ast.Name)
                and func.value.id == "pytest"
            ):
                found.append(f"{node.lineno}: pytest.{func.attr}()")

    return found


@pytest.mark.parametrize("path", _guard_modules(), ids=lambda p: p.name)
def test_guard_module_has_no_skip_or_xfail(path: Path) -> None:
    if path.name == Path(__file__).name:
        return  # 本文件包含上述名字的字符串常量与 AST 匹配逻辑，豁免自身
    evasions = _evasions(path)
    assert not evasions, (
        f"{path.name} 中出现了 skip / xfail：\n"
        + "\n".join(f"  {line}" for line in evasions)
        + "\n架构守卫失败即阻断，规则不成立时应修代码或显式改规则，不是把用例关掉。"
    )


def test_every_rule_has_an_injection_case() -> None:
    """`R1`–`R6` 每条都必须有反向用例，否则「守卫会拦」只是假设。"""
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in _guard_modules()
        if path.name.startswith("test_")
    )
    missing = [rule for rule in RULES if f'"{rule}"' not in sources]
    assert not missing, f"以下规则缺少反向（注入违规）用例：{missing}"
