"""官方插件 `anthropic` 的验收：行为、配置与 manifest（开发方案 `D32`）。

| 验收项 | 测试 |
| --- | --- |
| 通过 `ModelProviderContract` 全部用例 | `TestAnthropicModelProvider` |
| 错误映射到正确的 `ErrorCategory` 且 `retryable` 正确（`MOD-003`） | `TestFaultMapping` |
| 凭据不进日志与事件（`MOD-002`） | `TestCredentialNeverLeaks` |
| 取消语义 | `TestCancellation` |
| 配置校验在 `setup()` 时发生一次 | `TestSettings` |
| manifest 自洽、经真注册路径装得起来 | `TestManifest` |

线格式的逐字节断言在 `test_wire.py` 与 `test_decode.py`，这里只放需要真的发一次请求
（或真的解析一次配置）才成立的判定。

**全部走 `httpx.MockTransport`，一个 socket 都不开。** `conftest.py` 的 autouse 闸门是这件事
的断言而不是补充手段——`ModelProviderContract` 会不带参数地反复构造 provider 并真的发起
`complete()` / `stream()`，没有注入口就会打到真实网络。

**哨兵密钥必须匹配 `contracts/errors.py::_SECRET_VALUE_PATTERNS`**，否则「没泄漏」可能只是
因为它压根不长得像密钥——`TestCredentialNeverLeaks` 的第一条用例断言的正是这件事。
"""

from __future__ import annotations

import json
from importlib.metadata import entry_points

import httpx
import pytest
from _support import (
    BASE_URL,
    MODEL_ID,
    SENTINEL_KEY,
    collect,
    make_context,
    make_provider,
    make_request,
    make_settings,
    message_body,
    text_stream,
)
from karyvia_plugin_anthropic import (
    CACHE_TTLS,
    CAPABILITY_NAME,
    ENTRY_PROPERTIES,
    MANIFEST,
    MODEL_ENTRY_KEYS,
    THINKING_MODES,
    AnthropicModelProvider,
    error_for_event,
    error_for_status,
    error_for_transport,
    read_credential,
    resolve_settings,
    setup,
)
from karyvia_plugin_anthropic.faults import retry_after_ms
from karyvia_plugin_anthropic.provider import is_local_endpoint
from karyvia_plugin_anthropic.settings import (
    CONFIG_ANTHROPIC_VERSION_KEY,
    CONFIG_AUTH_KEY,
    CONFIG_BASE_URL_KEY,
    CONFIG_BETA_HEADERS_KEY,
    CONFIG_CACHING_KEY,
    CONFIG_CAPABILITIES_KEY,
    CONFIG_DEFAULT_MAX_OUTPUT_KEY,
    CONFIG_EFFORT_KEY,
    CONFIG_MODELS_KEY,
    CONFIG_SUPPORTS_TEMPERATURE_KEY,
    CONFIG_THINKING_KEY,
    PROVIDER_NAME,
)
from karyvia_plugin_anthropic.wire import MESSAGES_PATH

from karyvia.contracts import (
    CapabilityKind,
    ErrorCategory,
    ErrorCode,
    KaryviaError,
    ModelCapability,
)
from karyvia.sdk.testing import FakePluginContext, ManualCancel, ModelProviderContract

# ------------------------------------------------------------------------------ 契约基类


class TestAnthropicModelProvider(ModelProviderContract):
    """`ModelProvider` 的通用契约。每个用例都真的发一次请求，走 MockTransport。"""

    def make_provider(self) -> AnthropicModelProvider:
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            if payload.get("stream"):
                return httpx.Response(
                    200,
                    text=text_stream(),
                    headers={"content-type": "text/event-stream"},
                )
            return httpx.Response(200, json=message_body())

        return make_provider(handler)

    def model_id(self) -> str:
        return MODEL_ID


# ------------------------------------------------------------------------------ 错误映射


