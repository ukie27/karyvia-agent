"""错误契约测试（`D02`，需求 §10.7、`OBS-003`、`OBS-004`）。

核心验收：携带哨兵密钥构造 `KaryviaError` 后，`user_message`、`detail`、`repr`、`str`
与 `args` 均不含哨兵值——脱敏发生在构造时，下游忘记处理也不会泄漏。
"""

from __future__ import annotations

import copy
import dataclasses

import pytest

from karyvia.contracts import (
    CODE_CATEGORIES,
    ErrorCategory,
    ErrorCode,
    KaryviaError,
    SecretStr,
    redact,
)
from karyvia.contracts.errors import (
    MASK,
    MAX_DETAIL_STRING_LENGTH,
    UnknownErrorCodeError,
    key_words,
    scrub,
)
from karyvia.contracts.ids import Correlation, InstanceId, SessionKey, TurnId

SENTINEL = "S3NT1NEL-do-not-leak-9f2a7c"


# ------------------------------------------------------------------ 错误码登记表


def test_every_code_is_registered() -> None:
    missing = [code for code in ErrorCode if code not in CODE_CATEGORIES]
    assert not missing, f"错误码未登记分类：{missing}"


def test_every_category_has_at_least_one_code() -> None:
    covered = set(CODE_CATEGORIES.values())
    assert covered == set(ErrorCategory)


def test_category_is_derived_not_supplied() -> None:
    """分类由码推导，杜绝同一个码在不同抛出点被归入不同类别。"""
    for code, category in CODE_CATEGORIES.items():
        assert KaryviaError(code, "x").category is category


def test_unknown_code_is_a_programming_error() -> None:
    with pytest.raises(UnknownErrorCodeError):
        KaryviaError("not.a.registered.code", "x")  # pyright: ignore[reportArgumentType]


# ------------------------------------------------------------------ 脱敏


def test_sentinel_never_survives_construction() -> None:
    error = KaryviaError(
        ErrorCode.EXTERNAL_MODEL_PROVIDER,
        f"调用失败，使用的密钥是 {SENTINEL}",
        detail={"api_key": SENTINEL, "endpoint": "https://example.invalid/v1"},
    )

    for rendered in (error.user_message, repr(error), str(error), repr(error.args)):
        assert SENTINEL not in rendered
    assert SENTINEL not in repr(dict(error.detail))
    assert error.detail["api_key"] == MASK
    # 非敏感字段照常保留，脱敏不能把诊断信息一起抹掉。
    assert error.detail["endpoint"] == "https://example.invalid/v1"


@pytest.mark.parametrize(
    "key",
    [
        "api_key",
        "apiKey",
        "APIKey",
        "ACCESS_KEY",
        "refresh_token",
        "client_secret",
        "Authorization",
        "cookie",
        "private_key",
    ],
)
def test_sensitive_key_names_are_masked(key: str) -> None:
    error = KaryviaError(ErrorCode.CONFIG_INVALID, "配置无效", detail={key: SENTINEL})
    assert error.detail[key] == MASK


@pytest.mark.parametrize(
    "key",
    [
        # 用量统计：整词比对下 "tokens" ≠ "token"，限定词又会一票否决。
        "tokens",
        "prompt_tokens",
        "input_tokens",
        "token_count",
        "token_limit",
        "token_usage",
        # 裸 "key" 不算密钥。
        "session_key",
        "cache_key",
        "storage_key",
        "key",
        # 其余常规诊断字段。
        "auth_type",
        "signature_algorithm_count",
        "path",
    ],
)
def test_benign_key_names_survive(key: str) -> None:
    """脱敏不能把可观测性一起打掉——用量统计和会话标识都必须原样保留。"""
    error = KaryviaError(ErrorCode.EXTERNAL_MODEL_PROVIDER, "调用失败", detail={key: 1234})
    assert error.detail[key] == 1234


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("APIKey", ("api", "key")),
        ("apiKey", ("api", "key")),
        ("API_KEY", ("api", "key")),
        ("prompt_tokens", ("prompt", "tokens")),
        ("oauth2Token", ("oauth", "2", "token")),
    ],
)
def test_key_words_splits_on_word_boundaries(key: str, expected: tuple[str, ...]) -> None:
    assert key_words(key) == expected


def test_session_key_is_not_treated_as_secret() -> None:
    """会话标识不是密钥；把它打码会让诊断失去主线索。"""
    error = KaryviaError(
        ErrorCode.PERSISTENCE_READ_FAILED,
        "读取会话失败",
        detail={"session_key": "cli~local~default"},
    )
    assert error.detail["session_key"] == "cli~local~default"


