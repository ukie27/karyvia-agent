"""`kernel/config/` 的行为测试：四层合并、来源追踪、schema 校验、加载与实例锁。

覆盖 D10 的验收点：优先级顺序、`origin_of` 的可回答性、深合并的边界（dict 递归 / 标量
与列表整体替换）、校验一次报全、workspace 相对路径按实例目录解析、锁的获取·冲突·
陈旧回收。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from karyvia.contracts import JsonValue
from karyvia.contracts.errors import ErrorCode, KaryviaError
from karyvia.kernel.config import (
    CLI_ORIGIN,
    DEFAULT_ORIGIN,
    ENV_ORIGIN,
    FILE_ORIGIN,
    MEMORY_ON_FAILURE_CHOICES,
    SECTION_SPECS,
    SESSION_CONCURRENCY_CHOICES,
    ConfigLayer,
    InstanceLayout,
    InstanceLock,
    Liveness,
    collect_layers,
    env_layer,
    load_config,
    merge_layers,
    overrides_layer,
    parse_override,
    process_is_alive,
    process_started_at,
    read_config_file,
    validate_config,
)
from karyvia.kernel.config.fields import FieldKind, FieldSpec
from karyvia.kernel.config.sources import MAX_CONFIG_BYTES


def write_config(root: Path, payload: object) -> Path:
    """在实例目录里放一份 `config.json`，返回其路径。"""
    root.mkdir(parents=True, exist_ok=True)
    path = root / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestMerge:
    def test_later_layer_wins_scalar(self) -> None:
        result = merge_layers(
            [
                ConfigLayer(origin="low", data={"a": 1}),
                ConfigLayer(origin="high", data={"a": 2}),
            ]
        )
        assert result.data["a"] == 2
        assert result.origin_of("a") == "high"

    def test_dicts_merge_recursively(self) -> None:
        """两层各设 turn 的不同字段时，两个字段都要留下。"""
        result = merge_layers(
            [
                ConfigLayer(origin="low", data={"turn": {"max_iterations": 8, "tool_timeout_ms": 5}}),
                ConfigLayer(origin="high", data={"turn": {"max_iterations": 9}}),
            ]
        )
        assert result.data["turn"] == {"max_iterations": 9, "tool_timeout_ms": 5}
        assert result.origin_of("turn", "max_iterations") == "high"
        assert result.origin_of("turn", "tool_timeout_ms") == "low"

    def test_lists_replace_wholesale(self) -> None:
        """列表不做元素级合并——`plugins.disable` 的语义是「就这些」，不是「再加这些」。"""
        result = merge_layers(
            [
                ConfigLayer(origin="low", data={"plugins": {"disable": ["a", "b"]}}),
                ConfigLayer(origin="high", data={"plugins": {"disable": ["c"]}}),
            ]
        )
        assert result.data["plugins"] == {"disable": ["c"]}

    def test_scalar_over_dict_drops_stale_origins(self) -> None:
        """高层用标量盖掉整个子树后，子树里的来源记录不能残留。"""
        result = merge_layers(
            [
                ConfigLayer(origin="low", data={"model": {"provider": "openai", "name": "x"}}),
                ConfigLayer(origin="high", data={"model": None}),
            ]
        )
        assert result.data["model"] is None
        assert result.origin_of("model") == "high"
        assert result.origin_of("model", "provider") is None

    def test_untouched_field_has_no_origin(self) -> None:
        """没人设过的字段来源为 None，调用方据此说「取自默认值」。"""
        result = merge_layers([ConfigLayer(origin="low", data={"a": 1})])
        assert result.origin_of("b") is None

    def test_pointer_escaping(self) -> None:
        """RFC 6901：键里的 `/` 与 `~` 必须转义，否则 pointer 会指错地方。"""
        result = merge_layers([ConfigLayer(origin="low", data={"a/b~c": 1})])
        assert "/a~1b~0c" in result.origins


class TestFileSource:
    def test_missing_file_is_not_an_error(self, tmp_path: Path) -> None:
        """默认值本身是一份合法配置，首次启动没有 config.json 必须能跑。"""
        assert read_config_file(tmp_path / "config.json") == {}

    def test_malformed_json_reports_config_invalid(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(KaryviaError) as caught:
            read_config_file(path)
        assert caught.value.code is ErrorCode.CONFIG_INVALID

    def test_top_level_must_be_an_object(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text("[1, 2]", encoding="utf-8")
        with pytest.raises(KaryviaError) as caught:
            read_config_file(path)
        assert caught.value.code is ErrorCode.CONFIG_INVALID

    def test_oversized_file_is_rejected_before_parsing(self, tmp_path: Path) -> None:
        """误把配置指向一个大文件时，报错而不是把它整个读进内存再解析。"""
        path = tmp_path / "config.json"
        path.write_bytes(b'{"x": "' + b"a" * (MAX_CONFIG_BYTES + 16) + b'"}')
        with pytest.raises(KaryviaError) as caught:
            read_config_file(path)
        assert caught.value.code is ErrorCode.CONFIG_INVALID
        assert caught.value.detail["limit"] == MAX_CONFIG_BYTES

    def test_non_utf8_file_is_rejected(self, tmp_path: Path) -> None:
        """配置文件必须是 UTF-8；GBK 存盘的中文路径会走到这里。"""
        path = tmp_path / "config.json"
        path.write_bytes(b'{"workspace": {"root": "\xd6\xd0\xce\xc4"}}')
        with pytest.raises(KaryviaError) as caught:
            read_config_file(path)
        assert caught.value.code is ErrorCode.CONFIG_INVALID

    def test_unreadable_file_reports_persistence_failure(self, tmp_path: Path) -> None:
        """目录占了 config.json 的位置：这是 IO 故障，不是「配置内容不对」。"""
        path = tmp_path / "config.json"
        path.mkdir()
        with pytest.raises(KaryviaError) as caught:
            read_config_file(path)
        assert caught.value.code is ErrorCode.PERSISTENCE_READ_FAILED


class TestEnvSource:
    def test_double_underscore_nests_and_key_lowercases(self) -> None:
        layer = env_layer({"KARYVIA_CFG_TURN__MAX_ITERATIONS": "32"})
        assert layer.data == {"turn": {"max_iterations": 32}}
        assert layer.origin == ENV_ORIGIN

    def test_scalar_decoding_keeps_bare_strings(self) -> None:
        """`32` 要成 int（schema 要 int），`openai` 必须留成字符串。"""
        layer = env_layer(
            {
                "KARYVIA_CFG_MODEL__PROVIDER": "openai",
                "KARYVIA_CFG_LOGGING__FILE_ENABLED": "false",
            }
        )
        assert layer.data == {"model": {"provider": "openai"}, "logging": {"file_enabled": False}}

    def test_unprefixed_vars_ignored(self) -> None:
        assert env_layer({"PATH": "/usr/bin", "KARYVIA_INSTANCE": "work"}).data == {}

    def test_bare_prefix_is_ignored(self) -> None:
        """`KARYVIA_CFG_=1` 没有指向任何字段，不能变成一个空键。"""
        assert env_layer({"KARYVIA_CFG_": "1", "KARYVIA_CFG___": "2"}).data == {}

    def test_conflicting_paths_in_one_layer_are_rejected(self) -> None:
        """同一层里 `turn=1` 与 `turn.max_iterations=2` 自相矛盾，静默选一个等于替用户猜。"""
        with pytest.raises(KaryviaError) as caught:
            env_layer(
                {
                    "KARYVIA_CFG_TURN": "1",
                    "KARYVIA_CFG_TURN__MAX_ITERATIONS": "2",
                }
            )
        assert caught.value.code is ErrorCode.CONFIG_INVALID


class TestOverrides:
    def test_dotted_path_and_typed_value(self) -> None:
        assert parse_override("turn.max_iterations=32") == (["turn", "max_iterations"], 32)

    def test_missing_equals_is_rejected(self) -> None:
        with pytest.raises(KaryviaError) as caught:
            parse_override("turn.max_iterations")
        assert caught.value.code is ErrorCode.CONFIG_INVALID

    def test_empty_value_is_allowed(self) -> None:
        """`--set model.name=` 是「设成空串」，与漏写 `=` 不同。"""
        assert parse_override("model.name=") == (["model", "name"], "")

    def test_layer_origin_is_cli(self) -> None:
        assert overrides_layer(["turn.max_iterations=3"]).origin == CLI_ORIGIN

    def test_dotted_key_with_no_segments_is_rejected(self) -> None:
        with pytest.raises(KaryviaError) as caught:
            parse_override("...=1")
        assert caught.value.code is ErrorCode.CONFIG_INVALID


class TestPriority:
    def test_cli_beats_env_beats_file(self, tmp_path: Path) -> None:
        path = write_config(tmp_path, {"turn": {"max_iterations": 1}})
        layers = collect_layers(
            path,
            env={"KARYVIA_CFG_TURN__MAX_ITERATIONS": "2"},
            overrides=["turn.max_iterations=3"],
        )
        result = merge_layers(layers)
        turn = result.data["turn"]
        assert isinstance(turn, dict)
        assert turn["max_iterations"] == 3
        assert result.origin_of("turn", "max_iterations") == CLI_ORIGIN

    def test_env_beats_file(self, tmp_path: Path) -> None:
        path = write_config(tmp_path, {"turn": {"max_iterations": 1}})
        result = merge_layers(
            collect_layers(path, env={"KARYVIA_CFG_TURN__MAX_ITERATIONS": "2"})
        )
        turn = result.data["turn"]
        assert isinstance(turn, dict)
        assert turn["max_iterations"] == 2
        assert result.origin_of("turn", "max_iterations") == ENV_ORIGIN

    def test_file_origin_named_for_diagnostics(self, tmp_path: Path) -> None:
        path = write_config(tmp_path, {"turn": {"max_iterations": 1}})
        result = merge_layers(collect_layers(path))
        assert result.origin_of("turn", "max_iterations") == FILE_ORIGIN


def _other_value(spec: FieldSpec) -> JsonValue:
    """给一个字段挑一个**合法但不等于默认值**的取值。

    只认识 `FieldKind` 的六个形状，不认识任何字段名——加一种形状才改这里，加一个字段
    不用改（与 `kernel/config/fields.py` 的分界线相同）。
    """
    if spec.kind is FieldKind.BOOL:
        return not spec.default
    if spec.kind in (FieldKind.POSITIVE_INT, FieldKind.OPTIONAL_POSITIVE_INT):
        return int(spec.default) + 1 if isinstance(spec.default, int) else 7
    if spec.kind is FieldKind.STR_LIST:
        return ["probe"]
    if spec.choices:
        return next(item for item in spec.choices if item != spec.default)
    return "probe"


class TestSchema:
    def test_defaults_form_a_valid_config(self) -> None:
        config = validate_config({})
        assert config.turn.max_iterations > 0
        assert config.workspace.root is None
        assert config.context.turn_compactor == "basic"

    def test_unknown_key_rejected_with_pointer_and_suggestion(self) -> None:
        """`CFG-001`：未知字段用自己的码，不被笼统的「配置无效」吞掉。"""
        with pytest.raises(KaryviaError) as caught:
            validate_config({"turn": {"max_iterationz": 4}})
        error = caught.value
        assert error.code is ErrorCode.CONFIG_UNKNOWN_FIELD
        errors = error.detail["errors"]
        assert errors[0]["pointer"] == "/turn/max_iterationz"
        # 「只认 snake_case」这条规则要可教：给出近似的正确写法。
        assert "max_iterations" in errors[0]["reason"]

    def test_camel_case_key_gets_a_snake_case_suggestion(self) -> None:
        """新层只认 snake_case，不提供 camelCase 别名——但要告诉用户该写什么。"""
        with pytest.raises(KaryviaError) as caught:
            validate_config({"turn": {"maxIterations": 4}})
        assert "max_iterations" in caught.value.detail["errors"][0]["reason"]

    @pytest.mark.parametrize("field", ["compactor", "compactor_timeout_ms"])
    def test_removed_context_compactor_fields_are_rejected(self, field: str) -> None:
        with pytest.raises(KaryviaError) as caught:
            validate_config({"context": {field: "removed"}})
        assert caught.value.code is ErrorCode.CONFIG_UNKNOWN_FIELD
        assert caught.value.detail["errors"][0]["pointer"] == f"/context/{field}"

    def test_unknown_section_is_reported(self) -> None:
        with pytest.raises(KaryviaError) as caught:
            validate_config({"turnz": {}})
        errors = caught.value.detail["errors"]
        assert errors[0]["pointer"] == "/turnz"

    @pytest.mark.parametrize(
        ("payload", "pointer"),
        [
            ({"turn": {"max_iterations": "8"}}, "/turn/max_iterations"),
            ({"turn": {"max_iterations": True}}, "/turn/max_iterations"),
            ({"turn": {"max_iterations": 1.5}}, "/turn/max_iterations"),
            ({"turn": {"max_iterations": 0}}, "/turn/max_iterations"),
            ({"turn": {"max_iterations": -1}}, "/turn/max_iterations"),
            ({"turn": {"context_max_tokens": "auto"}}, "/turn/context_max_tokens"),
            ({"logging": {"file_enabled": "yes"}}, "/logging/file_enabled"),
            ({"logging": {"level": 5}}, "/logging/level"),
            ({"workspace": {"root": 7}}, "/workspace/root"),
            ({"plugins": {"disable": "acme"}}, "/plugins/disable"),
            ({"plugins": {"disable": [1, 2]}}, "/plugins/disable"),
            ({"turn": []}, "/turn"),
        ],
    )
    def test_wrong_types_report_their_pointer(
        self, payload: dict[str, object], pointer: str
    ) -> None:
        """`CFG-001` 的「类型错误」一行：每处都要定位得到。"""
        with pytest.raises(KaryviaError) as caught:
            validate_config(payload)  # type: ignore[arg-type]
        assert caught.value.detail["errors"][0]["pointer"] == pointer

    def test_bool_is_not_an_acceptable_integer(self) -> None:
        """`bool` 是 `int` 的子类，但 `max_iterations: true` 显然是写错了。"""
        with pytest.raises(KaryviaError):
            validate_config({"turn": {"max_iterations": True}})

    def test_null_section_falls_back_to_defaults(self) -> None:
        """`"turn": null` 是「用默认」，不是错误。"""
        assert validate_config({"turn": None}).turn.max_iterations > 0

    def test_optional_fields_accept_null(self) -> None:
        config = validate_config(
            {"turn": {"context_max_tokens": None}, "workspace": {"root": None}}
        )
        assert config.turn.context_max_tokens is None
        assert config.workspace.root is None

    def test_string_list_is_normalised_to_a_tuple(self) -> None:
        """配置对象是 frozen 的，列表必须变成元组才不会被调用方就地改掉。"""
        config = validate_config({"plugins": {"disable": ["a", "b"]}})
        assert config.plugins.disable == ("a", "b")

    def test_error_detail_never_contains_the_offending_value(self) -> None:
        """密钥可以出现在任何指针上，所以 detail 里只放指针与原因，绝不放值。"""
        sentinel = "sk-ThisMustNeverLeak0123456789"
        with pytest.raises(KaryviaError) as caught:
            validate_config({"model": {"provider": {"nested": sentinel}}})
        assert sentinel not in repr(caught.value.detail)
        assert sentinel not in str(caught.value)

    def test_all_errors_reported_at_once(self) -> None:
        """逐条抛会让用户改一个键、重启、再看到下一个错误。"""
        with pytest.raises(KaryviaError) as caught:
            validate_config({"turn": {"max_iterations": "x"}, "logging": {"level": 5}})
        assert len(caught.value.detail["errors"]) == 2

    def test_turn_section_converts_to_limits(self) -> None:
        """engine 只认 `TurnLimits`，转换必须由 schema 提供而不是各调用方自己拼。"""
        config = validate_config({"turn": {"max_iterations": 7}})
        assert config.turn.to_limits().max_iterations == 7

    def test_turn_defaults_match_the_limits_module(self) -> None:
        """schema 重写了六项默认值以避开 turn 包的导入开销，两张表因此必须逐一相等。

        这条测试就是那份重复的挡板。它若被删掉，两边会静默漂移，而受害者是「配置里没写
        的项到底用了什么值」。
        """
        from karyvia.kernel.turn import limits as limits_module

        defaults = validate_config({}).turn
        assert defaults.max_iterations == limits_module.DEFAULT_MAX_ITERATIONS
        assert defaults.max_tool_calls_per_turn == limits_module.DEFAULT_MAX_TOOL_CALLS_PER_TURN
        assert defaults.tool_timeout_ms == limits_module.DEFAULT_TOOL_TIMEOUT_MS
        assert defaults.tool_result_max_bytes == limits_module.DEFAULT_TOOL_RESULT_MAX_BYTES
        assert defaults.turn_timeout_ms == limits_module.DEFAULT_TURN_TIMEOUT_MS
        # `context_max_tokens` 两边都是 None（含义是「由模型能力推导」）。
        assert defaults.context_max_tokens is None
        assert limits_module.TurnLimits().context_max_tokens is None

    def test_default_limits_round_trip_through_turn_limits(self) -> None:
        """默认配置转出来的 `TurnLimits` 必须与直接构造的那个相等。"""
        from karyvia.kernel.turn import limits as limits_module

        assert validate_config({}).turn.to_limits() == limits_module.TurnLimits()

    def test_routing_defaults_are_the_documented_ones(self) -> None:
        routing = validate_config({}).routing
        assert routing.command_prefix == "/"
        assert routing.session_concurrency == "queue"
        assert routing.queue_max_size == 32
        assert routing.dedup_capacity == 4096
        assert routing.dedup_ttl_ms == 600_000

    def test_routing_defaults_match_the_routing_package(self) -> None:
        """配置镜像 routing 与 Runtime 的机制默认值，两侧必须逐一相等。

        与 `test_turn_defaults_match_the_limits_module` 同理：这条测试就是那份重复的挡板。
        """
        from karyvia.kernel import routing as routing_package
        from karyvia.kernel.routing import session_lock
        from karyvia.runtime.instance import DEFAULT_CHANNEL_CONCURRENCY

        routing = validate_config({}).routing
        assert routing.command_prefix == routing_package.DEFAULT_COMMAND_PREFIX
        assert routing.queue_max_size == routing_package.DEFAULT_QUEUE_MAX_SIZE
        assert routing.dedup_capacity == routing_package.DEFAULT_DEDUP_CAPACITY
        assert routing.dedup_ttl_ms == routing_package.DEFAULT_DEDUP_TTL_MS
        assert routing.channel_concurrency == DEFAULT_CHANNEL_CONCURRENCY
        # 策略字面量与枚举取值同名，否则配置里写的 `queue` 会转不成 `ConcurrencyPolicy`。
        assert set(SESSION_CONCURRENCY_CHOICES) == {
            policy.value for policy in session_lock.ConcurrencyPolicy
        }
        assert session_lock.ConcurrencyPolicy(routing.session_concurrency) is (
            session_lock.ConcurrencyPolicy.QUEUE
        )

    def test_orchestration_defaults_match_the_turn_package(self) -> None:
        """Hook 与 Context Provider 的三项超时同样在两处各写了一份（`D14`）。

        与上面两条同理：`schema.py` 不能 import `kernel.turn`（会把 engine 与 asyncio 拖上
        配置路径），代价就是这张对照表。
        """
        from karyvia.kernel.turn import context_builder, hooks, turn_compaction

        config = validate_config({})
        assert config.hooks.observer_timeout_ms == hooks.DEFAULT_OBSERVER_TIMEOUT_MS
        assert config.hooks.interceptor_timeout_ms == hooks.DEFAULT_INTERCEPTOR_TIMEOUT_MS
        assert (
            config.context.provider_timeout_ms
            == context_builder.DEFAULT_CONTEXT_PROVIDER_TIMEOUT_MS
        )
        assert config.context.turn_compactor == "basic"
        assert (
            config.context.turn_compactor_timeout_ms
            == turn_compaction.DEFAULT_TURN_COMPACTOR_TIMEOUT_MS
        )

    def test_memory_defaults_match_the_turn_package(self) -> None:
        """长期记忆的四项默认值 + 一张取值表，同样在两处各写了一份（`D44`）。

        与上面三条同理。**`on_failure` 的取值表也要对**：`MemorySection.critical` 把
        `"fail"` 翻成布尔，而那个字面量在 `kernel/turn/memory.py` 里也有一份。
        """
        from karyvia.kernel.turn import memory

        config = validate_config({})
        assert config.memory.recall_limit == memory.DEFAULT_MEMORY_RECALL_LIMIT
        assert config.memory.recall_timeout_ms == memory.DEFAULT_MEMORY_RECALL_TIMEOUT_MS
        assert config.memory.fragment_priority == memory.DEFAULT_MEMORY_FRAGMENT_PRIORITY
        assert config.memory.on_failure == memory.DEFAULT_MEMORY_ON_FAILURE
        assert MEMORY_ON_FAILURE_CHOICES == memory.MEMORY_ON_FAILURE_CHOICES

    def test_retry_defaults_match_the_turn_package(self) -> None:
        """模型请求重试的四项默认值同样在两处各写一份（`D48`）。

        `to_policy()` 与 `to_limits()` 一样用函数内 import，理由相同：`schema.py` 不得
        module-level import `kernel.turn`。
        """
        from karyvia.kernel.turn import retry

        config = validate_config({})
        assert config.retry.max_attempts == retry.DEFAULT_RETRY_MAX_ATTEMPTS
        assert config.retry.base_delay_ms == retry.DEFAULT_RETRY_BASE_DELAY_MS
        assert config.retry.max_delay_ms == retry.DEFAULT_RETRY_MAX_DELAY_MS
        assert config.retry.retry_empty_response == retry.DEFAULT_RETRY_EMPTY_RESPONSE

    def test_the_retry_section_converts_to_a_policy(self) -> None:
        """四个字段一个不漏地过去——漏一个的后果是那一项静默用回默认值。"""
        config = validate_config({"retry": {"max_attempts": 5, "retry_empty_response": False}})
        policy = config.retry.to_policy()
        assert policy.max_attempts == 5
        assert policy.retry_empty_response is False
        assert policy.base_delay_ms == config.retry.base_delay_ms
        assert policy.max_delay_ms == config.retry.max_delay_ms

    def test_every_declared_field_actually_reaches_the_config_object(self) -> None:
        """**每个字段都要真的被 `validate_config` 接进对应的小节 dataclass。**

        这条守卫是 `D48` 加的，它拦的是一个刚发生过的坑：`SECTION_SPECS` 里加了
        `retry` 小节、`sections.py` 里加了 `RetrySection`、
        `test_every_section_spec_has_a_dataclass_field` 照样绿——因为
        `validate_config()` 的返回值是**逐小节显式构造**的，漏掉一节的后果是用户写进
        `config.json` 的值被静默忽略、全程用默认值，而没有任何东西会响。

        做法是给每个字段挑一个与默认值不同的合法值，喂进去，再读回来。
        `plugins` 小节跳过：它的键空间对插件 id 开放，形状校验在 `plugin_blocks.py`。
        """
        for section, specs in SECTION_SPECS.items():
            if section == "plugins":
                continue
            for name, spec in specs.items():
                probe = _other_value(spec)
                loaded = validate_config({section: {name: probe}})
                actual = getattr(getattr(loaded, section), name)
                assert actual == probe, f"{section}.{name} 没有被 validate_config 接上"

    def test_the_reach_guard_would_notice_a_dropped_field(self) -> None:
        """自证：`_other_value` 真的给出了与默认值不同的值，否则上一条恒真。"""
        for specs in SECTION_SPECS.values():
            for name, spec in specs.items():
                assert _other_value(spec) != spec.default, name

    def test_memory_recall_is_off_unless_a_provider_is_named(self) -> None:
        """默认不启用 kernel 侧召回。

        自动挑一个会让「装上一个记忆插件」悄悄改变每一轮请求的内容——那是运维必须显式说出
        的决定，不是一个可以推断的默认。
        """
        assert validate_config({}).memory.provider is None
        assert validate_config({}).memory.critical is False

    def test_on_failure_fail_is_the_only_thing_that_makes_memory_critical(self) -> None:
        """`MEM-003`：默认降级。`critical` 是那个字面量的唯一消费点。"""
        assert validate_config({"memory": {"on_failure": "fail"}}).memory.critical is True
        assert validate_config({"memory": {"on_failure": "degrade"}}).memory.critical is False

    def test_unknown_memory_on_failure_is_rejected_with_the_allowed_values(self) -> None:
        with pytest.raises(KaryviaError) as caught:
            validate_config({"memory": {"on_failure": "retry"}})
        issue = caught.value.detail["errors"][0]
        assert issue["pointer"] == "/memory/on_failure"
        assert "degrade" in issue["reason"] and "fail" in issue["reason"]

    def test_unknown_session_concurrency_is_rejected_with_the_allowed_values(self) -> None:
        """取值受限的字段必须在校验时就带着指针报错，而不是等到构造调度器那一刻。"""
        with pytest.raises(KaryviaError) as caught:
            validate_config({"routing": {"session_concurrency": "parallel"}})
        issue = caught.value.detail["errors"][0]
        assert issue["pointer"] == "/routing/session_concurrency"
        assert issue["code"] == ErrorCode.CONFIG_INVALID.value
        assert "queue" in issue["reason"]

    def test_unknown_routing_field_is_rejected(self) -> None:
        with pytest.raises(KaryviaError) as caught:
            validate_config({"routing": {"dedup_capcity": 10}})
        issue = caught.value.detail["errors"][0]
        assert issue["code"] == ErrorCode.CONFIG_UNKNOWN_FIELD.value
        assert "dedup_capacity" in issue["reason"]

    def test_removed_channel_queue_limit_is_rejected(self) -> None:
        """删除重复策略后，旧字段必须显式失败，不能变成无效配置。"""
        with pytest.raises(KaryviaError) as caught:
            validate_config({"routing": {"channel_queue_max_size": 10}})
        issue = caught.value.detail["errors"][0]
        assert issue["pointer"] == "/routing/channel_queue_max_size"
        assert issue["code"] == ErrorCode.CONFIG_UNKNOWN_FIELD.value


class TestLoadConfig:
    def test_loads_defaults_for_a_fresh_instance(self, tmp_path: Path) -> None:
        loaded = load_config(instance_dir=tmp_path / "inst", env={})
        assert loaded.layout.root == (tmp_path / "inst").resolve()
        assert loaded.workspace_root == loaded.layout.workspace_dir
        assert loaded.config.turn.max_iterations > 0

    def test_default_values_are_traced_to_the_default_layer(self, tmp_path: Path) -> None:
        """`CFG-005` 对**所有**字段成立，包括没人写过的那些。

        默认值物化成一层，因此「这个值取自默认值」是查得到的答案，而不是「查不到来源」。
        """
        loaded = load_config(instance_dir=tmp_path / "inst", env={})
        assert loaded.origin_of("turn", "max_iterations") == DEFAULT_ORIGIN
        assert loaded.origin_of("logging", "file_enabled") == DEFAULT_ORIGIN

    def test_every_known_field_has_an_origin(self, tmp_path: Path) -> None:
        """遍历字段表：没有一个已知字段是「来源不明」的。"""
        loaded = load_config(instance_dir=tmp_path / "inst", env={})
        for section, fields in SECTION_SPECS.items():
            for name in fields:
                assert loaded.origin_of(section, name) is not None, f"{section}.{name}"

    def test_ensure_dirs_creates_the_layout(self, tmp_path: Path) -> None:
        loaded = load_config(instance_dir=tmp_path / "inst", env={})
        assert loaded.layout.workspace_dir.is_dir()
        assert loaded.layout.sessions_dir.is_dir()

    def test_ensure_dirs_false_leaves_disk_untouched(self, tmp_path: Path) -> None:
        """`karyvia doctor` 检查一个不存在的实例时不该顺手把它建出来。"""
        root = tmp_path / "inst"
        loaded = load_config(instance_dir=root, env={}, ensure_dirs=False)
        assert loaded.layout.root == root.resolve()
        assert not root.exists()

    def test_layers_apply_through_the_loader(self, tmp_path: Path) -> None:
        root = tmp_path / "inst"
        write_config(root, {"turn": {"max_iterations": 1, "tool_timeout_ms": 4000}})
        loaded = load_config(
            instance_dir=root,
            env={"KARYVIA_CFG_TURN__MAX_ITERATIONS": "2"},
            overrides=["turn.max_iterations=3"],
        )
        assert loaded.config.turn.max_iterations == 3
        assert loaded.config.turn.tool_timeout_ms == 4000
        assert loaded.origin_of("turn", "tool_timeout_ms") == FILE_ORIGIN

    def test_limits_property_feeds_the_engine(self, tmp_path: Path) -> None:
        loaded = load_config(
            instance_dir=tmp_path / "inst", env={}, overrides=["turn.max_iterations=5"]
        )
        assert loaded.limits.max_iterations == 5

    def test_relative_workspace_resolves_against_instance_dir(self, tmp_path: Path) -> None:
        """按 cwd 解析会让「从哪个目录跑 karyvia」改变 agent 能看见的文件。"""
        root = tmp_path / "inst"
        write_config(root, {"workspace": {"root": "shared-ws"}})
        loaded = load_config(instance_dir=root, env={})
        assert loaded.workspace_root == (root / "shared-ws").resolve()

    def test_absolute_workspace_is_used_as_given(self, tmp_path: Path) -> None:
        root = tmp_path / "inst"
        target = tmp_path / "elsewhere"
        target.mkdir()
        write_config(root, {"workspace": {"root": str(target)}})
        loaded = load_config(instance_dir=root, env={})
        assert loaded.workspace_root == target.resolve()

    def test_blank_workspace_root_is_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "inst"
        write_config(root, {"workspace": {"root": "  "}})
        with pytest.raises(KaryviaError) as caught:
            load_config(instance_dir=root, env={})
        assert caught.value.code is ErrorCode.CONFIG_INVALID

    def test_to_json_is_serializable(self, tmp_path: Path) -> None:
        """这个视图的唯一用途就是被打印或写盘，所以必须真能编码。"""
        loaded = load_config(
            instance_dir=tmp_path / "inst", env={}, overrides=["plugins.disable=[\"a\"]"]
        )
        text = json.dumps(loaded.to_json())
        assert "workspace_root" in text


def make_lock(path: Path, **kwargs: object) -> InstanceLock:
    """构造一把注入了探测函数的锁，测试不必真去制造僵死进程。"""
    return InstanceLock(path, **kwargs)  # type: ignore[arg-type]


def write_foreign_lock(path: Path, *, pid: int, created_at: float = 1.0) -> None:
    """伪造一把**别的进程**留下的锁。

    不能用 `InstanceLock(path).acquire()` 来造：那样写进去的是本进程的 PID，会先撞上
    「本进程已持有」的分支，陈旧判定根本走不到。
    """
    import socket

    path.write_text(
        json.dumps(
            {
                "pid": pid,
                "created_at": created_at,
                "process_started_at": created_at,
                "hostname": socket.gethostname(),
                "instance_dir": str(path.parent),
            }
        ),
        encoding="utf-8",
    )


class TestInstanceLock:
    def test_acquire_writes_holder_metadata(self, tmp_path: Path) -> None:
        lock = InstanceLock(tmp_path / "instance.lock").acquire()
        try:
            assert lock.held
            assert lock.info is not None
            assert lock.info.pid == os.getpid()
        finally:
            lock.release()

    def test_release_removes_the_file_and_is_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "instance.lock"
        lock = InstanceLock(path).acquire()
        lock.release()
        lock.release()
        assert not path.exists()
        assert not lock.held

    def test_context_manager_releases_on_exit(self, tmp_path: Path) -> None:
        path = tmp_path / "instance.lock"
        with InstanceLock(path) as lock:
            assert lock.held
        assert not path.exists()

    def test_live_holder_blocks_a_second_acquire(self, tmp_path: Path) -> None:
        path = tmp_path / "instance.lock"
        with InstanceLock(path):
            with pytest.raises(KaryviaError) as caught:
                make_lock(path, liveness=lambda pid: Liveness.ALIVE).acquire()
        error = caught.value
        assert error.code is ErrorCode.CONFIG_INSTANCE_LOCKED
        assert error.detail["holder_pid"] == os.getpid()

    def test_reacquire_by_same_object_is_an_error(self, tmp_path: Path) -> None:
        """藏起重复获取的 bug 比报出来更糟。"""
        lock = InstanceLock(tmp_path / "instance.lock").acquire()
        try:
            with pytest.raises(KaryviaError) as caught:
                lock.acquire()
            assert caught.value.code is ErrorCode.CONFIG_INSTANCE_LOCKED
        finally:
            lock.release()

    def test_dead_holder_is_reclaimed(self, tmp_path: Path) -> None:
        path = tmp_path / "instance.lock"
        write_foreign_lock(path, pid=os.getpid() + 1)
        lock = make_lock(path, liveness=lambda pid: Liveness.DEAD).acquire()
        try:
            assert lock.reclaimed is not None
            assert lock.reclaimed.holder is not None
            assert lock.reclaimed.holder.pid == os.getpid() + 1
        finally:
            lock.release()

    def test_unknown_liveness_never_steals_the_lock(self, tmp_path: Path) -> None:
        """模糊的答案不得授权抢走一把可能还活着的锁。"""
        path = tmp_path / "instance.lock"
        write_foreign_lock(path, pid=os.getpid() + 1)
        with pytest.raises(KaryviaError) as caught:
            make_lock(path, liveness=lambda pid: Liveness.UNKNOWN).acquire()
        assert caught.value.code is ErrorCode.CONFIG_INSTANCE_LOCKED

    def test_pid_reuse_is_detected_by_start_time(self, tmp_path: Path) -> None:
        """同一个 PID 上跑着更年轻的进程，说明锁是陈旧的。"""
        path = tmp_path / "instance.lock"
        write_foreign_lock(path, pid=os.getpid() + 1, created_at=1_000.0)
        lock = make_lock(
            path,
            liveness=lambda pid: Liveness.ALIVE,
            started_at=lambda pid: 5_000.0,
        ).acquire()
        try:
            assert lock.reclaimed is not None
            assert lock.reclaimed.holder is not None
        finally:
            lock.release()

    def test_same_start_time_keeps_the_lock_held(self, tmp_path: Path) -> None:
        path = tmp_path / "instance.lock"
        write_foreign_lock(path, pid=os.getpid() + 1, created_at=5_000.0)
        with pytest.raises(KaryviaError) as caught:
            make_lock(
                path,
                liveness=lambda pid: Liveness.ALIVE,
                started_at=lambda pid: 4_999.0,
            ).acquire()
        assert caught.value.code is ErrorCode.CONFIG_INSTANCE_LOCKED

    def test_unreadable_lock_is_reclaimed(self, tmp_path: Path) -> None:
        """一次「create 与 write 之间崩溃」不该永久砖掉实例。"""
        path = tmp_path / "instance.lock"
        path.write_text("{corrupt", encoding="utf-8")
        lock = InstanceLock(path).acquire()
        try:
            assert lock.reclaimed is not None
            assert lock.reclaimed.holder is None
        finally:
            lock.release()

    def test_foreign_hostname_is_never_reclaimed(self, tmp_path: Path) -> None:
        """PID 在别的主机上毫无意义，共享目录里不能凭它抢锁。"""
        path = tmp_path / "instance.lock"
        path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "created_at": 1.0,
                    "process_started_at": 1.0,
                    "hostname": "some-other-host",
                    "instance_dir": str(tmp_path),
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(KaryviaError) as caught:
            make_lock(path, liveness=lambda pid: Liveness.DEAD).acquire()
        assert caught.value.code is ErrorCode.CONFIG_INSTANCE_LOCKED

    def test_release_does_not_delete_a_reclaimed_lock(self, tmp_path: Path) -> None:
        """锁被别人回收重建后，删掉它会让两个进程同时认为自己持有实例。"""
        path = tmp_path / "instance.lock"
        mine = InstanceLock(path).acquire()
        path.write_text(
            json.dumps(
                {
                    "pid": os.getpid() + 1,
                    "created_at": 1.0,
                    "process_started_at": 1.0,
                    "hostname": "localhost",
                    "instance_dir": str(tmp_path),
                }
            ),
            encoding="utf-8",
        )
        mine.release()
        assert path.exists()

    def test_lock_lives_in_the_instance_dir(self, tmp_path: Path) -> None:
        layout = InstanceLayout.resolve(instance_dir=tmp_path / "inst", env={})
        assert InstanceLock(layout.lock_path).path.parent == layout.root


class TestLockInfoDecoding:
    """`from_json` 决定一把锁「能不能给出可探测的 PID」，因此它的边界要钉死。"""

    @pytest.mark.parametrize(
        "payload",
        [
            "[]",
            '"text"',
            "null",
            "{}",
            '{"pid": 0}',
            '{"pid": -1}',
            '{"pid": "123"}',
            '{"pid": true}',
            '{"pid": 1.5}',
        ],
    )
    def test_unusable_payloads_are_reclaimed_as_unreadable(
        self, payload: str, tmp_path: Path
    ) -> None:
        """给不出正整数 PID 的锁一律当陈旧处理，否则实例会被永久砖住。"""
        path = tmp_path / "instance.lock"
        path.write_text(payload, encoding="utf-8")
        lock = InstanceLock(path).acquire()
        try:
            assert lock.reclaimed is not None
            assert lock.reclaimed.reason == "unreadable"
        finally:
            lock.release()

    def test_missing_optional_fields_default_without_crashing(self, tmp_path: Path) -> None:
        """只有 pid 的锁仍然可用：其余字段缺失时退化，但不能抛异常。"""
        path = tmp_path / "instance.lock"
        path.write_text(json.dumps({"pid": os.getpid() + 1}), encoding="utf-8")
        with pytest.raises(KaryviaError) as caught:
            make_lock(path, liveness=lambda pid: Liveness.UNKNOWN).acquire()
        assert caught.value.detail["holder_pid"] == os.getpid() + 1

    def test_non_numeric_timestamps_degrade_to_zero(self, tmp_path: Path) -> None:
        """时间戳是坏的就当「未知」，不能因此把整把锁判成不可读。"""
        path = tmp_path / "instance.lock"
        path.write_text(
            json.dumps({"pid": os.getpid() + 1, "created_at": "yesterday"}), encoding="utf-8"
        )
        with pytest.raises(KaryviaError) as caught:
            make_lock(path, liveness=lambda pid: Liveness.ALIVE).acquire()
        assert caught.value.detail["holder_created_at"] == 0.0

    def test_truncated_raw_excerpt_is_bounded(self, tmp_path: Path) -> None:
        """一个被写坏成几 MB 的锁文件不该把诊断输出淹掉。"""
        path = tmp_path / "instance.lock"
        path.write_text("x" * 10_000, encoding="utf-8")
        lock = InstanceLock(path).acquire()
        try:
            assert lock.reclaimed is not None
            assert len(lock.reclaimed.raw) < 10_000
        finally:
            lock.release()


class TestProcessProbe:
    """真实 PID 探测。`lock` 的测试全用注入的假探测，这里才验证真实实现。"""

    def test_probing_a_live_process_does_not_kill_it(self) -> None:
        """本项目最重要的跨平台断言。

        Windows 上 `os.kill(pid, 0)` **不是**探测：CPython 把非 CTRL 信号映射到
        `TerminateProcess`，那样「探测」一个持锁进程会直接把它杀掉。
        """
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            assert process_is_alive(child.pid) is Liveness.ALIVE
            # 探测之后仍在跑——poll() 为 None 即进程未退出。
            assert child.poll() is None
        finally:
            child.terminate()
            child.wait(timeout=10)

    def test_exited_process_reads_as_dead(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"])
        child.wait(timeout=10)
        assert process_is_alive(child.pid) is Liveness.DEAD

    def test_current_process_is_alive(self) -> None:
        assert process_is_alive(os.getpid()) is Liveness.ALIVE

    @pytest.mark.parametrize("pid", [0, -1, -12345])
    def test_non_positive_pids_never_reach_a_syscall(
        self, pid: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POSIX 上 `os.kill(0, 0)` 打的是整个进程组，`-1` 是几乎所有进程。

        一个被写坏的锁文件不该有能力向本机广播信号，所以非正数必须在任何 syscall
        **之前**被挡下。
        """
        calls: list[int] = []
        monkeypatch.setattr(os, "kill", lambda target, signal: calls.append(target))
        assert process_is_alive(pid) is Liveness.DEAD
        assert calls == []

    def test_started_at_is_a_plausible_timestamp(self) -> None:
        """PID 复用护栏的数据源。取不到返回 None（macOS 等平台），取到就必须像个时间戳。"""
        observed = process_started_at(os.getpid())
        if observed is None:
            pytest.skip("本平台不提供进程创建时间")
        assert 0 < observed <= time.time() + 1

    def test_started_at_rejects_non_positive_pids(self) -> None:
        assert process_started_at(0) is None


