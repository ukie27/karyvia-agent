"""初始配置模板与派生 JSON Schema（`D24`：`kernel/config/{scaffold,json_schema}.py`）。

职责：验模板自己加载得了、文本形态跨平台一致、`${VAR}` 能被列出来；验派生的 schema 覆盖
字段表的每一项、且真的能校验生成的配置。
不负责：写盘（`tests/runtime/test_first_run.py`）、`karyvia init` 的退出码
（`tests/runtime/cli/test_cli.py`）。

**这套用例的中心是一条自证**：`karyvia init` 生成的那份配置，必须能被 `validate_config()` 与
派生的 JSON Schema **同时**接受。两条判定分叉的后果是用户对着一份合法配置看到红波浪线，
或者相反——刚生成的文件下一次启动就报未知字段。
"""

from __future__ import annotations

import json

import jsonschema
import pytest

from karyvia.contracts import ErrorCode, KaryviaError
from karyvia.kernel.config import (
    JSON_SCHEMA_FILENAME,
    SCHEMA_KEY,
    SECTION_SPECS,
    InitialConfig,
    build_initial_config,
    config_json_schema,
    defaults,
    validate_config,
)
from karyvia.kernel.config.fields import FieldKind

SECRET_REF = "${OPENAI_API_KEY}"


def sample() -> InitialConfig:
    return build_initial_config(
        model_name="gpt-4o-mini",
        model_provider="openai",
        plugin_secrets={"model-openai": {"api_key": SECRET_REF}},
    )


# ------------------------------------------------------------------------ 模板


class TestInitialConfig:
    def test_the_generated_document_validates(self) -> None:
        """最要紧的一条：生成的配置一定能被自己加载。"""
        initial = sample()
        config = validate_config(initial.document)
        assert config.model.name == "gpt-4o-mini"
        assert config.plugins.entry("model-openai").secrets == {"api_key": SECRET_REF}

    def test_the_schema_reference_is_a_relative_path(self) -> None:
        """两个文件同在实例目录里；绝对路径会在实例目录搬家后失效。"""
        assert sample().document[SCHEMA_KEY] == f"./{JSON_SCHEMA_FILENAME}"

    def test_the_text_is_utf8_lf_and_ends_with_a_newline(self) -> None:
        """`NFR-605`：两个平台上生成的字节必须一致。"""
        text = sample().text
        assert "\r" not in text
        assert text.endswith("\n")
        assert "gpt-4o-mini" in text
        assert json.loads(text)["model"]["name"] == "gpt-4o-mini"

    def test_required_env_lists_every_referenced_variable(self) -> None:
        """指引照它印。只有名字，没有值——这里根本没有值可言（`EDG-502`）。"""
        assert sample().required_env == ("OPENAI_API_KEY",)

    def test_the_template_only_carries_keys_the_user_must_touch(self) -> None:
        """把 `defaults()` 整份倒进去会让四十多个字段都被记成「用户设的」。"""
        document = sample().document
        assert set(document) == {SCHEMA_KEY, "model", "plugins"}
        assert set(document) < set(defaults()) | {SCHEMA_KEY, "model", "plugins"}

    def test_the_credential_is_a_sibling_of_config_not_inside_it(self) -> None:
        """`D19`/`D23` 定死的分界：凭据不在插件自己的配置块里（`CFG-003`）。"""
        entry = sample().document["plugins"]["model-openai"]  # type: ignore[index]
        assert "config" not in entry
        assert entry["secrets"] == {"api_key": SECRET_REF}

    def test_the_schema_reference_can_be_left_out(self) -> None:
        initial = build_initial_config(model_name="m", schema_ref=None)
        assert SCHEMA_KEY not in initial.document

    def test_rendering_is_deterministic(self) -> None:
        assert sample().text == sample().text


