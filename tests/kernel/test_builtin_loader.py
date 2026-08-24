"""静态清单 bootstrap 的测试（`D16`；`EDG-103`、`EDG-101`、`PLG-004`、`PLG-007`）。

三条主线：

- **零请求是一等路径**。`D16` 的 `BUILTIN_MANIFESTS` 就是空元组，而「未启用任何插件时实例
  照常启动」是 `PLG-007`/`EDG-101` 写死的需求，因此它有自己的用例。
- **事务性**。`setup` 中途抛异常，registry 不得留下半注册状态（`EDG-103`）——这是
  `RegistrationBatch` 存在的全部理由，必须在真实调用链上被断言一次。
- **失败隔离**（`PLG-004`、`EDG-106`）：提供方失败记进结果，只跳过其依赖方，
  无关提供方继续加载。

`resolve_setup` 全程注入假解析器：本模块的可测性不该依赖任何真实内建存在，
而 `D16` 恰好一个都没有。只有 `import_setup` 自己的用例碰导入系统。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nucleamind.contracts import (
    Builtin,
    CapabilityKind,
    ErrorCode,
    NucleaError,
    Plugin,
    PluginId,
)
from nucleamind.kernel.plugins import (
    CapabilityDeclaration,
    CapabilityHost,
    LoadRequest,
    RegistrationHost,
    SetupFn,
    import_setup,
    load_into,
)
from nucleamind.kernel.registry import CapabilityRegistry, RegistrationBatch
from nucleamind.sdk.testing import ECHO_SPEC, EchoTool, FakeModelProvider, FakePluginContext

# ------------------------------------------------------------------------------------ 夹具


def host_for(batch: RegistrationBatch, request: LoadRequest) -> RegistrationHost:
    return CapabilityHost(batch, FakePluginContext(), declarations=request.declarations)


def request_for(
    setup: str = "fake:setup",
    *,
    plugin: str | None = None,
    dependencies: tuple[str, ...] = (),
) -> LoadRequest:
    return LoadRequest(
        plugin_id=plugin or "builtin-probe",
        provider=Plugin(PluginId(plugin)) if plugin else Builtin(),
        setup=setup,
        declarations=(
            CapabilityDeclaration(kind=CapabilityKind.TOOL, name=ECHO_SPEC.name),
            CapabilityDeclaration(kind=CapabilityKind.MODEL, name="model"),
        ),
        dependencies=dependencies,
    )


def good_setup(api: object) -> None:
    """一个正常的 `setup`：把声明的两项都注册上。"""
    assert isinstance(api, CapabilityHost)
    api.register_tool(ECHO_SPEC, EchoTool())
    api.register_model_provider("model", FakeModelProvider())


def resolver(setup: SetupFn) -> object:
    def resolve(target: str) -> SetupFn:
        del target
        return setup

    return resolve


# ----------------------------------------------------------------------------------- 零请求


async def test_zero_requests_loads_nothing_and_leaves_the_registry_writable() -> None:
    """`PLG-007`/`EDG-101`：没有任何内建时装配链照常跑完，registry 仍可写。"""
    registry = CapabilityRegistry()
    outcomes = await load_into(registry, (), host_for=host_for)
    assert outcomes == ()
    assert registry.registrations == ()
    assert not registry.frozen


# ------------------------------------------------------------------------------------ 正常路径


async def test_a_successful_load_commits_and_reports_what_it_registered() -> None:
    registry = CapabilityRegistry()
    outcomes = await load_into(
        registry, [request_for()], host_for=host_for, resolve_setup=resolver(good_setup)
    )
    assert len(outcomes) == 1
    assert outcomes[0].ok
    assert {ref.name for ref in outcomes[0].registered} == {ECHO_SPEC.name, "model"}
    assert len(registry.registrations) == 2


async def test_an_async_setup_is_awaited() -> None:
    """同步与异步 `setup` 都接受——纯注册函数不该被迫写 `async`，反之亦然。"""

    async def async_setup(api: object) -> None:
        good_setup(api)

    registry = CapabilityRegistry()
    outcomes = await load_into(
        registry, [request_for()], host_for=host_for, resolve_setup=resolver(async_setup)
    )
    assert outcomes[0].ok
    assert len(registry.registrations) == 2


async def test_requests_load_in_the_given_order() -> None:
    """静态清单里的顺序**就是**加载顺序（`D16` 没有拓扑排序，那是 `D27`）。"""
    registry = CapabilityRegistry()
    requests = [request_for(plugin="a"), request_for(plugin="b")]
    outcomes = await load_into(
        registry, requests, host_for=host_for, resolve_setup=resolver(good_setup)
    )
    assert [str(outcome.provider) for outcome in outcomes] == ["plugin:a", "plugin:b"]


# ------------------------------------------------------------------------------------ 事务性


async def test_a_failing_setup_discards_the_whole_batch() -> None:
    """`EDG-103`：注册到一半抛异常，registry 不留半注册状态。"""

    def half_then_boom(api: object) -> None:
        assert isinstance(api, CapabilityHost)
        api.register_tool(ECHO_SPEC, EchoTool())
        raise RuntimeError("setup 炸了")

    registry = CapabilityRegistry()
    outcomes = await load_into(
        registry, [request_for()], host_for=host_for, resolve_setup=resolver(half_then_boom)
    )
    assert registry.registrations == ()
    assert outcomes[0].error is not None
    assert outcomes[0].registered == ()


async def test_a_third_party_exception_message_is_not_carried_into_the_error() -> None:
    """第三方异常文本可能带着凭据，因此只留类型名（与 `D13` 的命令 handler 同一条理由）。"""

    def leaky(api: object) -> None:
        del api
        raise RuntimeError("token=sk-should-never-appear")

    registry = CapabilityRegistry()
    outcomes = await load_into(
        registry, [request_for()], host_for=host_for, resolve_setup=resolver(leaky)
    )
    error = outcomes[0].error
    assert error is not None
    assert error.detail["exception"] == "RuntimeError"
    assert "sk-should-never-appear" not in repr(error)


async def test_an_unfulfilled_declaration_fails_the_load() -> None:
    """声明了却没注册——由 `host.finish()` 判定，loader 负责把它折成失败结果。"""

    def partial(api: object) -> None:
        assert isinstance(api, CapabilityHost)
        api.register_tool(ECHO_SPEC, EchoTool())

    registry = CapabilityRegistry()
    outcomes = await load_into(
        registry, [request_for()], host_for=host_for, resolve_setup=resolver(partial)
    )
    assert registry.registrations == ()
    error = outcomes[0].error
    assert error is not None
    assert error.code is ErrorCode.PLUGIN_LOAD_FAILED


# -------------------------------------------------------------------------- 失败隔离


async def test_a_provider_failure_is_reported_without_propagating() -> None:
    """`PLG-004`：提供方不能决定中断宿主。"""

    def boom(api: object) -> None:
        del api
        raise RuntimeError("boom")

    registry = CapabilityRegistry()
    outcomes = await load_into(
        registry,
        [request_for()],
        host_for=host_for,
        resolve_setup=resolver(boom),
    )
    assert outcomes[0].error is not None
    assert outcomes[0].error.code is ErrorCode.PLUGIN_LOAD_FAILED
    assert registry.registrations == ()


async def test_a_failure_does_not_stop_the_remaining_providers() -> None:
    """`EDG-106`：插件坏掉，实例继续装配，其余能力照常注册。"""
    calls: list[str] = []

    def flaky(api: object) -> None:
        if not calls:
            calls.append("first")
            raise RuntimeError("boom")
        good_setup(api)

    registry = CapabilityRegistry()
    outcomes = await load_into(
        registry,
        [request_for(plugin="bad"), request_for(plugin="good")],
        host_for=host_for,
        resolve_setup=resolver(flaky),
    )
    assert outcomes[0].error is not None
    assert outcomes[1].ok
    assert len(registry.registrations) == 2


async def test_a_failed_dependency_skips_only_its_transitive_dependents() -> None:
    """阶段 A 排好顺序后，阶段 B 的失败仍必须沿依赖图级联，但不能波及无关插件。"""
    calls: list[str] = []

    def resolve(target: str) -> SetupFn:
        def setup(api: object) -> None:
            calls.append(target)
            if target == "fail:setup":
                raise RuntimeError("boom")
            good_setup(api)

        return setup

    registry = CapabilityRegistry()
    outcomes = await load_into(
        registry,
        [
            request_for("fail:setup", plugin="a"),
            request_for("b:setup", plugin="b", dependencies=("a",)),
            request_for("c:setup", plugin="c", dependencies=("b",)),
            request_for("free:setup", plugin="free"),
        ],
        host_for=host_for,
        resolve_setup=resolve,
    )

    assert calls == ["fail:setup", "free:setup"]
    assert [outcome.ok for outcome in outcomes] == [False, False, False, True]
    assert outcomes[1].error is not None
    assert outcomes[1].error.detail["failed_dependencies"] == ["a"]
    assert outcomes[2].error is not None
    assert outcomes[2].error.detail["failed_dependencies"] == ["b"]
    assert len(registry.registrations) == 2


async def test_a_dependent_is_reported_without_running_setup() -> None:
    calls: list[str] = []

    def resolve(target: str) -> SetupFn:
        def setup(api: object) -> None:
            calls.append(target)
            if target == "fail:setup":
                raise RuntimeError("boom")
            good_setup(api)

        return setup

    outcomes = await load_into(
        CapabilityRegistry(),
        [
            request_for("fail:setup", plugin="a"),
            request_for(
                "must-not-run:setup",
                plugin="b",
                dependencies=("a",),
            ),
        ],
        host_for=host_for,
        resolve_setup=resolve,
    )
    assert calls == ["fail:setup"]
    assert outcomes[1].error is not None
    assert outcomes[1].error.code is ErrorCode.PLUGIN_LOAD_FAILED
    assert outcomes[1].error.detail["plugin_id"] == "b"


# -------------------------------------------------------------------------------- import_setup


def test_import_setup_resolves_a_real_entry_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "d16_probe.py"
    module.write_text("def setup(api):\n    return None\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert callable(import_setup("d16_probe:setup"))


@pytest.mark.parametrize(
    "target",
    ["no-colon", ":setup", "d16_probe:", "nucleamind.does_not_exist:setup"],
    ids=["无冒号", "缺模块名", "缺属性名", "模块不存在"],
)
def test_import_setup_reports_a_bad_entry_point(target: str) -> None:
    """不让 `ImportError` 直接冒泡：「插件坏了」与「Kernel 坏了」必须可区分。"""
    with pytest.raises(NucleaError) as excinfo:
        import_setup(target)
    assert excinfo.value.code is ErrorCode.PLUGIN_LOAD_FAILED
    assert excinfo.value.detail["setup"] == target


def test_import_setup_rejects_a_non_callable_attribute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "d16_notfn.py"
    module.write_text("setup = 42\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(NucleaError) as excinfo:
        import_setup("d16_notfn:setup")
    assert excinfo.value.code is ErrorCode.PLUGIN_LOAD_FAILED
