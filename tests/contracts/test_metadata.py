"""`normalize_metadata()` 的上限与归一化测试（`D03`，需求 §10.2 校验规则、`MSG-004`）。

四项上限各有一个超限用例；「非 JSON 值必须报错而不是被静默转换」是本模块最重要的
性质——它是 Channel 归一化没做完时唯一的报警器。
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from karyvia.contracts import ErrorCategory, ErrorCode, KaryviaError, normalize_metadata
from karyvia.contracts.metadata import (
    MAX_METADATA_BYTES,
    MAX_METADATA_DEPTH,
    MAX_METADATA_ENTRIES,
    MAX_METADATA_KEY_LENGTH,
)


class _SdkObject:
    """冒充第三方 SDK 对象：既不是 JSON 值，也没有可用的序列化形式。"""


def test_none_yields_shared_empty_mapping() -> None:
    assert normalize_metadata(None) == {}


def test_nested_values_are_deep_frozen() -> None:
    """快照语义：调用方事后改自己那份 dict，影响不到已构造的契约对象。"""
    source = {"telegram": {"chat": {"id": 1}}, "tags": ["a", "b"]}
    frozen = normalize_metadata(source)

    source["telegram"] = {"chat": {"id": 999}}
    assert frozen["telegram"] == MappingProxyType({"chat": MappingProxyType({"id": 1})})
    assert frozen["tags"] == ("a", "b")

    with pytest.raises(TypeError):
        frozen["telegram"]["chat"]["id"] = 2  # pyright: ignore[reportIndexIssue]


def test_bool_is_not_treated_as_int() -> None:
    """`isinstance(True, int)` 为真，判断顺序错了会把布尔值算成数字。"""
    assert normalize_metadata({"ok": True})["ok"] is True


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("条数超限", {f"k{index}": 1 for index in range(MAX_METADATA_ENTRIES + 1)}),
        ("字节超限", {"blob": "x" * (MAX_METADATA_BYTES + 1)}),
        ("键名超长", {"k" * (MAX_METADATA_KEY_LENGTH + 1): 1}),
    ],
)
def test_size_limits_reject(label: str, payload: dict[str, object]) -> None:
    with pytest.raises(KaryviaError) as exc:
        normalize_metadata(payload)
    assert exc.value.code is ErrorCode.INPUT_TOO_LARGE, label
    assert exc.value.category is ErrorCategory.INVALID_INPUT


def test_depth_limit_rejects() -> None:
    payload: dict[str, object] = {"leaf": 1}
    for _ in range(MAX_METADATA_DEPTH + 1):
        payload = {"nest": payload}
    with pytest.raises(KaryviaError) as exc:
        normalize_metadata(payload)
    assert exc.value.code is ErrorCode.INPUT_TOO_LARGE


def test_depth_at_limit_is_accepted() -> None:
    """边界值必须通过，否则上限就成了「上限减一」。"""
    payload: dict[str, object] = {"leaf": 1}
    for _ in range(MAX_METADATA_DEPTH - 2):
        payload = {"nest": payload}
    assert normalize_metadata(payload)


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("SDK 对象", {"chat": _SdkObject()}),
        ("嵌套 SDK 对象", {"chat": {"raw": _SdkObject()}}),
        ("字节串", {"blob": b"\x00\x01"}),
        ("非字符串键", {1: "x"}),
    ],
)
def test_non_json_values_are_rejected(label: str, payload: dict[object, object]) -> None:
    """静默 `str()` 会让问题推迟到持久化层才炸，因此这里直接失败（`MSG-004`）。"""
    with pytest.raises(KaryviaError) as exc:
        normalize_metadata(payload)  # pyright: ignore[reportArgumentType]
    assert exc.value.code is ErrorCode.INPUT_MALFORMED, label


def test_field_name_reaches_detail() -> None:
    """`detail` 必须指出是哪个字段的元数据出了问题，否则四处 metadata 无从定位。"""
    with pytest.raises(KaryviaError) as exc:
        normalize_metadata({"x": _SdkObject()}, field="tool_result.data")
    assert exc.value.detail["field"] == "tool_result.data"