class TestFaultMapping:
    @pytest.mark.parametrize(
        ("status", "code", "retryable"),
        [
            (400, ErrorCode.EXTERNAL_MODEL_PROVIDER, False),
            (401, ErrorCode.CONFIG_SECRET_MISSING, False),
            (403, ErrorCode.PERMISSION_DENIED, False),
            (404, ErrorCode.EXTERNAL_MODEL_PROVIDER, False),
            (408, ErrorCode.EXTERNAL_MODEL_PROVIDER, True),
            (413, ErrorCode.EXTERNAL_MODEL_CONTEXT_OVERFLOW, False),
            (500, ErrorCode.EXTERNAL_MODEL_PROVIDER, True),
            (529, ErrorCode.EXTERNAL_MODEL_PROVIDER, True),
        ],
    )
    def test_status_table(self, status: int, code: ErrorCode, retryable: bool) -> None:
        error = error_for_status(status, body={"type": "error", "error": {"type": "api_error"}})
        assert error.code is code
        assert error.retryable is retryable

    def test_rate_limit_is_retryable_but_quota_is_not(self) -> None:
        """两者都是 429：撞限速等一会儿就好，欠费重试一万次也不会好。"""
        limited = error_for_status(429, body={"error": {"type": "rate_limit_error"}})
        exhausted = error_for_status(429, body={"error": {"type": "credit_balance_too_low"}})
        assert limited.retryable is True
        assert exhausted.retryable is False

    def test_structured_context_overflow_is_recoverable_by_the_kernel(self) -> None:
        error = error_for_status(
            400,
            body={"error": {"type": "invalid_request_error", "code": "prompt_too_long"}},
        )
        assert error.code is ErrorCode.EXTERNAL_MODEL_CONTEXT_OVERFLOW

    def test_unknown_429_defaults_to_retryable(self) -> None:
        assert error_for_status(429, body={}).retryable is True

    def test_detail_never_carries_the_error_message(self) -> None:
        """那段自由文本会回显 prompt，也可能带着被 echo 回来的凭据。"""
        error = error_for_status(
            400,
            body={"error": {"type": "invalid_request_error", "message": f"bad key {SENTINEL_KEY}"}},
        )
        assert "message" not in error.detail
        assert SENTINEL_KEY not in repr(error)

    def test_request_id_is_kept(self) -> None:
        error = error_for_status(500, body={"error": {"type": "api_error"}, "request_id": "req_9"})
        assert error.detail["request_id"] == "req_9"

    def test_retry_after_headers(self) -> None:
        assert retry_after_ms({"retry-after-ms": "250"}) == 250
        assert retry_after_ms({"retry-after": "2"}) == 2000
        assert retry_after_ms({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}) is None
        assert retry_after_ms({}) is None

    def test_retry_after_is_recorded_in_detail(self) -> None:
        error = error_for_status(429, body={}, headers={"retry-after": "3"})
        assert error.detail["retry_after_ms"] == 3000

    def test_sse_error_events_use_the_same_table(self) -> None:
        assert error_for_event({"error": {"type": "overloaded_error"}}).retryable is True
        assert error_for_event({"error": {"type": "invalid_request_error"}}).retryable is False
        assert error_for_event({"error": {"type": "quota_exceeded"}}).retryable is False

    def test_transport_errors_are_classified(self) -> None:
        timed_out = error_for_transport(httpx.ConnectTimeout("slow"))
        refused = error_for_transport(httpx.ConnectError("nope"))
        assert timed_out.code is ErrorCode.TIMEOUT_MODEL_REQUEST
        assert refused.code is ErrorCode.EXTERNAL_MODEL_PROVIDER
        assert timed_out.retryable and refused.retryable

    async def test_httpx_exceptions_never_escape(self) -> None:
        """供应商客户端库的原生异常不得从 `complete()` 逸出（`protocols.py`）。"""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        provider = make_provider(handler)
        with pytest.raises(KaryviaError):
            await provider.complete(make_request(), ManualCancel())


# ------------------------------------------------------------------------------ 凭据


