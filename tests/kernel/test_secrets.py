"""`kernel/config/secrets.py` 的行为测试。

覆盖四类验收点：`SecretStr` 的全部输出路径都不泄漏（哨兵扫描）、`${VAR}` 的扫描与解析
（含 JSON Pointer 位置与转义）、缺失变量只报变量名、写回往返保留 `${VAR}` 字面量。

哨兵值 `SENTINEL` 只在本文件里出现一次，断言一律是「它不出现在这段输出里」——
用一个固定长串而不是随手写的短值，是为了让 `in` 判定不会被巧合的子串命中。
"""

from __future__ import annotations

import dataclasses
import io
import json
import logging
from typing import Final

import pytest

from karyvia.contracts import ErrorCode, KaryviaError, SecretStr
from karyvia.contracts.errors import MASK
from karyvia.kernel.config import (
    SecretMap,
    SecretRef,
    contains_secret_ref,
    prepare_for_write,
    resolve_secrets,
    resolve_text,
    scan_secret_refs,
    secret_ref_names,
)

#: 哨兵：任何输出里出现它都是泄漏。
SENTINEL: Final = "karyvia-sentinel-4f2b8c1e-do-not-leak"

ENV: Final = {"NM_TEST_KEY": SENTINEL, "NM_TEST_HOST": "example.invalid"}


# --------------------------------------------------------------------- SecretStr


def test_secret_str_renders_as_mask_on_every_formatting_path() -> None:
    """`str` / `repr` / f-string / `format()` / `%` 全部只得到掩码。"""
    secret = SecretStr(SENTINEL)

    rendered = [
        str(secret),
        repr(secret),
        f"{secret}",
        f"{secret!s}",
        f"{secret!r}",
        f"{secret:>20}",
        format(secret),
        format(secret, "^30"),
        "%s" % (secret,),
        "{}".format(secret),
    ]

    assert all(SENTINEL not in text for text in rendered), rendered
    assert all(MASK in text for text in rendered), rendered


def test_secret_str_reveals_plaintext_only_through_reveal() -> None:
    assert SecretStr(SENTINEL).reveal() == SENTINEL


def test_secret_str_is_not_json_serializable() -> None:
    """`json.dumps` 必须抛 `TypeError` 而不是写出任何东西。

    「大声失败」在这里比「静默掩码」正确：一个密钥被送进 JSON 序列化，说明调用方本来
    就打算把它写到某处，那是要被发现的，不是要被悄悄美化的。
    """
    with pytest.raises(TypeError):
        json.dumps({"api_key": SecretStr(SENTINEL)})


def test_secret_str_is_opaque_to_dataclasses_asdict() -> None:
    """`dataclasses.asdict()` 不得抖出明文——这正是它不做成 dataclass 的原因。

    `asdict` 对非 dataclass 字段走 `copy.deepcopy`，因此拿到的还是 `SecretStr` 本身；
    如果哪天有人把它改回 `@dataclass`，这条断言会立刻变成 `{"_value": SENTINEL}`。
    """

    @dataclasses.dataclass
    class Holder:
        api_key: SecretStr

    dumped = dataclasses.asdict(Holder(api_key=SecretStr(SENTINEL)))

    assert SENTINEL not in repr(dumped)
    assert isinstance(dumped["api_key"], SecretStr)


def test_secret_str_is_immutable() -> None:
    secret = SecretStr(SENTINEL)
    with pytest.raises(AttributeError):
        secret._value = "other"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        del secret._value  # type: ignore[misc]


def test_secret_str_equality_and_hash_are_by_value() -> None:
    assert SecretStr(SENTINEL) == SecretStr(SENTINEL)
    assert SecretStr(SENTINEL) != SecretStr("other")
    assert SecretStr(SENTINEL) != SENTINEL  # 与裸串不相等：类型本身是「已包装」的标记
    assert len({SecretStr(SENTINEL), SecretStr(SENTINEL)}) == 1


def test_secret_str_does_not_leak_through_logging() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("karyvia.test.secrets")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("api_key=%s repr=%r", SecretStr(SENTINEL), SecretStr(SENTINEL))
    finally:
        logger.removeHandler(handler)

    assert SENTINEL not in stream.getvalue()
    assert MASK in stream.getvalue()


