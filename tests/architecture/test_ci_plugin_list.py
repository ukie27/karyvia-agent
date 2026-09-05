"""CI 的插件安装清单必须等于磁盘上的插件集合（`D41`）。

职责：断言 `.github/workflows/ci.yml` 里 `pip install --no-deps -e <路径>` 的那组路径，
与 `plugins/` ∪ `examples/plugins/` 下真实存在的发行包一一对应。
不负责：判断插件本身对不对（那是各插件自己的测试树）、CI 的其余步骤。

**为什么这条规则值得一个守卫。** `pytest` 的 `testpaths` 收集整个 `plugins/`，而每棵插件
测试树第一行就 `import karyvia_plugin_<id>`；插件经 entry point 被发现，没 editable
装进环境就**根本 import 不到**。因此清单漏一个的后果不是「少跑几个用例」，而是**收集期
`ModuleNotFoundError` 直接中断整个作业**。

而这件事真的发生过：`D36`–`D40` 连续新增官方插件时没有同步这张清单，`web` /
`mcp` / `memory` / `cron` 五个全漏，约 1100 个用例在 CI 里从未跑过——本地开发环境装了插件，
所以没有任何人看见。`D40` 收口时才发现。

规则本身是纪律性的（「加插件记得改 CI」），而这个仓库对纪律的一贯答案是把它变成守卫：
`R1`–`R6` 的依赖边界、`scripts/check_startup_cost.py` 的冷启动预算、
`tests/e2e/test_plugin_docs.py` 直接 `exec` 文档代码块——都是同一种做法。少的正是这一条。

**不解析 YAML。** `pyyaml` 不在本仓库的依赖里，而架构守卫是独立作业、刻意不装可选依赖
（见 `test_guard_integrity.py`）。这里只需要那一组路径，正则扫原文即可；判据因此也更直接
——它读的就是人会去改的那几行。
"""

from __future__ import annotations

import re
from pathlib import Path

from ._common import REPO_ROOT, plugin_package_roots, rel

#: CI 定义。它是唯一的安装清单来源。
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: `pip install [--no-deps] -e <路径>` 里的那个路径。只认 `plugins/` 与
#: `examples/plugins/` 下的目标——宿主自身的 `pip install -e ".[dev]"` 不在这张清单里。
_EDITABLE = re.compile(
    r"""pip\s+install\b[^\n]*?-e\s+(?P<path>(?:examples/)?plugins/[A-Za-z0-9._-]+)"""
)


def _installed_paths() -> list[str]:
    """CI 里被 editable 安装的插件路径，按出现顺序。"""
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    return [match.group("path") for match in _EDITABLE.finditer(text)]


def _distributions() -> list[Path]:
    """磁盘上真实的插件发行包。

    判据是**有 `pyproject.toml`**：`plugins/README.md` 旁边将来可能出现文档目录或
    脚手架，它们没有发行元数据，也就装不了、也没有测试树要收集。
    """
    return [root for root in plugin_package_roots() if (root / "pyproject.toml").is_file()]


def test_ci_installs_every_plugin_on_disk() -> None:
    """磁盘上有、CI 没装 —— 这正是 `D36`–`D40` 那次回归的形状。"""
    expected = {rel(root) for root in _distributions()}
    installed = set(_installed_paths())
    missing = sorted(expected - installed)
    assert not missing, (
        "以下插件在磁盘上存在但 CI 没有安装：\n"
        + "\n".join(f"  {path}" for path in missing)
        + f"\n\n请在 {rel(CI_WORKFLOW)} 的「Install plugins」步骤里补上"
        " `pip install --no-deps -e <路径>`。\n"
        "漏装不是「少跑几个用例」：`testpaths` 会收集该插件的 tests/，"
        "而它 import 不到自己的包，整个作业在收集期就中断。"
    )


def test_ci_does_not_install_plugins_that_are_gone() -> None:
    """CI 装了、磁盘上没有 —— 删插件时漏改 CI，同样是收集期失败（这次是 pip 失败）。"""
    expected = {rel(root) for root in _distributions()}
    stale = sorted(set(_installed_paths()) - expected)
    assert not stale, (
        "以下路径出现在 CI 的安装清单里，但磁盘上没有对应的发行包：\n"
        + "\n".join(f"  {path}" for path in stale)
        + f"\n\n删除或重命名插件时要同时改 {rel(CI_WORKFLOW)}。"
    )


def test_ci_installs_each_plugin_exactly_once() -> None:
    """重复一行不会让 CI 变红，但它是清单被手工维护到失控的第一个征兆。"""
    installed = _installed_paths()
    duplicates = sorted({path for path in installed if installed.count(path) > 1})
    assert not duplicates, f"CI 的安装清单里有重复项：{duplicates}"


def test_the_disk_side_actually_finds_plugins() -> None:
    """守卫自身的完整性：`_distributions()` 返回空集时上面三条会**全部通过**。

    没有这条，把 `plugins/` 整个删掉或改名都不会让守卫报警——而那时它已经不再守任何
    东西了。与 `test_guard_integrity.py` 的「每条规则都要有反向用例」同一种考虑。
    """
    found = _distributions()
    assert len(found) >= 2, (
        f"只在磁盘上找到 {len(found)} 个插件发行包，本仓库至少有 examples/plugins/ 下的两个"
        " 示例插件。`plugin_package_roots()` 的判据或目录布局变了，这条守卫已失效。"
    )
