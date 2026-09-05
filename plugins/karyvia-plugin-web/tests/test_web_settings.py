"""`settings.py` 的用例：默认值、跨字段依赖、上界、以及被拒的写法。

manifest 的 `config_schema` 在阶段 A 校验形状，这里校验它表达不了的那些。两处都通过
才叫「配置错了会响」。
"""

from __future__ import annotations

import pytest
from karyvia_plugin_web.settings import (
    CREDENTIALLESS_PROVIDERS,
    DEFAULT_MAX_RESULTS,
    DEFAULT_PROVIDER,
    PROVIDERS,
    resolve_settings,
)

from karyvia.contracts import ErrorCode, JsonValue, KaryviaError


def _fails(config: dict[str, JsonValue]) -> KaryviaError:
    with pytest.raises(KaryviaError) as caught:
        resolve_settings(config)
    assert caught.value.code is ErrorCode.CONFIG_INVALID
    return caught.value


class TestDefaults:
    def test_an_empty_block_is_valid(self) -> None:
        """装上插件不配任何东西就能用——`BAS-001` 在这一层的形态。"""
        settings = resolve_settings({})
        assert settings.search.provider == DEFAULT_PROVIDER
        assert settings.search.max_results == DEFAULT_MAX_RESULTS
        assert settings.fetch.max_bytes > 0

    def test_the_default_provider_needs_no_credential(self) -> None:
        """外部插件用不上 `keep` 声明过滤，两条能力恒被注册；默认后端因此不能要凭据，
        否则就成了「声明了却不可用」。"""
        assert DEFAULT_PROVIDER in CREDENTIALLESS_PROVIDERS
        assert resolve_settings({}).search.needs_credential is False

    @pytest.mark.parametrize("provider", sorted(PROVIDERS))
    def test_every_provider_is_either_credentialless_or_not(self, provider: str) -> None:
        config: dict[str, JsonValue] = {"search": {"provider": provider}}
        if provider in {"searxng", "custom"}:
            config["search"] = {"provider": provider, "base_url": "https://s.example"}
        settings = resolve_settings(config)
        assert settings.search.needs_credential is (provider not in CREDENTIALLESS_PROVIDERS)


class TestValidation:
    def test_an_unknown_provider_lists_the_choices(self) -> None:
        """取值受限的字段必须在报错时说「你可以写哪几个」（`D13` 给 `FieldSpec` 加
        `choices` 的同一条理由）。"""
        error = _fails({"search": {"provider": "bing"}})
        assert error.detail["choices"] == list(PROVIDERS)
        assert error.detail["key"] == "plugins.web.config.search.provider"

    @pytest.mark.parametrize("provider", ["searxng", "custom"])
    def test_self_hosted_backends_require_a_base_url(self, provider: str) -> None:
        error = _fails({"search": {"provider": provider}})
        assert error.detail["key"] == "plugins.web.config.search.base_url"

    def test_max_result_chars_has_a_ceiling(self) -> None:
        """放行只会让每次调用都在构造 `ToolResult` 时才炸，那时错误指向 kernel 而不是
        这行配置（`D20` 的先例）。"""
        error = _fails({"fetch": {"max_result_chars": 10_000_000}})
        assert error.detail["key"] == "plugins.web.config.fetch.max_result_chars"

    def test_true_is_not_a_positive_integer(self) -> None:
        """`True` 是 `int` 的实例，放行它会让 `timeout_ms: true` 变成 1 毫秒。"""
        _fails({"fetch": {"timeout_ms": True}})

    @pytest.mark.parametrize(
        "config",
        [
            {"fetch": "nope"},
            {"search": {"max_results": 0}},
            {"search": {"provider": 5}},
            {"user_agent": 1},
            {"search": {"provider": "custom", "base_url": "u", "custom": {"method": "PUT"}}},
            {"search": {"provider": "custom", "base_url": "u", "custom": {"headers": "x"}}},
            {"search": {"provider": "custom", "base_url": "u", "custom": {"headers": {"a": 1}}}},
        ],
    )
    def test_rejected_shapes(self, config: dict[str, JsonValue]) -> None:
        _fails(config)


class TestCustomBackend:
    def test_defaults_are_filled_in(self) -> None:
        settings = resolve_settings(
            {"search": {"provider": "custom", "base_url": "https://s.example/api"}}
        )
        backend = settings.search.custom
        assert (backend.method, backend.results_path) == ("POST", "results")

    def test_method_is_upper_cased(self) -> None:
        settings = resolve_settings(
            {
                "search": {
                    "provider": "custom",
                    "base_url": "https://s.example/api",
                    "custom": {"method": "get"},
                }
            }
        )
        assert settings.search.custom.method == "GET"