def test_karyvia_error_masks_secret_in_detail_and_scrubs_it_from_the_message() -> None:
    """`redact` 认得 `SecretStr`，且明文进入密文集合后会被 `scrub` 从消息里擦掉。

    第二条是重点：只按键名脱敏挡不住「凭据被顺手拼进了 user_message」。
    """
    error = KaryviaError(
        ErrorCode.CONFIG_INVALID,
        f"provider 拒绝了凭据 {SENTINEL}。",
        detail={"credential": SecretStr(SENTINEL), "pointer": "/model/api_key"},
    )

    assert SENTINEL not in str(error)
    assert SENTINEL not in error.user_message
    assert SENTINEL not in repr(dict(error.detail))
    assert error.detail["credential"] == MASK


# ------------------------------------------------------------------------- 扫描


def test_scan_finds_refs_with_json_pointer_positions() -> None:
    document = {
        "model": {"api_key": "${NM_TEST_KEY}"},
        "net": {"base": "https://${NM_TEST_HOST}/v1"},
        "plugins": {"paths": ["plain", "${NM_TEST_KEY}"]},
        "turn": {"max_iterations": 8},
    }

    refs = scan_secret_refs(document)

    assert [ref.pointer for ref in refs] == [
        "/model/api_key",
        "/net/base",
        "/plugins/paths/1",  # 列表下标也是一段
    ]
    assert refs[1].literal == "https://${NM_TEST_HOST}/v1"
    assert refs[1].names == ("NM_TEST_HOST",)


def test_scan_escapes_pointer_tokens() -> None:
    """插件 id 里可能有 `/` 或 `~`，位置必须按 RFC 6901 转义。"""
    refs = scan_secret_refs({"plugins": {"a/b~c": {"api_key": "${NM_TEST_KEY}"}}})

    assert [ref.pointer for ref in refs] == ["/plugins/a~1b~0c/api_key"]


def test_scan_does_not_read_the_environment() -> None:
    """纯扫描：变量一个都没导出也不该失败（`karyvia doctor` 要靠它列出待补的变量）。"""
    refs = scan_secret_refs({"model": {"api_key": "${NM_NEVER_SET_ANYWHERE}"}})

    assert refs[0].names == ("NM_NEVER_SET_ANYWHERE",)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("${NM_TEST_KEY}", ("NM_TEST_KEY",)),
        ("Bearer ${NM_TEST_KEY}", ("NM_TEST_KEY",)),
        ("${A}-${B}-${A}", ("A", "B")),  # 重复只算一次，顺序按首次出现
        ("no refs here", ()),
        ("${}", ()),  # 空名不是引用

        ("${a b}", ()),  # 含空格不是引用

        ("$NM_TEST_KEY", ()),  # 无花括号不是引用

    ],
)
def test_secret_ref_names(text: str, expected: tuple[str, ...]) -> None:
    assert secret_ref_names(text) == expected
    assert contains_secret_ref(text) is bool(expected)


def test_dollar_escape_is_not_supported() -> None:
    """`$${VAR}` 不是转义，仍然被当成引用——这是刻意的，见模块 docstring。"""
    assert secret_ref_names("$${NM_TEST_KEY}") == ("NM_TEST_KEY",)


# ------------------------------------------------------------------------- 解析


def test_resolve_keeps_plaintext_out_of_the_document() -> None:
    """解析结果不是一份替换过的文档：文档自始至终持有 `${VAR}` 字面量。"""
    document = {"model": {"api_key": "${NM_TEST_KEY}"}}

    secrets = resolve_secrets(document, env=ENV)

    assert document == {"model": {"api_key": "${NM_TEST_KEY}"}}  # 未被改写
    assert SENTINEL not in json.dumps(document)
    assert secrets.at("model", "api_key") == SecretStr(SENTINEL)
    assert secrets.literal_at("model", "api_key") == "${NM_TEST_KEY}"


def test_resolve_substitutes_embedded_refs_and_wraps_the_whole_value() -> None:
    """内嵌引用也解析，且**整个值**成为密钥——一种机制一种含义。"""
    secrets = resolve_secrets({"net": {"base": "https://${NM_TEST_HOST}/v1"}}, env=ENV)

    assert secrets.at("net", "base") == SecretStr("https://example.invalid/v1")


def test_resolve_handles_multiple_refs_in_one_value() -> None:
    secrets = resolve_secrets(
        {"h": "${NM_TEST_HOST}:${NM_TEST_KEY}:${NM_TEST_HOST}"}, env=ENV
    )

    assert secrets.at("h") == SecretStr(f"example.invalid:{SENTINEL}:example.invalid")