class TestCredentialNeverLeaks:
    def test_the_sentinel_is_actually_recognised_as_a_secret(self) -> None:
        """否则「没泄漏」只是因为它压根不长得像密钥，这条用例就成了同义反复。"""
        probe = KaryviaError(ErrorCode.CONFIG_INVALID, f"key={SENTINEL_KEY}")
        assert SENTINEL_KEY not in probe.user_message

    def test_the_settings_object_never_holds_the_plaintext(self) -> None:
        """明文只经 `SecretStr` 到达 `_headers()`，配置对象里根本没有它。"""
        provider = make_provider(lambda request: httpx.Response(200, json=message_body()))
        assert SENTINEL_KEY not in repr(provider.settings)
        assert SENTINEL_KEY not in repr(provider)
        assert SENTINEL_KEY not in json.dumps(dict(make_context().config), default=str)

    async def test_bearer_and_x_api_key_modes(self) -> None:
        for auth, header in (("x_api_key", "x-api-key"), ("bearer", "authorization")):
            captured: dict[str, str] = {}

            def handler(request: httpx.Request) -> httpx.Response:
                captured.update(dict(request.headers))
                return httpx.Response(200, json=message_body())

            provider = make_provider(handler, **{CONFIG_AUTH_KEY: auth})
            await provider.complete(make_request(), ManualCancel())
            assert SENTINEL_KEY in captured[header]

    async def test_a_401_does_not_leak_the_key_anywhere(self) -> None:
        provider = make_provider(
            lambda request: httpx.Response(
                401, json={"error": {"type": "authentication_error", "message": f"bad {SENTINEL_KEY}"}}
            )
        )
        with pytest.raises(KaryviaError) as excinfo:
            await provider.complete(make_request(), ManualCancel())
        error = excinfo.value
        rendered = [
            repr(error),
            str(error),
            error.user_message,
            json.dumps(error.detail, default=str),
            json.dumps(list(error.args), default=str),
        ]
        assert all(SENTINEL_KEY not in text for text in rendered)

    def test_version_and_beta_headers_are_sent(self) -> None:
        provider = make_provider(
            lambda request: httpx.Response(200, json=message_body()),
            **{CONFIG_ANTHROPIC_VERSION_KEY: "2024-01-01", CONFIG_BETA_HEADERS_KEY: ["a", "b"]},
        )
        headers = provider._headers()  # noqa: SLF001 - 断言的就是这层拼装
        assert headers["anthropic-version"] == "2024-01-01"
        assert headers["anthropic-beta"] == "a,b"

    def test_auth_none_never_touches_the_secret(self) -> None:
        """本地 relay 没有密钥，去要一个必然缺失的凭据只会让插件加载失败。"""
        ctx = FakePluginContext(plugin_id=MANIFEST.id, config={CONFIG_AUTH_KEY: "none"})
        settings = resolve_settings(ctx)
        assert read_credential(ctx, settings) is None


# ------------------------------------------------------------------------------ 取消


class TestCancellation:
    async def test_an_already_cancelled_turn_never_reaches_the_network(self) -> None:
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, json=message_body())

        provider = make_provider(handler)
        cancel = ManualCancel()
        cancel.request()
        with pytest.raises(KaryviaError) as excinfo:
            await provider.complete(make_request(), cancel)
        assert excinfo.value.category is ErrorCategory.CANCELLED
        assert calls == []

    async def test_streaming_checks_cancellation_before_each_chunk(self) -> None:
        provider = make_provider(lambda request: httpx.Response(200, text=text_stream()))
        cancel = ManualCancel()
        cancel.request()
        with pytest.raises(KaryviaError) as excinfo:
            await collect(provider, make_request(stream=True), cancel)
        assert excinfo.value.category is ErrorCategory.CANCELLED


# ------------------------------------------------------------------------------ 配置


