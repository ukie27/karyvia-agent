""" 的配置扩展：`plugins.<plugin_id>.{config,secrets}`。

职责：验插件条目的形状校验、保留键与插件 id 的边界、诊断视图里的呈现，
以及「凭据只以 `${VAR}` 引用形式出现」这条。
不负责：解析 `${VAR}`（`test_secrets.py`）、把块交给插件（`tests/runtime/test_bootstrap.py`）。
"""

from __future__ import annotations

import json

import pytest

from karyvia.contracts import ErrorCode, JsonValue, KaryviaError
from karyvia.kernel.config import PluginEntry, validate_config
from karyvia.kernel.config.plugin_blocks import (
    ENTRY_KEYS,
    RESERVED_PLUGIN_KEYS,
    entries_to_json,
)


def _plugins(**entries: JsonValue) -> dict[str, JsonValue]:
    return {"plugins": dict(entries)}


def test_a_plugin_entry_is_parsed_into_config_and_secrets() -> None:
    config = validate_config(
        _plugins(**{"model-openai": {"config": {"base_url": "x"}, "secrets": {"api_key": "${T}"}}})
    )
    entry = config.plugins.entry("model-openai")
    assert entry.config == {"base_url": "x"}
    assert entry.secrets == {"api_key": "${T}"}


def test_an_unconfigured_plugin_gets_an_empty_entry() -> None:
    """没配过不是错误——每个字段都有默认值的插件应当免配置可用。"""
    assert validate_config({}).plugins.entry("whatever") == PluginEntry()


def test_reserved_keys_are_not_plugin_ids() -> None:
    config = validate_config(_plugins(enabled=["acme"], disable=["a"]))
    assert config.plugins.enabled == ("acme",)
    assert config.plugins.disable == ("a",)
    assert config.plugins.entries == {}
    assert set(RESERVED_PLUGIN_KEYS) == {
        "enabled",
        "disable",
        "stop_timeout_ms",
    }
    # 保留键与插件 id 共用一个命名空间，但撞不上：插件 id 不允许下划线，而带下划线的
    # 保留键因此永远不是一个合法的 id（`sdk/manifest.py` 的 `_ID_CHARS`）。
    assert all(key.islower() and " " not in key for key in RESERVED_PLUGIN_KEYS)


def test_an_unknown_top_level_field_is_still_rejected() -> None:
    """`extra="forbid"` 只在 `plugins` 小节里让位给插件 id，别处一个字都没松。"""
    with pytest.raises(KaryviaError) as caught:
        validate_config({"turn": {"nope": 1}})
    assert caught.value.code is ErrorCode.CONFIG_UNKNOWN_FIELD


@pytest.mark.parametrize(
    ("document", "pointer"),
    [
        (_plugins(acme=3), "/plugins/acme"),
        (_plugins(acme={"nope": {}}), "/plugins/acme/nope"),
        (_plugins(acme={"config": 3}), "/plugins/acme/config"),
        (_plugins(acme={"secrets": 3}), "/plugins/acme/secrets"),
        (_plugins(acme={"secrets": {"k": 3}}), "/plugins/acme/secrets/k"),
    ],
)
def test_malformed_entries_report_their_pointer(
    document: dict[str, JsonValue], pointer: str
) -> None:
    """每处问题都要带 JSON Pointer，否则用户不知道该改哪一行。"""
    with pytest.raises(KaryviaError) as caught:
        validate_config(document)
    pointers = [item["pointer"] for item in caught.value.detail["errors"]]
    assert pointer in pointers


def test_all_problems_are_reported_at_once() -> None:
    """改一个键、重启、再看到下一个错误不是可接受的启动体验。"""
    with pytest.raises(KaryviaError) as caught:
        validate_config(_plugins(a=3, b={"nope": 1}))
    assert len(caught.value.detail["errors"]) == 2


def test_entry_keys_are_exactly_config_and_secrets() -> None:
    """未实现的策略键不能提前放行，否则用户会误以为配置已经生效。"""
    assert ENTRY_KEYS == ("config", "secrets")


def test_the_diagnostic_view_keeps_the_reference_literal() -> None:
    """`/config` 与 `karyvia config show` 看到的是 `${VAR}` 字面量，不是明文。"""
    config = validate_config(_plugins(acme={"secrets": {"api_key": "${OPENAI_API_KEY}"}}))
    document = json.dumps(config.to_json(), ensure_ascii=False)
    assert "${OPENAI_API_KEY}" in document
    assert entries_to_json(config.plugins.entries)["acme"] == {
        "config": {},
        "secrets": {"api_key": "${OPENAI_API_KEY}"},
    }


def test_the_document_round_trips_through_json() -> None:
    config = validate_config(_plugins(acme={"config": {"n": 1}}))
    assert json.loads(json.dumps(config.to_json()))["plugins"]["acme"]["config"] == {"n": 1}