def test_resolve_exposes_variables_by_name() -> None:
    secrets = resolve_secrets({"model": {"api_key": "${NM_TEST_KEY}"}}, env=ENV)

    assert secrets.variable("NM_TEST_KEY") == SecretStr(SENTINEL)
    assert secrets.variable("NM_TEST_HOST") is None  # 没被引用就不收集


def test_resolve_returns_empty_map_for_a_document_without_refs() -> None:
    secrets = resolve_secrets({"turn": {"max_iterations": 8}}, env=ENV)

    assert not secrets
    assert len(secrets) == 0
    assert secrets.at("turn", "max_iterations") is None


def test_secret_map_repr_reports_counts_only() -> None:
    secrets = resolve_secrets({"model": {"api_key": "${NM_TEST_KEY}"}}, env=ENV)

    assert SENTINEL not in repr(secrets)
    assert repr(secrets) == "SecretMap(refs=1, variables=1)"


def test_resolve_text_returns_plain_string_when_there_is_no_ref() -> None:
    """没有引用就原样返回 `str`：「这个值来自环境变量」是要保住的信息。"""
    assert resolve_text("gpt-4o", env=ENV) == "gpt-4o"
    assert resolve_text("${NM_TEST_KEY}", env=ENV) == SecretStr(SENTINEL)


# ----------------------------------------------------------------- 缺失变量


def test_missing_variable_reports_the_name_only() -> None:
    with pytest.raises(KaryviaError) as excinfo:
        resolve_secrets({"model": {"api_key": "${NM_ABSENT_KEY}"}}, env={})

    error = excinfo.value
    assert error.code is ErrorCode.CONFIG_SECRET_MISSING
    assert "NM_ABSENT_KEY" in error.user_message
    assert error.detail["missing"] == [
        {"name": "NM_ABSENT_KEY", "pointer": "/model/api_key", "reason": "unset"}
    ]


def test_empty_variable_counts_as_missing_with_its_own_reason() -> None:
    """`OPENAI_API_KEY=` 几乎总是配错；静默接受只会把报错推到第一次模型调用。"""
    with pytest.raises(KaryviaError) as excinfo:
        resolve_secrets({"model": {"api_key": "${NM_EMPTY}"}}, env={"NM_EMPTY": "   "})

    assert excinfo.value.detail["missing"] == [
        {"name": "NM_EMPTY", "pointer": "/model/api_key", "reason": "empty"}
    ]


def test_all_missing_variables_are_reported_at_once() -> None:
    """一次报全：缺三个变量是首次配置的常态，逐条抛出会让用户重启三次。"""
    with pytest.raises(KaryviaError) as excinfo:
        resolve_secrets(
            {"a": "${NM_ONE}", "b": {"c": "${NM_TWO}"}, "d": "${NM_THREE}"},
            env={},
        )

    reported = {entry["name"] for entry in excinfo.value.detail["missing"]}
    assert reported == {"NM_ONE", "NM_TWO", "NM_THREE"}


def test_missing_variable_error_carries_no_values() -> None:
    """哨兵：另一个变量已导出时，它的值不得出现在错误里。"""
    with pytest.raises(KaryviaError) as excinfo:
        resolve_secrets(
            {"a": "${NM_TEST_KEY}", "b": "${NM_ABSENT_KEY}"},
            env=ENV,
        )

    error = excinfo.value
    assert SENTINEL not in error.user_message
    assert SENTINEL not in json.dumps(dict(error.detail), ensure_ascii=False)


def test_resolve_text_missing_variable_records_the_given_pointer() -> None:
    with pytest.raises(KaryviaError) as excinfo:
        resolve_text("${NM_ABSENT_KEY}", env={}, pointer="/plugins/acme/config/api_key")

    assert excinfo.value.detail["missing"] == [
        {
            "name": "NM_ABSENT_KEY",
            "pointer": "/plugins/acme/config/api_key",
            "reason": "unset",
        }
    ]


# ------------------------------------------------------------- 写回


def test_write_back_round_trip_preserves_the_literal() -> None:
    """读取 -> 改别的字段 -> 写回：`${VAR}` 字面量原样保留。"""
    document = {
        "model": {"api_key": "${NM_TEST_KEY}", "name": "gpt-4o"},
        "turn": {"max_iterations": 8},
    }
    secrets = resolve_secrets(document, env=ENV)

    updated = {**document, "turn": {"max_iterations": 32}}
    writable = prepare_for_write(updated, secrets)

    assert writable == {
        "model": {"api_key": "${NM_TEST_KEY}", "name": "gpt-4o"},
        "turn": {"max_iterations": 32},
    }
    assert SENTINEL not in json.dumps(writable)


