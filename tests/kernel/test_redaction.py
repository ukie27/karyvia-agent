"""`kernel/observability/redaction.py` 的行为测试。

三类验收点：脱敏复用 `contracts.errors.redact` 且顺序是「先脱敏后截断」、条数上界生效、
`event_to_json` / `error_to_json` 产出的是真 JSON（`json.dumps` 往返）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Final

from karyvia.contracts import (
    Correlation,
    ErrorCode,
    EventName,
    InstanceId,
    KaryviaError,
    RuntimeEvent,
    SecretStr,
    SessionKey,
    TurnId,
)
from karyvia.contracts.errors import MASK
from karyvia.kernel.observability import (
    MAX_PAYLOAD_ENTRIES,
    MAX_SEQUENCE_ITEMS,
    error_to_json,
    event_to_json,
    prepare_payload,
)

SENTINEL: Final = "karyvia-sentinel-9d41ab77-do-not-leak"

CORRELATION: Final = Correlation(
    instance_id=InstanceId("inst-1"),
    session_key=SessionKey("cli", "local"),
    turn_id=TurnId("turn-1"),
)


# ------------------------------------------------------------------ 脱敏


def test_secret_str_and_sensitive_keys_are_masked() -> None:
    prepared = prepare_payload({"api_key": SENTINEL, "token": SecretStr(SENTINEL), "host": "ok"})
    assert prepared == {"api_key": MASK, "token": MASK, "host": "ok"}


def test_usage_counters_survive_redaction() -> None:
    """`errors.redact` 的整词规则不该被本模块推翻：用量统计必须原样进事件。"""
    prepared = prepare_payload({"prompt_tokens": 12, "completion_tokens": 3, "session_key": "s"})
    assert prepared == {"prompt_tokens": 12, "completion_tokens": 3, "session_key": "s"}


def test_redaction_happens_before_truncation() -> None:
    """先截断会把一个长令牌切成明文前缀，那既不再匹配已知形状，又仍然是密钥。"""
    token = "sk-" + "a" * 400
    prepared = prepare_payload({"note": f"用了 {token} 这个凭据"})
    assert token not in json.dumps(prepared)
    assert prepared["note"] == f"用了 {MASK} 这个凭据"


# ------------------------------------------------------------------ 条数上界


def test_mapping_entries_are_capped() -> None:
    prepared = prepare_payload({f"k{i}": i for i in range(MAX_PAYLOAD_ENTRIES + 10)})
    assert len(prepared) == MAX_PAYLOAD_ENTRIES + 1
    assert prepared["<dropped-entries>"] == 10


def test_sequence_items_are_capped() -> None:
    prepared = prepare_payload({"items": list(range(MAX_SEQUENCE_ITEMS + 5))})
    items = prepared["items"]
    assert isinstance(items, list)
    assert len(items) == MAX_SEQUENCE_ITEMS + 1
    assert items[-1] == "<dropped 5 items>"


def test_nested_structures_are_capped_too() -> None:
    prepared = prepare_payload({"outer": {"inner": list(range(MAX_SEQUENCE_ITEMS + 1))}})
    outer = prepared["outer"]
    assert isinstance(outer, dict)
    inner = outer["inner"]
    assert isinstance(inner, list)
    assert inner[-1] == "<dropped 1 items>"


def test_small_payloads_are_untouched() -> None:
    payload = {"a": 1, "b": [1, 2, 3], "c": {"d": None}}
    assert prepare_payload(payload) == payload


# ------------------------------------------------------------------ 序列化


def _event(**overrides: object) -> RuntimeEvent:
    base: dict[str, object] = {
        "name": EventName.TURN_STARTED,
        "sequence": 7,
        "occurred_at": datetime(2026, 8, 11, 10, 30, tzinfo=UTC),
        "instance_id": InstanceId("inst-1"),
        "correlation": CORRELATION,
    }
    base.update(overrides)
    return RuntimeEvent(**base)  # pyright: ignore[reportArgumentType]


def test_event_to_json_is_a_literal_shape() -> None:
    payload = event_to_json(_event(payload={"model": "gpt-4o"}))
    assert payload == {
        "name": "turn.started",
        "family": "turn",
        "sequence": 7,
        "occurred_at": "2026-08-11T10:30:00+00:00",
        "instance_id": "inst-1",
        "correlation": {
            "session_key": "cli~local~default",
            "turn_id": "turn-1",
            "parent_turn_id": None,
        },
        "payload": {"model": "gpt-4o"},
        "error": None,
    }
    assert json.loads(json.dumps(payload)) == payload


def test_instance_level_events_have_no_correlation() -> None:
    payload = event_to_json(_event(name=EventName.INSTANCE_STARTING, correlation=None))
    assert payload["correlation"] is None


def test_derived_turn_keeps_its_parent() -> None:
    derived = CORRELATION.derive(TurnId("turn-2"))
    payload = event_to_json(_event(correlation=derived))
    correlation = payload["correlation"]
    assert isinstance(correlation, dict)
    assert correlation["turn_id"] == "turn-2"
    assert correlation["parent_turn_id"] == "turn-1"


def test_error_to_json_carries_code_category_and_detail() -> None:
    error = KaryviaError(
        ErrorCode.CONFIG_INVALID, "配置有问题。", detail={"field": "model", "api_key": SENTINEL}
    )
    payload = error_to_json(error)
    assert payload["code"] == ErrorCode.CONFIG_INVALID.value
    assert payload["category"] == error.category.value
    assert payload["detail"] == {"field": "model", "api_key": MASK}
    assert payload["capability"] is None
    assert SENTINEL not in json.dumps(payload)


def test_event_error_is_serialized_in_place() -> None:
    error = KaryviaError(ErrorCode.EXTERNAL_MODEL_PROVIDER, "上游挂了。")
    payload = event_to_json(_event(name=EventName.MODEL_REQUEST_FAILED, error=error))
    nested = payload["error"]
    assert isinstance(nested, dict)
    assert nested["code"] == ErrorCode.EXTERNAL_MODEL_PROVIDER.value
