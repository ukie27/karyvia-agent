"""用户文档的防漂移测试。

职责：把 `docs/configuration.md` 的字段表与 `SECTION_SPECS`、`docs/cli.md` 的子命令清单与
`runtime/cli/main.py` 的派发分支、两篇文档里的插件安装清单与磁盘上的发行包，各比对一次。
不负责：执行文档里的代码块（`test_plugin_docs.py` 干那个）、检查文档的文字表述。

**为什么是这三条。** 这三样都是「一处改了、另一处不会有任何东西提醒你」的清单：
加一个配置字段要改五处（`SECTION_SPECS` / `sections.py` / `validate_config` /
`defaults.py` / `document.py`），文档是第六处；加一条子命令要改 `main.py` 的派发与
`_USAGE`，文档是第三处；加一个官方插件要改 CI 安装清单与两篇文档。
– 连着五轮漏改 CI 清单那件事说明纪律不够用，而这个仓库对纪律的一贯答案是把它
变成守卫。

另钉住插件开发文档的信任事实：启用插件即完全信任代码，资源服务不是安全沙箱。

**每条断言都配一个自证用例**（`test_the_docs_have_something_to_check`）：正则写错、
文档被改成别的结构时，这些断言会以「零项全部通过」的形式静默失效——那是这类测试最常见的
失败方式， 的两条清单守卫也各带一个。

**不比对说明文字。** 字段的一句话解释会随理解演进，钉住它只会让每次改文案都失败一次；
这里钉的是**名字与默认值**——那两样漂了，文档就是在骗人。
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from karyvia.kernel.config.schema import SECTION_SPECS

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"
CONFIG_DOC = DOCS / "configuration.md"
CLI_DOC = DOCS / "cli.md"
GETTING_STARTED = DOCS / "getting-started.md"
PLUGIN_DEVELOPMENT = DOCS / "plugin-development.md"
ROOT_README = REPO_ROOT / "README.md"
CLI_MAIN = REPO_ROOT / "src" / "karyvia" / "runtime" / "cli" / "main.py"

#: 字段表的一行：`| \`名字\` | 类型 | \`默认值\` | 说明 |`。
#: 第二列（类型）刻意不参与比对——它是给人读的措辞，不是可判定的事实。
_FIELD_ROW = re.compile(r"^\|\s*`(?P<field>[a-z_]+)`\s*\|[^|]*\|\s*`(?P<default>[^`]+)`\s*\|")

#: 小节标题：`### \`turn\` —— …`。
_SECTION_HEADING = re.compile(r"^###\s+`(?P<section>[a-z_]+)`")

#: 子命令标题：`## \`karyvia init\``。取 `karyvia ` 后的第一个词。
_COMMAND_HEADING = re.compile(r"^##\s+`karyvia\s+(?P<command>[a-z]+)")

#: 用户文档中的全局安装命令。CI 还会额外用 pip 把包放进类型检查环境，那不是用户侧
#: 插件管理语义，因此这里仅认 ``karyvia``。
_INSTALL_COMMAND = re.compile(
    r"karyvia\s+plugins\s+install\b[^\n]*?\s(?P<path>(?:examples/)?plugins/[A-Za-z0-9._-]+)"
)


def _documented_fields() -> dict[tuple[str, str], str]:
    """文档里的 `(小节, 字段) -> 默认值字面量`。"""
    found: dict[tuple[str, str], str] = {}
    section: str | None = None
    for line in CONFIG_DOC.read_text(encoding="utf-8").splitlines():
        heading = _SECTION_HEADING.match(line)
        if heading is not None:
            section = heading.group("section")
            continue
        if line.startswith("## "):
            # 离开 §6，后面的表格（插件配置块那张）不是字段表。
            section = None
            continue
        row = _FIELD_ROW.match(line)
        if row is not None and section is not None:
            found[(section, row.group("field"))] = row.group("default")
    return found


def _actual_fields() -> dict[tuple[str, str], str]:
    """`SECTION_SPECS` 里的 `(小节, 字段) -> 默认值字面量`（JSON 形态）。

    默认值统一渲染成 JSON：`STR_LIST` 的默认值是元组，而文档里写的是 `[]`——
    用 JSON 做公共语言，比让文档去表达 Python 字面量更接近用户实际要写的东西。
    """
    return {
        (section, field): json.dumps(
            list(spec.default) if isinstance(spec.default, tuple) else spec.default,
            ensure_ascii=False,
        )
        for section, fields in SECTION_SPECS.items()
        for field, spec in fields.items()
    }


def _documented_commands() -> set[str]:
    return {
        match.group("command")
        for line in CLI_DOC.read_text(encoding="utf-8").splitlines()
        if (match := _COMMAND_HEADING.match(line)) is not None
    }


def _dispatched_commands() -> set[str]:
    """`main.py::app()` 里 `command == "<字面量>"` 的那组名字。

    **用 AST 而不是文本包含**：`_USAGE` 里也有这些名字，文本扫描会把「说明里提过」
    当成「真的分派了」，那正好放过「文档与说明都写了、实现漏了」这一种。
    """
    tree = ast.parse(CLI_MAIN.read_text(encoding="utf-8"))
    app = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "app"
    )
    return {
        comparator.value
        for node in ast.walk(app)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "command"
        for comparator in node.comparators
        if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str)
    }


def _official_plugins() -> set[str]:
    """磁盘上的官方发行包路径。判据是**有 `pyproject.toml`**。"""
    return {
        f"plugins/{path.name}"
        for path in (REPO_ROOT / "plugins").iterdir()
        if path.is_dir() and (path / "pyproject.toml").is_file()
    }


def _mentioned_plugins(doc: Path) -> set[str]:
    return {
        match.group("path")
        for match in _INSTALL_COMMAND.finditer(doc.read_text(encoding="utf-8"))
    }


def test_the_docs_have_something_to_check() -> None:
    """先证明**有东西可查**（见模块 docstring 第三段）。"""
    assert len(_documented_fields()) >= 20
    assert len(_documented_commands()) >= 6
    assert len(_dispatched_commands()) >= 6
    assert len(_official_plugins()) >= 5
    assert _mentioned_plugins(GETTING_STARTED)
    assert _mentioned_plugins(ROOT_README)


def test_the_config_doc_lists_exactly_the_known_fields() -> None:
    """文档的字段表 == `SECTION_SPECS`。多一行少一行都失败。"""
    documented = set(_documented_fields())
    actual = set(_actual_fields())
    assert documented - actual == set(), "文档里有 SECTION_SPECS 里没有的字段"
    assert actual - documented == set(), "新增字段没有写进 docs/configuration.md"


@pytest.mark.parametrize(("section", "field"), sorted(_actual_fields()))
def test_every_documented_default_matches_the_schema(section: str, field: str) -> None:
    """逐字段比对默认值。一个字段一条用例——报错时直接指出是哪一个。"""
    assert _documented_fields()[(section, field)] == _actual_fields()[(section, field)]


def test_the_cli_doc_lists_exactly_the_dispatched_subcommands() -> None:
    assert _documented_commands() == _dispatched_commands()


def test_the_top_level_usage_mentions_every_dispatched_subcommand() -> None:
    """`karyvia --help` 与派发分支不许分叉——文档对了而 `--help` 漏了同样是骗人。"""
    from karyvia.runtime.cli.main import _USAGE

    missing = {name for name in _dispatched_commands() if f"  {name}" not in _USAGE}
    assert missing == set()


def test_plugin_docs_keep_the_full_trust_model() -> None:
    """信任边界不许退回一个无法强制的应用级权限系统。"""
    text = PLUGIN_DEVELOPMENT.read_text(encoding="utf-8")
    assert "安装并启用即授予完整信任" in text
    assert "并不是安全沙箱" in text
    assert "容器或操作系统策略" in text


@pytest.mark.parametrize("doc", [GETTING_STARTED, ROOT_README], ids=lambda path: path.name)
def test_docs_mention_every_official_plugin(doc: Path) -> None:
    """新增官方插件时，这两篇的安装清单要一起改（CI 那张清单已有守卫）。"""
    assert _official_plugins() - _mentioned_plugins(doc) == set()


@pytest.mark.parametrize("doc", [GETTING_STARTED, ROOT_README], ids=lambda path: path.name)
def test_docs_do_not_mention_plugins_that_do_not_exist(doc: Path) -> None:
    """反向：改名或删掉一个插件之后，文档里那条命令会照抄失败。"""
    on_disk = _official_plugins() | {
        f"examples/plugins/{path.name}"
        for path in (REPO_ROOT / "examples" / "plugins").iterdir()
        if path.is_dir() and (path / "pyproject.toml").is_file()
    }
    assert _mentioned_plugins(doc) - on_disk == set()