def test_write_back_replaces_a_secret_object_with_its_literal() -> None:
    """有人把 `SecretStr` 塞回文档时，写回把它换成字面量而不是明文。"""
    document = {"model": {"api_key": "${NM_TEST_KEY}"}}
    secrets = resolve_secrets(document, env=ENV)

    writable = prepare_for_write({"model": {"api_key": secrets.at("model", "api_key")}}, secrets)

    assert writable == {"model": {"api_key": "${NM_TEST_KEY}"}}


def test_write_back_replaces_a_secret_object_that_moved_position() -> None:
    """值被搬到别的字段时按明文反查，仍然写得出引用。"""
    document = {"model": {"api_key": "${NM_TEST_KEY}"}}
    secrets = resolve_secrets(document, env=ENV)

    writable = prepare_for_write({"backup": {"key": secrets.at("model", "api_key")}}, secrets)

    assert writable == {"backup": {"key": "${NM_TEST_KEY}"}}


def test_write_back_replaces_revealed_plaintext() -> None:
    """`reveal()` 之后塞回来的裸明文同样换回引用——这是那道闸的主要用途。"""
    document = {"model": {"api_key": "${NM_TEST_KEY}"}}
    secrets = resolve_secrets(document, env=ENV)

    writable = prepare_for_write({"model": {"api_key": SENTINEL}}, secrets)

    assert writable == {"model": {"api_key": "${NM_TEST_KEY}"}}
    assert SENTINEL not in json.dumps(writable)


def test_write_back_refuses_a_secret_it_cannot_express_as_a_reference() -> None:
    """换不回去就抛错。「找不到来源就写明文」是这条防线唯一不能有的行为。"""
    secrets = resolve_secrets({"model": {"api_key": "${NM_TEST_KEY}"}}, env=ENV)

    with pytest.raises(KaryviaError) as excinfo:
        prepare_for_write({"other": SecretStr("from-somewhere-else")}, secrets)

    assert excinfo.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED
    assert excinfo.value.detail["pointer"] == "/other"


def test_write_back_refuses_non_json_values() -> None:
    """非 JSON 形状的值不 `str()` 它：`str()` 正是明文泄漏最爱走的那条路。"""
    with pytest.raises(KaryviaError) as excinfo:
        prepare_for_write({"path": object()}, SecretMap())

    assert excinfo.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED
    assert excinfo.value.detail["actual_type"] == "object"


def test_write_back_leaves_ordinary_values_alone() -> None:
    """短的普通值不因为「碰巧等于某个密钥」被改写：那是在悄悄改用户的配置。"""
    secrets = resolve_secrets({"model": {"api_key": "${NM_SHORT}"}}, env={"NM_SHORT": "1234"})

    writable = prepare_for_write({"turn": {"note": "1234", "max_iterations": 8}}, secrets)

    assert writable == {"turn": {"note": "1234", "max_iterations": 8}}


def test_write_back_still_restores_a_short_secret_by_position() -> None:
    """按明文反查有长度阈值，按**位置**恢复没有——那条不需要猜。"""
    document = {"model": {"api_key": "${NM_SHORT}"}}
    secrets = resolve_secrets(document, env={"NM_SHORT": "1234"})

    writable = prepare_for_write({"model": {"api_key": secrets.at("model", "api_key")}}, secrets)

    assert writable == {"model": {"api_key": "${NM_SHORT}"}}


def test_write_back_refuses_a_moved_short_secret_instead_of_guessing() -> None:
    """短值 + 换了位置 = 反查不可靠，此时抛错。不确定就拒写，绝不写明文。"""
    secrets = resolve_secrets({"model": {"api_key": "${NM_SHORT}"}}, env={"NM_SHORT": "1234"})

    with pytest.raises(KaryviaError) as excinfo:
        prepare_for_write({"backup": secrets.at("model", "api_key")}, secrets)

    assert excinfo.value.code is ErrorCode.KERNEL_INVARIANT_VIOLATED


def test_write_back_preserves_document_shape() -> None:
    document = {"a": [1, 2.5, True, None, "x"], "b": {"c": []}}

    assert prepare_for_write(document, SecretMap()) == document


def test_secret_ref_is_frozen() -> None:
    ref = SecretRef(pointer="/model/api_key", literal="${NM_TEST_KEY}", names=("NM_TEST_KEY",))
    with pytest.raises(dataclasses.FrozenInstanceError):
        ref.pointer = "/other"  # type: ignore[misc]
