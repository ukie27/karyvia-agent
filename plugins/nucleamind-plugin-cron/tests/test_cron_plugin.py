"""manifest、注册与配置的用例，外加那条 `inspect.signature` 守卫。

**签名守卫是照抄 `plugins/…-memory/tests/test_memory_plugin.py` 的**（`D39` 的教训）：
`isinstance` 对 `runtime_checkable` Protocol 只查属性存在性，而 basedpyright 的 `include`
只覆盖 `src/nucleamind`——**插件全都不在类型检查范围内**。`D39` 就是这么漏掉
`CommandHandler.handle` 的第二个参数、直到跑真实 `nm run` 才发现。
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from _cron_fakes import Api, CronContext
from nucleamind_plugin_cron import (
    CHANNEL_NAME,
    COMMAND_NAME,
    JOBS_DIR_NAME,
    JOBS_FILE,
    MANIFEST,
    TOOL_NAMES,
    jobs_directory,
    register,
    resolve_settings,
    setup,
)

from nucleamind.contracts import CapabilityKind, ErrorCode, NucleaError

SOURCE_DIR = Path(__file__).resolve().parents[1] / "src" / "nucleamind_plugin_cron"


# ------------------------------------------------------------------------------ manifest


def test_manifest_identity() -> None:
    """entry point 的 name 必须等于 manifest 的 `id`（`D25`：候选 id 先于 manifest 可知）。"""
    assert MANIFEST.id == "cron"
    assert MANIFEST.setup == "nucleamind_plugin_cron:setup"


def test_manifest_declares_five_capabilities() -> None:
    declared = {(decl.kind, decl.name) for decl in MANIFEST.capabilities}
    assert declared == {
        (CapabilityKind.CHANNEL, CHANNEL_NAME),
        *((CapabilityKind.TOOL, name) for name in TOOL_NAMES),
        (CapabilityKind.COMMAND, COMMAND_NAME),
    }


def test_manifest_declares_no_priority() -> None:
    """manifest 里写 `priority` 会被原样采纳（默认 100），而内建基准是 0。"""
    assert all("priority" not in decl.model_fields_set for decl in MANIFEST.capabilities)


def test_register_covers_exactly_the_declaration(tmp_path: Path) -> None:
    """外部插件用不上装配根的 `keep` 声明过滤，声明与注册必须**严格相等**。"""
    api = Api(CronContext(tmp_path))
    register(api, api.ctx)
    assert api.registered == {(decl.kind.value, decl.name) for decl in MANIFEST.capabilities}


def test_setup_registers_through_the_same_path(tmp_path: Path) -> None:
    api = Api(CronContext(tmp_path))
    setup(api)  # type: ignore[arg-type]
    assert api.registered == {(decl.kind.value, decl.name) for decl in MANIFEST.capabilities}


def test_all_capabilities_share_one_scheduler(tmp_path: Path) -> None:
    """给它们各建一个会让「工具刚排的任务，命令查不到」这种问题只在并发下偶发。"""
    api = Api(CronContext(tmp_path))
    scheduler = register(api, api.ctx)
    holders = [
        *api.tools.values(),
        *api.commands.values(),
        *api.channels.values(),
    ]
    for holder in holders:
        found = [
            value
            for name in dir(holder)
            if name.startswith("_") and (value := getattr(holder, name, None)) is scheduler
        ]
        assert found, f"{type(holder).__name__} 没有共用那一个调度器"


def test_setup_does_not_touch_the_disk(tmp_path: Path) -> None:
    """任务表在第一次排期时才落盘——不为一个可能永远不排期的插件动用户的磁盘。"""
    state = tmp_path / "state"
    state.mkdir()
    api = Api(CronContext(state))
    setup(api)  # type: ignore[arg-type]
    assert list(state.iterdir()) == []


def test_importing_the_module_has_no_side_effects() -> None:
    """发现阶段只 import 本模块取 `MANIFEST`，此时不该发生任何 IO（技术方案 §7.2）。

    用 AST 扫模块顶层：`import` 之后再断言「什么都没发生」是测不到的。
    """
    tree = ast.parse((SOURCE_DIR / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        assert isinstance(
            node,
            (
                ast.Import,
                ast.ImportFrom,
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.Assign,
                ast.AnnAssign,
                ast.Expr,  # 模块 docstring
            ),
        ), f"模块顶层出现了可执行语句：{type(node).__name__}"


def test_the_plugin_imports_no_kernel_module() -> None:
    """`R4`：插件只能 import `contracts` 与 `sdk`。扫 import 语句而不是文本包含
    （`D38-B` 把这条守成了 AST 断言，下一个插件照抄它）。"""
    for path in sorted(SOURCE_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not name.startswith("nucleamind.kernel"), f"{path.name} import 了 {name}"
                assert not name.startswith("nucleamind.runtime"), f"{path.name} import 了 {name}"
                assert not name.startswith("nucleamind.builtins"), f"{path.name} import 了 {name}"


def test_no_third_party_import_outside_the_standard_library() -> None:
    """**一个第三方依赖都不引入**：cron 表达式自己解析、写盘用标准库。

    `tzdata` 不算例外——它只是给 `zoneinfo` 用的数据包，没有可 import 的 API。
    """
    allowed = {"nucleamind", "__future__"}
    standard = {
        "asyncio",
        "collections",
        "dataclasses",
        "datetime",
        "enum",
        "json",
        "os",
        "pathlib",
        "re",
        "time",
        "typing",
        "uuid",
        "zoneinfo",
    }
    for path in sorted(SOURCE_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for name in names:
                root = name.split(".")[0]
                assert root in allowed | standard, f"{path.name} import 了第三方模块 {root}"


# ------------------------------------------------------------------------------ 签名守卫


def test_registered_implementations_match_the_contract_signatures(tmp_path: Path) -> None:
    """逐个比对注册实现与契约 Protocol 的签名。

    `D39` 的 `/memory` 第一版只写了 `handle(invocation)`，49 个命令用例全绿——它们直接用
    一个实参调 `handle()`，测的是「我自己写的那个签名」而不是「kernel 会怎么调」。
    真实表现是 `nm run` 下一条 `kernel.unexpected` + `TypeError`。
    """
    from nucleamind.contracts import Channel, CommandHandler, ToolHandler

    api = Api(CronContext(tmp_path))
    register(api, api.ctx)

    pairs: list[tuple[object, object, tuple[str, ...]]] = [
        (api.commands[COMMAND_NAME], CommandHandler, ("handle",)),
        (api.channels[CHANNEL_NAME], Channel, ("start", "stop", "receive", "deliver")),
        *((tool, ToolHandler, ("execute",)) for tool in api.tools.values()),
    ]
    for implementation, protocol, methods in pairs:
        for method in methods:
            expected = inspect.signature(getattr(protocol, method))
            actual = inspect.signature(getattr(implementation, method))
            assert [
                (name, parameter.kind)
                for name, parameter in expected.parameters.items()
                if name != "self"
            ] == [
                (name, parameter.kind) for name, parameter in actual.parameters.items()
            ], f"{type(implementation).__name__}.{method} 与 {protocol.__name__} 的签名不一致"


def test_the_channel_satisfies_the_protocol_structurally(tmp_path: Path) -> None:
    from nucleamind.contracts import Channel

    api = Api(CronContext(tmp_path))
    register(api, api.ctx)
    assert isinstance(api.channels[CHANNEL_NAME], Channel)


# ------------------------------------------------------------------------------ 配置


def test_the_default_directory_is_under_the_state_dir(tmp_path: Path) -> None:
    ctx = CronContext(tmp_path)
    settings = resolve_settings({})
    assert jobs_directory(ctx, settings) == tmp_path / JOBS_DIR_NAME


def test_a_relative_directory_resolves_against_the_state_dir(tmp_path: Path) -> None:
    """`nm` 从哪个目录启动不该改变任务存到哪里。"""
    ctx = CronContext(tmp_path, config={"dir": "jobs"})
    settings = resolve_settings(dict(ctx.config))
    assert jobs_directory(ctx, settings) == tmp_path / "jobs"


def test_an_absolute_directory_is_taken_as_written(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    ctx = CronContext(tmp_path, config={"dir": str(elsewhere)})
    settings = resolve_settings(dict(ctx.config))
    assert jobs_directory(ctx, settings) == elsewhere


def test_setup_reports_a_bad_timezone_as_a_config_error(tmp_path: Path) -> None:
    """配置里写了个不存在的时区，要在 `setup()` 就报出来——而不是三天后某条任务安静地
    不再触发。"""
    api = Api(CronContext(tmp_path, config={"timezone": "Nope/Nowhere"}))
    with pytest.raises(NucleaError) as caught:
        setup(api)  # type: ignore[arg-type]
    assert caught.value.code is ErrorCode.CONFIG_INVALID
    assert caught.value.detail["key"] == "timezone"


@pytest.mark.parametrize(
    "config",
    [
        {"dir": 1},
        {"timezone": 1},
        {"tick_ceiling_ms": 5},
        {"tick_ceiling_ms": 0},
        {"min_interval_ms": -1},
        {"catch_up_window_ms": -1},
        {"max_jobs": 0},
        {"max_jobs": "many"},
    ],
)
def test_setup_rejects_bad_configuration(tmp_path: Path, config: dict[str, object]) -> None:
    api = Api(CronContext(tmp_path, config=config))  # type: ignore[arg-type]
    with pytest.raises(NucleaError) as caught:
        setup(api)  # type: ignore[arg-type]
    assert caught.value.code is ErrorCode.CONFIG_INVALID


def test_the_config_schema_forbids_unknown_keys() -> None:
    """`additionalProperties: false`：拼错的配置项要报出来而不是静默失效。"""
    assert MANIFEST.config_schema is not None
    assert MANIFEST.config_schema["additionalProperties"] is False


def test_every_config_key_is_described() -> None:
    """`nm config` 与编辑器补全都靠它。"""
    assert MANIFEST.config_schema is not None
    properties = MANIFEST.config_schema["properties"]
    assert isinstance(properties, dict)
    for key, spec in properties.items():
        assert isinstance(spec, dict)
        assert spec.get("description"), f"配置项 {key} 没有说明"


def test_the_jobs_file_name_is_stable() -> None:
    """它是发布出去的契约（README 里有示例），改名要同时改文档。"""
    assert JOBS_FILE == "jobs.json"
