"""首次运行的写盘与指引（`runtime/first_run.py`）。

职责：验 `config.json` 永不被覆盖、派生 schema 会被刷新、指引里有「文件/字段/变量名」
且没有任何值，以及本模块自写的四个常量与内建模型供应商一致。
不负责：模板内容（`tests/kernel/test_scaffold.py`）、`karyvia init` 的退出码
（`tests/runtime/cli/test_cli.py`）、端到端路径（`tests/e2e/`）。

**「永不覆盖」是的可执行形态**：一个能覆盖用户配置的实现，在测试里表现为
「第二次调用之后文件内容变了」——因此这里逐字节比对。
"""

from __future__ import annotations

import json
from pathlib import Path

from karyvia.kernel.config import (
    JSON_SCHEMA_FILENAME,
    InstanceLayout,
    config_json_schema,
    load_config,
)
from karyvia.runtime.first_run import (
    DEFAULT_MODEL_NAME,
    MODEL_API_KEY_ENV,
    MODEL_PLUGIN_ID,
    MODEL_PROVIDER_NAME,
    MODEL_SECRET_NAME,
    ensure_initial_config,
    guidance_lines,
    initial_config,
)

SENTINEL_KEY = "sk-firstrun0123456789abcdef"


def layout_at(root: Path) -> InstanceLayout:
    return InstanceLayout.resolve(instance_dir=root)


# ----------------------------------------------------------------------- 写盘


class TestEnsureInitialConfig:
    def test_it_creates_both_files_on_a_fresh_instance(self, tmp_path: Path) -> None:
        result = ensure_initial_config(layout_at(tmp_path), env={})
        assert result.created
        assert result.config_path.exists()
        assert result.schema_path.name == JSON_SCHEMA_FILENAME
        assert json.loads(result.schema_path.read_text(encoding="utf-8")) == config_json_schema()

    def test_the_written_config_loads(self, tmp_path: Path) -> None:
        ensure_initial_config(layout_at(tmp_path), env={})
        loaded = load_config(instance_dir=tmp_path)
        assert loaded.config.model.name == DEFAULT_MODEL_NAME
        assert loaded.config.model.provider == MODEL_PROVIDER_NAME
        assert loaded.config.plugins.entry(MODEL_PLUGIN_ID).secrets == {
            MODEL_SECRET_NAME: f"${{{MODEL_API_KEY_ENV}}}"
        }

    def test_an_existing_config_is_never_touched(self, tmp_path: Path) -> None:
        """不得静默回退后覆盖原文件。这里连一个字节都不许变。"""
        layout = layout_at(tmp_path)
        layout.ensure()
        mine = '{"model": {"name": "my-model"}}'
        layout.config_path.write_text(mine, encoding="utf-8")

        result = ensure_initial_config(layout, env={})

        assert not result.created
        assert layout.config_path.read_text(encoding="utf-8") == mine

    def test_the_derived_schema_is_refreshed_but_not_rewritten(self, tmp_path: Path) -> None:
        """schema 是我们生成的产物，过期就该刷新；内容相同则一个字节都不写。"""
        layout = layout_at(tmp_path)
        first = ensure_initial_config(layout, env={})
        stamp = first.schema_path.stat().st_mtime_ns

        first.schema_path.write_text("{}", encoding="utf-8")
        again = ensure_initial_config(layout, env={})
        assert json.loads(again.schema_path.read_text(encoding="utf-8")) == config_json_schema()

        third = ensure_initial_config(layout, env={})
        assert third.schema_path.stat().st_mtime_ns == again.schema_path.stat().st_mtime_ns
        assert stamp  # 只是确认第一次真的写了

    def test_it_reports_which_variables_are_still_missing(self, tmp_path: Path) -> None:
        missing = ensure_initial_config(layout_at(tmp_path), env={})
        assert missing.required_env == (MODEL_API_KEY_ENV,)
        assert missing.missing_env == (MODEL_API_KEY_ENV,)
        assert not missing.ready

    def test_an_empty_variable_counts_as_missing(self, tmp_path: Path) -> None:
        """与 `secrets.py` 的判据一致：导出成空串和没导出是同一件事。"""
        result = ensure_initial_config(layout_at(tmp_path), env={MODEL_API_KEY_ENV: ""})
        assert result.missing_env == (MODEL_API_KEY_ENV,)

    def test_a_present_variable_makes_the_instance_ready(self, tmp_path: Path) -> None:
        result = ensure_initial_config(
            layout_at(tmp_path), env={MODEL_API_KEY_ENV: SENTINEL_KEY}
        )
        assert result.ready
        assert result.missing_env == ()

    def test_it_creates_the_kernel_owned_directories(self, tmp_path: Path) -> None:
        """首次运行之后 `karyvia run` 要能直接跑，而工具与会话各自需要自己的目录。"""
        ensure_initial_config(layout_at(tmp_path), env={})
        for name in ("sessions", "logs", "plugins", "workspace"):
            assert (tmp_path / name).is_dir()


