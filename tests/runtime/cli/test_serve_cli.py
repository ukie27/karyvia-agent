"""`karyvia serve` 的命令层行为（`D31`）。

职责：验参数解析、首次运行分支与「没有任何 Channel 时的退出码」。
不负责：HTTP 协议与真实 turn（`tests/e2e/test_openai_api.py`）。

**故意不在这里起一个真的服务**：这一层要断言的是退出码与那几条分支，而
`tests/runtime/conftest.py` 的 autouse 夹具把 entry point 清空了——那正是让这层
用例看不见开发环境里装着的插件的原因，因此「没有可服务的 Channel」在这里是常态，
也正好是最值得钉住的那条分支。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from karyvia.runtime.cli.commands.serve import _channel_overrides
from karyvia.runtime.cli.main import app
from karyvia.runtime.first_run import (
    MODEL_API_KEY_ENV,
    MODEL_PLUGIN_ID,
    MODEL_SECRET_NAME,
)

from .._support import write_config


def test_serve_appears_in_the_top_level_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert app(["--help"]) == 0
    assert "serve" in capsys.readouterr().out


def test_bad_flags_return_two(capsys: pytest.CaptureFixture[str]) -> None:
    assert app(["serve", "--bogus"]) == 2
    assert "karyvia serve" in capsys.readouterr().err


def test_help_returns_two_with_usage(capsys: pytest.CaptureFixture[str]) -> None:
    """`--help` 与参数错走同一条出口：这条命令没有自己的帮助页，只有一行用法。"""
    assert app(["serve", "--help"]) == 2
    assert "karyvia serve" in capsys.readouterr().err


def test_a_dangling_port_value_is_rejected() -> None:
    assert _channel_overrides(["--port"]) is None
    assert _channel_overrides(["--port", "not-a-number"]) is None


def test_host_and_port_become_plugin_config_overrides() -> None:
    """两个参数是所有网络 Channel 的公分母，翻成插件配置块的覆盖。"""
    assert _channel_overrides(["--host", "0.0.0.0", "--port", "9000"]) == [
        "plugins.openai-api.config.host=0.0.0.0",
        "plugins.openai-api.config.port=9000",
    ]


def test_no_arguments_is_valid() -> None:
    assert _channel_overrides([]) == []


def test_serve_on_a_fresh_instance_generates_a_config_and_stops(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """与 `karyvia run` 完全同一条首次运行分支：只生成、只指路（§10.1 步骤 2）。"""
    root = tmp_path / "fresh"
    assert app(["serve", "--instance-dir", str(root)]) == 0
    assert (root / "config.json").exists()
    assert str(root / "config.json") in capsys.readouterr().out


def test_serve_without_any_channel_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有 Channel 插件时**不要**装成起来了——退出码 1 加一句该去哪看。"""
    monkeypatch.setenv(MODEL_API_KEY_ENV, "sk-serve0123456789abcdefg")
    write_config(
        tmp_path,
        model={"name": "gpt-4o-mini", "provider": "openai"},
        plugins={MODEL_PLUGIN_ID: {"secrets": {MODEL_SECRET_NAME: f"${{{MODEL_API_KEY_ENV}}}"}}},
    )
    assert app(["serve", "--instance-dir", str(tmp_path)]) == 1
    assert "karyvia plugins list" in capsys.readouterr().err
