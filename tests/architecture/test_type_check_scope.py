"""插件必须在 basedpyright 的检查范围内。

职责：断言 `pyproject.toml` 的 `[tool.basedpyright]` 真的覆盖 `plugins/` 与
`examples/plugins/`，且它排除的那几个模块**恰好**是「import 了 CI 里不存在的平台 SDK」
的那几个。
不负责：类型检查本身（那是 CI 的 `basedpyright` 步骤）。

**为什么插件必须进检查范围。**的 `memory` 插件把 `CommandHandler.handle` 写成了
单参数，49 个命令用例全绿——它们直接用一个实参调 `handle()`，测的是「我自己写的那个签名」
而不是「kernel 会怎么调」；`isinstance` 对 `runtime_checkable` Protocol 又只查属性存在性。
 把插件纳入检查后，同一类问题当场又抓到一个：`sdk.EventHandler` 声明的是
`Awaitable[None]`，而 `feishu` 与 `openai-api` 注册的都是同步 handler，桥接层于是每来一个
事件就多产一条 `await None` 的异常 Task。**两次都是「测试测不到、类型能看见」。**

**为什么要排除四个模块，以及为什么排除清单也要守。** CI 用 `pip install --no-deps` 装插件
（见 `test_ci_plugin_list.py`），因此 `lark-oapi` / `mcp` 在 CI 环境里
**不存在**——碰它们的模块在本地报「SDK 类型未知」，在 CI 报「import 解析不到」，两套
诊断对不上，把它们放进检查范围只会让 CI 与本地各红各的。

排除是**按模块**而不是按插件的，这正好落在各 Channel 插件早就划好的那条线上：
「只有 `gateway.py` / `client.py` 一两个模块 import SDK，其余全是纯函数」
。所以这条守卫同时钉住了那条线——**谁在第二个模块里 import 了平台
SDK，谁就会让这里失败**，而那正是需要有人看一眼的时刻。
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

from ._common import IGNORED_DIRS, REPO_ROOT, plugin_package_roots, rel

#: CI 用 `--no-deps` 装插件，因此这些包在 CI 环境里不存在。
#: **`httpx` 与 `aiohttp` 不在其中**：前者是宿主依赖，后者在 `[dev]` extra 里，
#: 两者在 CI 都装得到，碰它们的模块照常检查。
SDKS_ABSENT_IN_CI = frozenset({"lark_oapi", "mcp"})


def _basedpyright_config() -> dict[str, object]:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    tool = data["tool"]
    assert isinstance(tool, dict)
    config = tool["basedpyright"]
    assert isinstance(config, dict)
    return config


def _plugin_modules() -> list[Path]:
    """`plugins/` 与 `examples/plugins/` 下所有插件源码模块（不含各自的 tests/）。"""
    modules: list[Path] = []
    for root in plugin_package_roots():
        source = root / "src"
        if not source.is_dir():
            continue
        modules.extend(
            path
            for path in sorted(source.rglob("*.py"))
            if not any(part in IGNORED_DIRS for part in path.relative_to(source).parts)
        )
    return modules


def _top_level_imports(path: Path) -> set[str]:
    """模块 import 的顶层包名。惰性 import（函数体内的）也算——它们同样要求包存在。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def _modules_touching_absent_sdks() -> set[str]:
    return {
        rel(path)
        for path in _plugin_modules()
        if _top_level_imports(path) & SDKS_ABSENT_IN_CI
    }


def test_plugins_are_inside_the_type_check_scope() -> None:
    """`include` 必须真的覆盖两棵插件树。"""
    include = _basedpyright_config().get("include")
    assert isinstance(include, list)
    entries = {str(item) for item in include}
    for required in ("plugins/**/src", "examples/plugins/**/src"):
        assert required in entries, (
            f"basedpyright 的 include 里没有 {required!r}，插件不在类型检查范围内。\n"
            "当前 include：" + ", ".join(sorted(entries)) + "\n"
            "插件不被检查时，签名与契约的不一致只能靠人眼发现—— 与  各漏过一次。"
        )


def test_type_check_excludes_exactly_the_sdk_boundary_modules() -> None:
    """排除清单 == 碰 CI 缺席 SDK 的模块集合，一个不多一个不少。"""
    exclude = _basedpyright_config().get("exclude")
    assert isinstance(exclude, list)
    listed = {str(item) for item in exclude if str(item).startswith(("plugins/", "examples/"))}
    expected = _modules_touching_absent_sdks()

    missing = sorted(expected - listed)
    assert not missing, (
        "以下模块 import 了 CI 环境里不存在的平台 SDK，却在类型检查范围内：\n"
        + "\n".join(f"  {path}" for path in missing)
        + "\n\n两种可能，处理方式相反：\n"
        "  1. 这个 import 本该待在既有的 SDK 边界模块里（gateway.py / client.py），"
        "那就把它挪回去——每个插件只有一两个模块碰 SDK 是  定下的形状。\n"
        "  2. 确实需要新开一个边界模块，那就把它加进 pyproject 的 exclude，"
        "并在插件 README 里说明多了一处 SDK 接触点。"
    )

    stale = sorted(listed - expected)
    assert not stale, (
        "以下模块被排除在类型检查之外，但它们已经不 import 任何 CI 缺席的 SDK：\n"
        + "\n".join(f"  {path}" for path in stale)
        + "\n\n把它们从 pyproject 的 exclude 里删掉——白白少检查一个模块，"
        "而这正是  要堵的那个口子。"
    )


def test_the_sdk_side_actually_finds_something() -> None:
    """守卫自身的完整性：找不到任何 SDK 边界模块时，上面那条会**空对空地通过**。

    与 `test_ci_plugin_list.py` 的同名考虑一致——一条恒真的断言比没有断言更糟，
    因为它在报表里是绿的。
    """
    found = _modules_touching_absent_sdks()
    assert found, (
        "没有找到任何 import 平台 SDK 的插件模块。`SDKS_ABSENT_IN_CI` 或插件布局变了，"
        "这条守卫已经不再守任何东西。"
    )