# ----------------------------------------------------------------------- 指引


class TestGuidance:
    def test_it_names_the_file_the_field_and_the_variable(self, tmp_path: Path) -> None:
        """ 的三样：哪个文件、哪个字段、哪个环境变量。"""
        result = ensure_initial_config(layout_at(tmp_path), env={})
        text = "\n".join(guidance_lines(result))
        assert str(result.config_path) in text
        assert f"/plugins/{MODEL_PLUGIN_ID}/secrets/{MODEL_SECRET_NAME}" in text
        assert MODEL_API_KEY_ENV in text
        assert "/model/name" in text

    def test_it_never_prints_a_credential_value(self, tmp_path: Path) -> None:
        """只说变量名。本模块从头到尾没读过任何凭据的值。"""
        result = ensure_initial_config(
            layout_at(tmp_path), env={MODEL_API_KEY_ENV: SENTINEL_KEY}
        )
        text = "\n".join(guidance_lines(result))
        assert SENTINEL_KEY not in text
        assert "再跑一次" in text

    def test_it_says_so_when_the_config_already_existed(self, tmp_path: Path) -> None:
        layout = layout_at(tmp_path)
        ensure_initial_config(layout, env={})
        text = "\n".join(guidance_lines(ensure_initial_config(layout, env={})))
        assert "已存在" in text

    def test_it_mentions_the_local_endpoint_escape_hatch(self, tmp_path: Path) -> None:
        """没有密钥的用户（Ollama / vLLM / LM Studio）也要知道下一步怎么走。"""
        text = "\n".join(guidance_lines(ensure_initial_config(layout_at(tmp_path), env={})))
        assert "base_url" in text
        assert '"none"' in text


# --------------------------------------------------------------- 与内建的对照


def test_defaults_match_the_builtin_model_provider() -> None:
    """本模块自写的四个常量与 `builtins/model_openai/` 必须一致。

    各写一份是刻意的（`karyvia init` 不该为了四个字符串把 httpx 拉进进程），
    与 `estimate_tokens` / `DEFAULT_GRACE_MS` 同一种做法——**因此必须有这条对照**。
    """
    from karyvia.builtins.model_openai import CAPABILITY_NAME, SECRET_NAME
    from karyvia.builtins.registry import BUILTIN_MANIFESTS

    assert MODEL_PROVIDER_NAME == CAPABILITY_NAME
    assert MODEL_SECRET_NAME == SECRET_NAME
    assert MODEL_PLUGIN_ID in {manifest.id for manifest in BUILTIN_MANIFESTS}


def test_the_template_only_needs_the_one_credential() -> None:
    """模板要求用户导出的变量恰好一个——「只配凭据即可用」说的就是这个数字。"""
    assert initial_config().required_env == (MODEL_API_KEY_ENV,)