class TestSettings:
    def test_defaults(self) -> None:
        settings = make_settings()
        info = settings.describe(MODEL_ID)
        assert settings.base_url == BASE_URL
        assert settings.auth == "x_api_key"
        assert settings.requires_credential is True
        assert info.provider == PROVIDER_NAME
        assert info.context_window_tokens == 200_000
        assert info.max_output_tokens == 8_192
        assert info.capabilities == frozenset(
            {ModelCapability.TOOL_CALLS, ModelCapability.STREAMING}
        )

    def test_base_url_is_used_verbatim(self) -> None:
        """中转端点靠改这一行就能用，因此不能替用户拼后缀。"""
        settings = make_settings(**{CONFIG_BASE_URL_KEY: "https://api.minimax.io/anthropic/"})
        assert settings.base_url == "https://api.minimax.io/anthropic"

    def test_base_url_scheme_is_checked(self) -> None:
        with pytest.raises(KaryviaError) as excinfo:
            make_settings(**{CONFIG_BASE_URL_KEY: "api.example.test"})
        assert excinfo.value.code is ErrorCode.CONFIG_INVALID

    def test_models_is_a_whitelist_when_non_empty(self) -> None:
        settings = make_settings(**{CONFIG_MODELS_KEY: {MODEL_ID: {}}})
        assert settings.model_ids == frozenset({MODEL_ID})
        with pytest.raises(KaryviaError) as excinfo:
            settings.describe("claude-typo")
        assert excinfo.value.category is ErrorCategory.CAPABILITY_MISSING

    def test_unknown_keys_in_a_model_entry_are_rejected(self) -> None:
        """「你多写了一个键」与「你的值写错了」补救动作不同，因此是两个码。"""
        with pytest.raises(KaryviaError) as excinfo:
            make_settings(**{CONFIG_MODELS_KEY: {MODEL_ID: {"windows": 1}}})
        assert excinfo.value.code is ErrorCode.CONFIG_UNKNOWN_FIELD

    def test_reasoning_is_derived_from_thinking_not_declared(self) -> None:
        assert ModelCapability.REASONING not in make_settings().describe(MODEL_ID).capabilities
        enabled = make_settings(**{CONFIG_THINKING_KEY: {"mode": "adaptive"}})
        assert ModelCapability.REASONING in enabled.describe(MODEL_ID).capabilities

    def test_prompt_caching_is_derived_from_the_switch(self) -> None:
        enabled = make_settings(**{CONFIG_CACHING_KEY: {"enabled": True}})
        assert ModelCapability.PROMPT_CACHING in enabled.describe(MODEL_ID).capabilities

    @pytest.mark.parametrize("capability", ["reasoning", "prompt_caching"])
    def test_declaring_a_derived_capability_is_rejected(self, capability: str) -> None:
        """声明了却没开开关，等于让组装器以为拿得到一份它拿不到的东西（`MOD-005`）。"""
        with pytest.raises(KaryviaError) as excinfo:
            make_settings(**{CONFIG_CAPABILITIES_KEY: [capability]})
        assert excinfo.value.code is ErrorCode.CONFIG_INVALID

    def test_thinking_disabled_does_not_declare_reasoning(self) -> None:
        """`disabled` 是显式关掉，它不会产出思考内容。"""
        settings = make_settings(**{CONFIG_THINKING_KEY: {"mode": "disabled"}})
        assert ModelCapability.REASONING not in settings.describe(MODEL_ID).capabilities

    def test_budget_must_leave_room_for_the_answer(self) -> None:
        """legacy 会偷偷把 max_tokens 抬到 budget+4096；我们直接拒。"""
        with pytest.raises(KaryviaError) as excinfo:
            make_settings(
                **{
                    CONFIG_DEFAULT_MAX_OUTPUT_KEY: 4096,
                    CONFIG_THINKING_KEY: {"mode": "budget", "budget_tokens": 4096},
                }
            )
        assert excinfo.value.code is ErrorCode.CONFIG_INVALID
        assert "budget_tokens" in str(excinfo.value.detail["pointer"])

    def test_unknown_thinking_mode_is_rejected(self) -> None:
        with pytest.raises(KaryviaError):
            make_settings(**{CONFIG_THINKING_KEY: {"mode": "turbo"}})

    def test_unknown_effort_is_rejected(self) -> None:
        with pytest.raises(KaryviaError):
            make_settings(**{CONFIG_EFFORT_KEY: "insane"})

    def test_unknown_capability_name_is_rejected_not_ignored(self) -> None:
        with pytest.raises(KaryviaError):
            make_settings(**{CONFIG_CAPABILITIES_KEY: ["telepathy"]})

    def test_booleans_must_be_booleans(self) -> None:
        with pytest.raises(KaryviaError):
            make_settings(**{CONFIG_SUPPORTS_TEMPERATURE_KEY: 1})

    def test_model_entries_inherit_the_top_level_defaults(self) -> None:
        settings = make_settings(
            **{
                CONFIG_THINKING_KEY: {"mode": "adaptive"},
                CONFIG_MODELS_KEY: {MODEL_ID: {"max_output_tokens": 4096}},
            }
        )
        entry = settings.entry_for(MODEL_ID)
        assert entry.max_output_tokens == 4096
        assert entry.thinking.mode == "adaptive"
        assert entry.context_window_tokens == 200_000

    def test_a_model_entry_can_override_thinking(self) -> None:
        settings = make_settings(
            **{
                CONFIG_THINKING_KEY: {"mode": "adaptive"},
                CONFIG_MODELS_KEY: {MODEL_ID: {"thinking": {"mode": "off"}}},
            }
        )
        assert settings.entry_for(MODEL_ID).thinking.mode == "off"
        assert ModelCapability.REASONING not in settings.describe(MODEL_ID).capabilities

    def test_local_endpoints_are_detected(self) -> None:
        assert is_local_endpoint("http://127.0.0.1:8080/v1") is True
        assert is_local_endpoint("http://localhost:1234") is True
        assert is_local_endpoint("http://192.168.1.9/v1") is True
        assert is_local_endpoint(BASE_URL) is False


