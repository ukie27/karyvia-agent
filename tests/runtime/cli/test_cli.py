"""`karyvia` 的进程入口：argv 解析、实例选择参数与三个子命令的只读路径。

职责：验 `app()` 的派发与退出码、`parse_options()` 摘参数的规则、`karyvia config show` 与
`karyvia session` 在真实实例目录上的输出。
不负责：验 `karyvia run` 的交互（那需要一个真终端；它的正文由
`tests/runtime/test_bootstrap.py` 与 `tests/builtins/test_cli_entry.py` 覆盖）。

**退出码是这套用例的主角**：`karyvia` 要能进脚本，「失败了但返回 0」比打印得难看严重得多。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from karyvia.contracts import ErrorCode, KaryviaError
from karyvia.runtime.cli.main import app, parse_options, resolve_version

from .._support import SCRIPT, TEST_MANIFESTS, text_response, write_config


@pytest.fixture(autouse=True)
def _script() -> None:
    SCRIPT[:] = [text_response("好的。")]


# ---------------------------------------------------------------------- 顶层派发


def test_help_and_version_return_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert app([]) == 0
    assert app(["--help"]) == 0
    assert app(["--version"]) == 0
    assert resolve_version() in capsys.readouterr().out


def test_an_unknown_command_returns_two(capsys: pytest.CaptureFixture[str]) -> None:
    assert app(["nope"]) == 2
    assert "未知命令" in capsys.readouterr().err


def test_instance_options_are_taken_out_of_the_argv() -> None:
    """实例选择参数属于进程入口，子命令看不到它们。"""
    options = parse_options(["--instance", "work", "-p", "你好", "--set", "turn.max_iterations=2"])
    assert options.instance == "work"
    assert options.overrides == ["turn.max_iterations=2"]
    assert options.rest == ["-p", "你好"]


def test_a_dangling_option_value_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(KaryviaError) as caught:
        parse_options(["--instance"])
    assert caught.value.code is ErrorCode.INPUT_MALFORMED
    # 同一条错误经 `app()` 时变成退出码 2 而不是 traceback。
    assert app(["config", "--instance"]) == 2
    assert "karyvia:" in capsys.readouterr().err


# ------------------------------------------------------------------------- karyvia init


def test_init_generates_a_config_and_returns_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "fresh"
    assert app(["init", "--instance-dir", str(root)]) == 0
    out = capsys.readouterr().out
    assert (root / "config.json").exists()
    assert (root / "config.schema.json").exists()
    assert str(root / "config.json") in out
    assert "OPENAI_API_KEY" in out


def test_init_puts_a_named_instance_under_the_instances_container(tmp_path: Path) -> None:
    assert app(["init", "--instance", "work"]) == 0

    root = tmp_path / ".karyvia" / "instances" / "work"
    assert (root / "config.json").is_file()
    assert not (tmp_path / ".karyvia" / "work").exists()


def test_init_refuses_to_overwrite_and_returns_three(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`EDG-501`：已存在的配置一个字节都不动，退出码说明「不需要初始化」。"""
    write_config(tmp_path)
    original = (tmp_path / "config.json").read_text(encoding="utf-8")

    assert app(["init", "--instance-dir", str(tmp_path)]) == 3

    assert (tmp_path / "config.json").read_text(encoding="utf-8") == original
    assert "已存在" in capsys.readouterr().out


def test_init_takes_no_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    assert app(["init", "show"]) == 2
    assert "karyvia:" in capsys.readouterr().err


def test_init_help_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert app(["init", "--help"]) == 0
    assert "karyvia init" in capsys.readouterr().out


def test_run_on_a_fresh_instance_generates_a_config_and_stops(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """§10.1 步骤 2 的「无配置文件」分支：只生成、只指路，**不装配实例**。

    「不装配」的可断言形态是没有取过实例锁——真跑起来会取。
    """
    root = tmp_path / "fresh"
    assert app(["run", "--instance-dir", str(root)]) == 0
    assert (root / "config.json").exists()
    assert not (root / "instance.lock").exists()
    assert "OPENAI_API_KEY" in capsys.readouterr().out


def test_run_does_not_touch_an_existing_config(tmp_path: Path) -> None:
    """有配置时首次运行分支一个字节都不写，连派生 schema 都不比对。"""
    write_config(tmp_path)
    original = (tmp_path / "config.json").read_text(encoding="utf-8")
    asyncio.run(_write_a_session(tmp_path))
    assert (tmp_path / "config.json").read_text(encoding="utf-8") == original
    assert not (tmp_path / "config.schema.json").exists()


# ------------------------------------------------------------------ karyvia config show


def test_config_show_prints_the_effective_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(tmp_path, routing={"command_prefix": "!"})
    assert app(["config", "show", "--instance-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert '"command_prefix": "!"' in out
    assert str(tmp_path) in out


def test_config_show_can_report_origins(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`CFG-005`：每个生效值都查得到来源。"""
    write_config(tmp_path, routing={"command_prefix": "!"})
    assert app(["config", "show", "--origins", "--instance-dir", str(tmp_path)]) == 0
    assert "/routing/command_prefix" in capsys.readouterr().out


def test_config_show_does_not_create_the_instance_directory(tmp_path: Path) -> None:
    """只读诊断不该顺手建目录（`load_config(ensure_dirs=False)` 就是为此存在的）。"""
    missing = tmp_path / "never-created"
    assert app(["config", "show", "--instance-dir", str(missing)]) == 0
    assert not missing.exists()


def test_config_show_does_not_take_the_instance_lock(tmp_path: Path) -> None:
    """看一眼配置不该与正在跑的实例互斥。"""
    write_config(tmp_path)
    assert app(["config", "show", "--instance-dir", str(tmp_path)]) == 0
    assert not (tmp_path / "instance.lock").exists()


def test_an_unknown_config_subcommand_returns_two(capsys: pytest.CaptureFixture[str]) -> None:
    assert app(["config", "nope"]) == 2
    assert "karyvia:" in capsys.readouterr().err


# --------------------------------------------------------------------- karyvia session


def test_session_list_reports_an_empty_instance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(tmp_path)
    assert app(["session", "list", "--instance-dir", str(tmp_path)]) == 0
    assert "没有会话" in capsys.readouterr().out


def test_session_show_needs_an_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_config(tmp_path)
    assert app(["session", "show", "--instance-dir", str(tmp_path)]) == 2
    assert "karyvia:" in capsys.readouterr().err


async def _write_a_session(root: Path) -> None:
    from karyvia.kernel.turn import CancelToken
    from karyvia.runtime.bootstrap import bootstrap

    instance = await bootstrap(instance_dir=root, manifests=TEST_MANIFESTS)
    try:
        await instance.run_cli(["-p", "记住这句话"], CancelToken())
    finally:
        await instance.stop()


def test_session_list_sees_a_written_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`karyvia session` 装的是**生效的**会话存储，因此它看得到刚写下的那条历史。

    **本用例是同步的**：`app()` 自己 `asyncio.run()`，在一个已经在跑的循环里调它会当场
    报错——那正是「`karyvia` 是进程入口」的形状，不是缺陷。
    """
    write_config(tmp_path)
    asyncio.run(_write_a_session(tmp_path))
    capsys.readouterr()

    assert app(["session", "list", "--instance-dir", str(tmp_path)]) == 0
    listed = capsys.readouterr().out
    assert "cli~local~default" in listed
    assert app(["session", "show", "cli~local~default", "--instance-dir", str(tmp_path)]) == 0
    assert "记录数" in capsys.readouterr().out
