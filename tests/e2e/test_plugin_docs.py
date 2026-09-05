"""`docs/plugin-development.md` 的防漂移测试（`D30` 验收的「另加」一条）。

职责：把入门文档里的每一个代码块真的执行一遍（Python 执行、JSON 与 TOML 解析），
并核对文档声称的几件事与实现一致。
不负责：验证插件加载路径（`test_plugin_runtime.py`）、检查文档的文字表述。

**为什么是「执行」而不是「比对片段」**：一份复制粘贴自实现的文档在实现改名之后仍然长得
一模一样——比对通过，读者照抄却跑不起来。执行是唯一能把「文档里的代码是对的」变成一条
可失败断言的做法。

代码块里可以出现留给读者的名字（例子里的 `MyTool`）：Python 的 `def` 不求值函数体，
因此那种占位不影响执行，也不必为了让测试跑通而把示例写成完整程序。
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
from pathlib import Path

import pytest

from nucleamind.sdk import NucleaAPI

DOC = Path(__file__).resolve().parents[2] / "docs" / "plugin-development.md"

#: ```<语言>\n<正文>```。语言缺省时归入空串，那些块不参与执行。
_BLOCK = re.compile(r"^```(\w*)\n(.*?)^```", re.MULTILINE | re.DOTALL)


def blocks(language: str) -> list[str]:
    return [body for tag, body in _BLOCK.findall(DOC.read_text(encoding="utf-8")) if tag == language]


def test_the_doc_has_the_blocks_this_test_claims_to_check() -> None:
    """先证明**有东西可查**。

    正则写错、文档被改成别的标记语言时，下面几条会以「零个块全部通过」的形式静默失效——
    那是这类测试最常见的失败方式。
    """
    assert len(blocks("python")) >= 4
    assert blocks("toml") and blocks("json")


@pytest.mark.parametrize("index", range(len(blocks("python"))))
def test_every_python_block_executes(index: int) -> None:
    """每个 Python 代码块在**各自干净的**命名空间里执行。

    刻意不让它们共享命名空间：读者是一段一段照抄的，一个只有在前一段跑过之后才成立的
    示例等于文档里藏了一条没写出来的前提。
    """
    source = blocks("python")[index]
    exec(compile(source, f"{DOC.name}#python[{index}]", "exec"), {})  # noqa: S102


@pytest.mark.parametrize("index", range(len(blocks("json"))))
def test_every_json_block_parses(index: int) -> None:
    json.loads(blocks("json")[index])


@pytest.mark.parametrize("index", range(len(blocks("toml"))))
def test_every_toml_block_parses(index: int) -> None:
    tomllib.loads(blocks("toml")[index])


def test_the_doc_lists_exactly_the_ten_registration_methods() -> None:
    """文档说「恰好 10 个注册方法」，那就得真的是这 10 个。

    多一个方法等于多一类没有冲突语义的能力，少一个等于某类能力只能靠内部特权注册——
    这句话在文档里是一条承诺，不是修辞。
    """
    listed = {name for name in re.findall(r"`(register_\w+|on)`", DOC.read_text(encoding="utf-8"))}
    actual = {name for name in dir(NucleaAPI) if name.startswith("register_")} | {"on"}
    assert len(actual) == 10
    assert actual <= listed, f"文档漏掉了：{sorted(actual - listed)}"


def test_the_override_example_matches_the_shipped_plugin() -> None:
    """文档里那个 `overrides` 串与示例插件用的是同一个。"""
    source = (
        DOC.parent.parent
        / "examples/plugins/nucleamind-plugin-session-memory/src"
        / "nucleamind_plugin_session_memory/__init__.py"
    )
    tree = ast.parse(source.read_text(encoding="utf-8"))
    target = next(
        node.value.value
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "OVERRIDE_TARGET"
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )

    assert f'overrides="{target}"' in DOC.read_text(encoding="utf-8")


def test_the_doc_points_at_both_example_plugins() -> None:
    """两个示例的相对链接得真的指到东西上。"""
    text = DOC.read_text(encoding="utf-8")
    for relative in re.findall(r"\]\((\.\./examples/plugins/[^)]+)\)", text):
        assert (DOC.parent / relative).exists(), relative
