"""`config.json` 的第二个写入点：改一个字符串列表（`D29` 的 `runtime/config_edit.py`）。

职责：验「只改那一层」「形状不对即拒绝」「原子写回」「不存在时让用户先 karyvia init」四件事。
不负责：验 `karyvia plugins` 的语义（`tests/runtime/cli/test_plugins_cli.py`）、验首次生成
（`tests/runtime/test_first_run.py`）。

**这套用例的主角是「没被改到的东西」**：一次 `enable` 之后，用户手写的键、`$schema`、
`${VAR}` 字面量与四十多个默认值的缺席都必须原样成立。写回时多物化一层，
`karyvia config show --origins` 就再也答不出「我改过什么」。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from karyvia.contracts import ErrorCode, JsonValue, KaryviaError
from karyvia.runtime.config_edit import (
    add_to_list,
    read_document,
    remove_from_list,
    write_document,
)

_ORIGINAL = {
    "$schema": "./config.schema.json",
    "model": {"name": "gpt-4o-mini", "provider": "openai"},
    "plugins": {"model-openai": {"secrets": {"api_key": "${OPENAI_API_KEY}"}}},
}


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_ORIGINAL, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ------------------------------------------------------------------------------ 读


def test_a_missing_config_points_at_nm_init(tmp_path: Path) -> None:
    """**写**这条路上缺文件不是「空配置」：凭空造一份会绕过首次运行的模板。"""
    with pytest.raises(KaryviaError) as caught:
        read_document(tmp_path / "config.json")
    assert caught.value.code is ErrorCode.CONFIG_INVALID
    assert caught.value.detail["suggestion"] == "karyvia init"


def test_broken_json_is_reported_by_the_shared_reader(tmp_path: Path) -> None:
    """坏 JSON 的诊断沿用 `read_config_file()`，不在这里写第二份解析。"""
    path = tmp_path / "config.json"
    path.write_text("{,}", encoding="utf-8")
    with pytest.raises(KaryviaError) as caught:
        read_document(path)
    assert caught.value.code is ErrorCode.CONFIG_INVALID


# ------------------------------------------------------------------------------ 改


def test_adding_is_idempotent(config_path: Path) -> None:
    document = read_document(config_path)
    first = add_to_list(document, "plugins", "enabled", "acme")
    assert first.changed and first.values == ("acme",)
    assert add_to_list(first.document, "plugins", "enabled", "acme").changed is False


def test_removing_an_absent_item_changes_nothing(config_path: Path) -> None:
    edit = remove_from_list(read_document(config_path), "plugins", "disable", "acme")
    assert edit.changed is False


def test_the_original_document_is_not_mutated(config_path: Path) -> None:
    """纯函数：调用方手上那份不受影响，否则「改了但没写盘」就成了半生效状态。"""
    document = read_document(config_path)
    add_to_list(document, "plugins", "enabled", "acme")
    assert "enabled" not in document["plugins"]  # type: ignore[index]


@pytest.mark.parametrize(
    ("section", "value"),
    [
        ({"plugins": "nope"}, "/plugins"),
        ({"plugins": {"enabled": "acme"}}, "/plugins/enabled"),
        ({"plugins": {"enabled": ["acme", 3]}}, "/plugins/enabled/1"),
    ],
)
def test_a_wrong_shape_is_refused_with_a_pointer(
    section: dict[str, JsonValue], value: str
) -> None:
    """**不静默修正**（原则 7）：把 `"enabled": "acme"` 当成 `["acme"]` 会让用户的下一次
    `karyvia config show` 看到一份他没写过的配置。指针指到具体那一项。"""
    with pytest.raises(KaryviaError) as caught:
        add_to_list(section, "plugins", "enabled", "acme")
    assert caught.value.code is ErrorCode.CONFIG_INVALID
    assert caught.value.detail["pointer"] == value


# ------------------------------------------------------------------------------ 写


def test_only_the_edited_list_is_added(config_path: Path) -> None:
    """写回的是 `config.json` 那一层：默认值、env 与 `--set` 一个都不物化进来。"""
    edit = add_to_list(read_document(config_path), "plugins", "enabled", "acme")
    write_document(config_path, edit.document)

    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert written["plugins"]["enabled"] == ["acme"]
    # 用户写的三样东西一个字都没动，`${VAR}` 仍是字面量（`CFG-003` 的结构性保证）。
    assert written["$schema"] == _ORIGINAL["$schema"]
    assert written["model"] == _ORIGINAL["model"]
    assert written["plugins"]["model-openai"]["secrets"]["api_key"] == "${OPENAI_API_KEY}"
    # 没有凭空多出来的小节——`turn` / `routing` / `logging` 全都还在默认值层。
    assert set(written) == {"$schema", "model", "plugins"}


def test_the_write_is_atomic_and_leaves_no_temp_file(config_path: Path) -> None:
    edit = add_to_list(read_document(config_path), "plugins", "enabled", "acme")
    write_document(config_path, edit.document)
    assert list(config_path.parent.iterdir()) == [config_path]


def test_a_failed_write_leaves_the_original_untouched(config_path: Path) -> None:
    """目标是目录时 `os.replace` 必然失败——原文件因此必须一个字节都没变。"""
    before = config_path.read_text(encoding="utf-8")
    target = config_path.parent / "sub"
    target.mkdir()
    with pytest.raises(KaryviaError) as caught:
        write_document(target, {"plugins": {}})
    assert caught.value.code is ErrorCode.PERSISTENCE_WRITE_FAILED
    assert "errno" in caught.value.detail
    assert config_path.read_text(encoding="utf-8") == before