def test_loading_config_does_not_import_the_turn_engine(tmp_path: Path) -> None:
    """`karyvia config show` 不该把 turn 引擎与 asyncio 调度拖上路径（`NFR-405` 冷启动预算）。

    `schema.py` 只从 `kernel.turn.limits` 取六个默认值常量，engine / scheduling / folding
    都不该被牵连；`routing` 的五个默认值同理，那个包会把调度器与 asyncio 一起带进来。
    子进程里断言，因为本进程早就把它们导入了。
    """
    script = (
        "import sys;"
        "from karyvia.kernel.config import load_config;"
        f"load_config(instance_dir=r'{tmp_path / 'inst'}', env={{}});"
        "leaked=[m for m in ('karyvia.kernel.turn.engine',"
        "'karyvia.kernel.turn.scheduling','karyvia.kernel.turn.folding',"
        "'karyvia.kernel.routing')"
        " if m in sys.modules];"
        "print('LEAKED' if leaked else 'CLEAN', leaked)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert result.stdout.startswith("CLEAN"), result.stdout


def test_loading_config_does_not_import_pydantic(tmp_path: Path) -> None:
    """决策「schema 手写、不用 pydantic」的可执行形态（`NFR-405`）。

    `import pydantic` 实测约 90 ms，会把 `kernel.config` 的导入推到 300 ms 以上，而那是
    整个冷启动的预算。配置加载在启动第 2 步、永远在必经路径上。
    """
    script = (
        "import sys;"
        "from karyvia.kernel.config import load_config;"
        f"load_config(instance_dir=r'{tmp_path / 'inst'}', env={{}});"
        "print('LEAKED' if 'pydantic' in sys.modules else 'CLEAN')"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert result.stdout.startswith("CLEAN"), result.stdout
