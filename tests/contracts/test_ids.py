"""`SessionKey` 编码与 `Correlation` 的契约测试（需求）。

重点是 `storage_id()` 的两条性质：可逆（往返还原）与无碰撞（不同输入不可能同 id）。
这套编码一旦发布即为持久化契约，改动会让历史会话失联，因此测试写得比实现严。
"""

from __future__ import annotations

import dataclasses
import itertools

import pytest

from karyvia.contracts import Correlation, ErrorCategory, KaryviaError, SessionKey
from karyvia.contracts.ids import MAX_COMPONENT_LENGTH, InstanceId, TurnId

#: 专挑会撞车的分量：分隔符、转义符、空白、非 ASCII、以及互为前缀后缀的组合。
TRICKY_COMPONENTS: tuple[str, ...] = (
    "a",
    "b",
    "a:b",
    "b:c",
    "a~b",
    "a%b",
    "a%3Ab",
    "a/b",
    "a\\b",
    "a b",
    "AB",
    "ab",
    "会话",
    "..",
    "CON",
    "x" * 40,
)


def _correlation(**overrides: object) -> Correlation:
    base = {
        "instance_id": InstanceId("default"),
        "session_key": SessionKey("cli", "local"),
        "turn_id": TurnId("t-1"),
    }
    base.update(overrides)
    return Correlation(**base)  # pyright: ignore[reportArgumentType]


# ------------------------------------------------------------------ storage_id 性质


def test_storage_id_round_trips() -> None:
    for channel_id, conversation_id, scope in itertools.product(TRICKY_COMPONENTS, repeat=3):
        key = SessionKey(channel_id, conversation_id, scope)
        assert SessionKey.from_storage_id(key.storage_id()) == key


def test_storage_id_never_collides() -> None:
    """不同 `SessionKey` 必须产出不同 id——这是的全部意义。"""
    keys = [
        SessionKey(channel_id, conversation_id, scope)
        for channel_id, conversation_id, scope in itertools.product(TRICKY_COMPONENTS, repeat=3)
    ]
    ids = {key.storage_id() for key in keys}
    assert len(ids) == len(keys)


def test_separator_shift_produces_different_ids() -> None:
    """验收点：`("a","b:c")` 与 `("a:b","c")` 必须产出不同 id。"""
    left = SessionKey("a", "b:c").storage_id()
    right = SessionKey("a:b", "c").storage_id()
    assert left != right
    assert left == "a~b%3Ac~default"
    assert right == "a%3Ab~c~default"


def test_storage_id_is_filesystem_safe() -> None:
    """编码结果可直接当目录名用，不需要调用方再套一层转义。"""
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-%~")
    for channel_id, conversation_id in itertools.product(TRICKY_COMPONENTS, repeat=2):
        storage_id = SessionKey(channel_id, conversation_id).storage_id()
        assert set(storage_id) <= allowed, storage_id


def test_storage_id_is_stable_across_calls() -> None:
    key = SessionKey("telegram:main", "-100123", "project-a")
    assert key.storage_id() == key.storage_id() == "telegram%3Amain~-100123~project-a"


# ------------------------------------------------------------------ 解码与校验


@pytest.mark.parametrize(
    "storage_id",
    ["only~two", "a~b~c~d", "a~b%~c", "a~b%ZZ~c", "a~%FF~c"],
)
def test_from_storage_id_rejects_broken_encoding(storage_id: str) -> None:
    with pytest.raises(KaryviaError) as excinfo:
        SessionKey.from_storage_id(storage_id)
    assert excinfo.value.category is ErrorCategory.INVALID_INPUT


@pytest.mark.parametrize(
    ("channel_id", "conversation_id", "scope"),
    [
        ("", "c", "default"),
        ("a", "", "default"),
        ("a", "c", ""),
        ("a\n", "c", "default"),
        ("a", "c\x00", "default"),
    ],
)
def test_session_key_rejects_malformed_components(
    channel_id: str, conversation_id: str, scope: str
) -> None:
    with pytest.raises(KaryviaError) as excinfo:
        SessionKey(channel_id, conversation_id, scope)
    assert excinfo.value.category is ErrorCategory.INVALID_INPUT


def test_session_key_rejects_oversized_component() -> None:
    with pytest.raises(KaryviaError) as excinfo:
        SessionKey("cli", "x" * (MAX_COMPONENT_LENGTH + 1))
    assert excinfo.value.detail["limit"] == MAX_COMPONENT_LENGTH


def test_session_key_is_frozen_and_hashable() -> None:
    key = SessionKey("cli", "local")
    with pytest.raises(dataclasses.FrozenInstanceError):
        key.scope = "other"  # pyright: ignore[reportAttributeAccessIssue]
    assert {key, SessionKey("cli", "local")} == {key}


def test_scope_defaults_to_default() -> None:
    assert SessionKey("cli", "local").scope == "default"


# ------------------------------------------------------------------ Correlation


def test_correlation_derive_records_parent() -> None:
    parent = _correlation()
    child = parent.derive(TurnId("t-2"))

    assert child.parent_turn_id == parent.turn_id
    assert child.turn_id == TurnId("t-2")
    assert child.instance_id == parent.instance_id
    assert child.session_key == parent.session_key
    # 父子链只记一层：孙节点的父是子，不是祖父。
    assert child.derive(TurnId("t-3")).parent_turn_id == TurnId("t-2")


def test_correlation_derive_can_switch_session() -> None:
    other = SessionKey("cli", "subagent")
    child = _correlation().derive(TurnId("t-2"), session_key=other)
    assert child.session_key == other


def test_correlation_rejects_self_parent() -> None:
    with pytest.raises(KaryviaError) as excinfo:
        _correlation(parent_turn_id=TurnId("t-1"))
    assert excinfo.value.category is ErrorCategory.KERNEL_INTERNAL


@pytest.mark.parametrize("field", ["instance_id", "turn_id"])
def test_correlation_rejects_empty_identifiers(field: str) -> None:
    with pytest.raises(KaryviaError) as excinfo:
        _correlation(**{field: ""})
    assert excinfo.value.category is ErrorCategory.INVALID_INPUT


def test_correlation_is_frozen() -> None:
    correlation = _correlation()
    with pytest.raises(dataclasses.FrozenInstanceError):
        correlation.turn_id = TurnId("t-9")  # pyright: ignore[reportAttributeAccessIssue]