# ------------------------------------------------------------------------------ manifest


class TestManifest:
    def test_entry_point_name_equals_the_manifest_id(self) -> None:
        """`D25` 的判定：对不上时 `plugins.enabled` 指不到任何东西。"""
        names = {item.name for item in entry_points(group="karyvia.plugins")}
        assert MANIFEST.id in names

    def test_one_model_capability_and_no_overrides(self) -> None:
        assert len(MANIFEST.capabilities) == 1
        decl = MANIFEST.capabilities[0]
        assert decl.kind is CapabilityKind.MODEL
        assert decl.name == CAPABILITY_NAME
        # 与内建 `openai` 并存而不是取代它。
        assert decl.overrides is None

    def test_priority_is_not_declared(self) -> None:
        """写了默认值 100 会被原样采纳，而内建基准是 0（`D16` 记的坑）。"""
        assert "priority" not in MANIFEST.capabilities[0].model_fields_set

    def test_config_schema_entry_properties_match_the_settings_table(self) -> None:
        """两处都「自洽」而对不上时，一个写对了的配置会在阶段 A 被 schema 拒掉。"""
        assert set(ENTRY_PROPERTIES) == set(MODEL_ENTRY_KEYS)

    def test_config_schema_enums_come_from_the_constants(self) -> None:
        thinking = ENTRY_PROPERTIES["thinking"]["properties"]["mode"]["enum"]
        assert set(thinking) == set(THINKING_MODES)
        assert set(ENTRY_PROPERTIES["prompt_caching"]["properties"]["ttl"]["enum"]) == set(CACHE_TTLS)

    def test_config_schema_forbids_unknown_keys(self) -> None:
        assert MANIFEST.config_schema["additionalProperties"] is False

    def test_setup_registers_exactly_one_model_provider(self) -> None:
        registered: dict[str, object] = {}

        class _Recorder:
            def __init__(self, ctx: FakePluginContext) -> None:
                self.ctx = ctx

            def register_model_provider(self, name: str, provider: object) -> None:
                registered[name] = provider

        context = make_context()
        setup(_Recorder(context))  # type: ignore[arg-type]
        assert list(registered) == [CAPABILITY_NAME]
        assert isinstance(registered[CAPABILITY_NAME], AnthropicModelProvider)
        assert context.cleanup_actions == [registered[CAPABILITY_NAME].aclose]

    def test_setup_fails_on_a_bad_config(self) -> None:
        """坏配置在 `setup()` 时就被指出来，不拖到第一次 turn。"""

        class _Recorder:
            def __init__(self, ctx: FakePluginContext) -> None:
                self.ctx = ctx

            def register_model_provider(self, name: str, provider: object) -> None:
                raise AssertionError("不该走到注册")

        with pytest.raises(KaryviaError):
            setup(_Recorder(make_context(**{CONFIG_BASE_URL_KEY: "ftp://nope"})))  # type: ignore[arg-type]

    def test_messages_path_is_the_only_endpoint(self) -> None:
        assert MESSAGES_PATH == "/messages"