@pytest.mark.parametrize(
    "value",
    [
        "sk-abcdefghijklmnopqrstuvwxyz012345",
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123",
        "xoxb-1234567890-abcdefghij",
        "AKIAIOSFODNN7EXAMPLE",
        "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
    ],
)
def test_known_token_shapes_are_masked_regardless_of_key_name(value: str) -> None:
    """键名没命中时，值的形状是第二道防线。"""
    error = KaryviaError(ErrorCode.CONFIG_INVALID, f"来自 {value}", detail={"note": value})
    assert value not in str(error.detail["note"])
    assert value not in error.user_message


def test_nested_structures_are_redacted() -> None:
    redacted, secrets = redact(
        {"provider": {"name": "openai", "credentials": {"token": SENTINEL}}, "tags": ["a", "b"]}
    )
    provider = redacted["provider"]
    assert isinstance(provider, dict)
    assert provider["credentials"] == MASK
    assert provider["name"] == "openai"
    assert redacted["tags"] == ["a", "b"]
    assert SENTINEL in secrets


def test_redaction_stops_at_depth_limit() -> None:
    payload: dict[str, object] = {"leaf": 1}
    for _ in range(12):
        payload = {"nested": payload}
    redacted, _ = redact(payload)
    assert "<depth-limit>" in repr(redacted)


def test_long_strings_are_truncated() -> None:
    error = KaryviaError(
        ErrorCode.INPUT_TOO_LARGE, "输入过大", detail={"body": "x" * (MAX_DETAIL_STRING_LENGTH + 50)}
    )
    body = error.detail["body"]
    assert isinstance(body, str)
    assert body.endswith("<truncated 50 chars>")


def test_non_json_values_keep_type_name_only() -> None:
    redacted, _ = redact({"handle": object()})
    assert redacted["handle"] == "<unsupported:object>"


def test_scrub_replaces_longest_secret_first() -> None:
    """短密文先替换会把长密文切碎并留下残片，因此必须按长度倒序。"""
    assert scrub("abcdefgh 与 abcdefghijkl", ["abcdefgh", "abcdefghijkl"]) == f"{MASK} 与 {MASK}"


def test_scrub_ignores_too_short_secrets() -> None:
    assert scrub("id=42", ["42"]) == "id=42"


# ------------------------------------------------------------------ SecretStr


def test_secret_str_is_masked_by_redaction_regardless_of_key_name() -> None:
    """键名无辜（`endpoint`）也照样打码：类型本身就是「这是密钥」的声明。"""
    redacted, found = redact({"endpoint": SecretStr("plaintext-value")})

    assert redacted["endpoint"] == MASK
    assert found == frozenset({"plaintext-value"})


def test_secret_str_under_a_sensitive_key_still_yields_its_plaintext_for_scrubbing() -> None:
    """敏感键名走的是整体打码分支，`SecretStr` 的明文同样要进密文集合。

    否则同一个值被拼进 `user_message` 时 `scrub()` 认不出它。
    """
    redacted, found = redact({"api_key": SecretStr("plaintext-value")})

    assert redacted["api_key"] == MASK
    assert "plaintext-value" in found


def test_secret_str_is_not_a_dataclass() -> None:
    """做成 dataclass 会让 `dataclasses.asdict()` 把明文抖出来（`D11`）。"""
    assert not dataclasses.is_dataclass(SecretStr)


def test_secret_str_survives_deepcopy_as_itself() -> None:
    secret = SecretStr("plaintext-value")

    assert copy.deepcopy(secret) is secret
    assert copy.copy(secret) is secret


# ------------------------------------------------------------------ 不可变与关联


def test_detail_is_read_only() -> None:
    error = KaryviaError(ErrorCode.CONFIG_INVALID, "配置无效", detail={"path": "/x"})
    with pytest.raises(TypeError):
        error.detail["path"] = "/y"  # pyright: ignore[reportIndexIssue]


def test_attributes_are_read_only() -> None:
    error = KaryviaError(ErrorCode.CONFIG_INVALID, "配置无效")
    with pytest.raises(AttributeError):
        error.code = ErrorCode.KERNEL_UNEXPECTED  # pyright: ignore[reportAttributeAccessIssue]


def test_with_correlation_returns_new_instance() -> None:
    correlation = Correlation(
        instance_id=InstanceId("default"),
        session_key=SessionKey("cli", "local"),
        turn_id=TurnId("t-1"),
    )
    original = KaryviaError(ErrorCode.TIMEOUT_TOOL_CALL, "工具超时", retryable=True)
    attached = original.with_correlation(correlation)

    assert original.correlation is None
    assert attached.correlation is correlation
    assert attached.code is original.code
    assert attached.retryable is True


def test_is_an_exception() -> None:
    with pytest.raises(KaryviaError):
        raise KaryviaError(ErrorCode.KERNEL_UNEXPECTED, "内部错误")