class TestTopLevelSchemaKey:
    """`$schema` 是全项目第二处对未知键让路的地方，因此它必须是**具名的一条**。"""

    def test_a_bare_schema_key_is_accepted(self) -> None:
        validate_config({SCHEMA_KEY: "./config.schema.json"})

    def test_other_unknown_top_level_keys_are_still_rejected(self) -> None:
        with pytest.raises(KaryviaError) as caught:
            validate_config({"$turn": {}})
        assert caught.value.code is ErrorCode.CONFIG_UNKNOWN_FIELD

    def test_a_mistyped_section_is_still_rejected(self) -> None:
        """放行的是一个具名键，不是「以 `$` 开头就放行」——后者会让拼错的小节静默消失。"""
        with pytest.raises(KaryviaError):
            validate_config({"turnn": {}})


# ------------------------------------------------------------------- JSON Schema


class TestConfigJsonSchema:
    def test_every_known_field_appears_with_its_default(self) -> None:
        """schema 是 `SECTION_SPECS` 的派生物：字段表长出新项时它自动跟着长。

        元组默认值（`STR_LIST` 的 `()`）在 schema 里是列表——JSON 没有元组，
        而这份文档要能被**直接**交给 `jsonschema.validate()`，不只是被序列化。
        """
        schema = config_json_schema()
        properties = schema["properties"]
        for section, fields in SECTION_SPECS.items():
            section_schema = properties[section]  # type: ignore[index]
            for name, spec in fields.items():
                fragment = section_schema["properties"][name]  # type: ignore[index]
                expected = list(spec.default) if isinstance(spec.default, tuple) else spec.default
                assert fragment["default"] == expected

    def test_every_field_kind_has_a_representation(self) -> None:
        """缺一种 `FieldKind` 就该在生成时 KeyError，而不是悄悄退化成「任意值」。"""
        from karyvia.kernel.config.json_schema import _KIND_SCHEMAS

        assert set(_KIND_SCHEMAS) == set(FieldKind)

    def test_choices_become_an_enum(self) -> None:
        schema = config_json_schema()
        routing = schema["properties"]["routing"]  # type: ignore[index]
        assert routing["properties"]["session_concurrency"]["enum"] == [
            "queue",
            "merge",
            "reject",
        ]

    def test_only_the_plugins_section_admits_unknown_keys(self) -> None:
        """理由与 `_validate_section` 完全相同：那些键是插件 id。"""
        properties = config_json_schema()["properties"]
        for section in SECTION_SPECS:
            section_schema = properties[section]  # type: ignore[index]
            expected = section == "plugins"
            assert bool(section_schema["additionalProperties"]) is expected

    def test_the_generated_config_validates_against_the_generated_schema(self) -> None:
        """两条判定必须给出同一个结论，否则用户会对着一份合法配置看到红波浪线。"""
        jsonschema.validate(dict(sample().document), config_json_schema())

    def test_a_full_default_document_also_validates(self) -> None:
        """默认值那一层同样要过——它是「什么都不写」时的生效配置。

        先过一次 JSON 往返：`defaults()` 里 `STR_LIST` 的默认值是元组，而落到文件里的
        永远是数组。要校验的是**文件里那份**。
        """
        jsonschema.validate(json.loads(json.dumps(defaults())), config_json_schema())

    def test_a_mistyped_field_is_caught_by_the_schema(self) -> None:
        """schema 的价值全在这里：编辑器里当场标出来，不必等下一次启动。"""
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"turn": {"max_iterations": "十六"}}, config_json_schema())

    def test_an_unknown_field_is_caught_by_the_schema(self) -> None:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"turn": {"maxIterations": 16}}, config_json_schema())

    def test_a_plugin_entry_only_takes_config_and_secrets(self) -> None:
        jsonschema.validate(
            {"plugins": {"acme": {"config": {"x": 1}, "secrets": {"k": "${V}"}}}},
            config_json_schema(),
        )
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"plugins": {"acme": {"nope": {}}}}, config_json_schema())

    def test_the_schema_document_is_json_serializable(self) -> None:
        """它要被原样写进实例目录。"""
        assert json.loads(json.dumps(config_json_schema())) == config_json_schema()
