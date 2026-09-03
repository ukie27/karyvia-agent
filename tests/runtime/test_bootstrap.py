"""`D23` 装配根：`bootstrap()` 的十步、必需能力校验与 `AgentInstance` 的生命周期。

职责：验装配链本身——配置块怎么交到内建手上、`keep` 与 `setup()` 是否同源、必需能力
缺失时是不是显式失败、`EDG-108` 的两条守卫、Channel 泵是否真的把 CLI 输入送进 turn、
停止时锁是否释放。
不负责：验单个内建的行为（`tests/builtins/`）、验 kernel 各机制（`tests/kernel/`）。

**Fake 只换模型这一项**（`_support.TEST_MANIFESTS`）：其余六份 manifest 与生产完全一致，
因此这套用例真的走了一遍 `session_jsonl` 写盘、`tools_fs` 建守卫、`commands_core` 读
`ctx.instance` 的那条路。往里再多换一个 Fake，这套用例就退化成 `tests/kernel/` 的重复。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import nucleamind.runtime.bootstrap as bootstrap_module
from nucleamind.builtins.registry import BUILTIN_MANIFESTS
from nucleamind.contracts import (
    CancelSignal,
    CapabilityKind,
    ErrorCode,
    EventName,
    NucleaError,
    SessionKey,
    SideEffect,
    StreamState,
    ToolInvocation,
    ToolResult,
    ToolSpec,
)
from nucleamind.kernel.config import InstanceLock
from nucleamind.kernel.turn import CancelToken
from nucleamind.runtime.bootstrap import bootstrap, builtin_config_blocks
from nucleamind.runtime.instance import AgentInstance
from nucleamind.runtime.plugin_context import RuntimePluginContext
from nucleamind.sdk import CapabilityDecl, NucleaAPI, PluginManifest

from ._support import (
    FAKE_MODEL_ID,
    MEMORIES,
    MEMORY_NAME,
    SCRIPT,
    TEST_MANIFESTS,
    FakeMemoryProvider,
    inbound,
    manifests_with_memory,
    manifests_without,
    memory_fragment,
    register_test_manifest,
    text_response,
    write_config,
)


@pytest.fixture(autouse=True)
def _script() -> None:
    """每个用例从同一条脚本起步：一句普通回答。用例可以自己覆盖 `SCRIPT`。"""
    SCRIPT[:] = [text_response("好的。")]


async def _boot(root: Path, **kwargs: object) -> AgentInstance:
    manifests = kwargs.pop("manifests", TEST_MANIFESTS)
    assert isinstance(manifests, tuple)
    return await bootstrap(instance_dir=root, manifests=manifests, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------- 步骤 1–2：锁与配置


async def test_bootstrap_holds_the_instance_lock_until_stop(tmp_path: Path) -> None:
    write_config(tmp_path)
    instance = await _boot(tmp_path)
    try:
        with pytest.raises(NucleaError) as caught:
            InstanceLock(instance.layout.lock_path).acquire()
        assert caught.value.code is ErrorCode.CONFIG_INSTANCE_LOCKED
    finally:
        await instance.stop()
    # 停止之后锁必须真的放开，否则一次正常退出会砖掉实例直到下次陈旧回收。
    released = InstanceLock(instance.layout.lock_path).acquire()
    released.release()


async def test_a_failed_start_releases_the_lock(tmp_path: Path) -> None:
    """启动失败也要放锁：否则「配置写错了」会附赠一个锁死的实例目录。"""
    write_config(tmp_path, model={"name": None})
    with pytest.raises(NucleaError):
        await _boot(tmp_path)
    InstanceLock(tmp_path / "instance.lock").acquire().release()


async def test_a_builtin_setup_failure_keeps_its_precise_error(tmp_path: Path) -> None:
    """外部插件可隔离；宿主自己的内建基线坏了必须原样拒绝启动。"""
    broken_model = next(manifest for manifest in TEST_MANIFESTS if manifest.id == "model-openai")
    broken_model = broken_model.model_copy(
        update={"setup": "tests.runtime.test_bootstrap:setup_optional_failure"}
    )
    manifests = tuple(
        broken_model if manifest.id == "model-openai" else manifest
        for manifest in TEST_MANIFESTS
    )
    write_config(tmp_path)

    with pytest.raises(NucleaError) as caught:
        await _boot(tmp_path, manifests=manifests)

    assert caught.value.code is ErrorCode.PLUGIN_LOAD_FAILED
    assert caught.value.detail["exception"] == "RuntimeError"


async def test_a_broken_config_is_written_to_the_logs(tmp_path: Path) -> None:
    """`EDG-501` 的后半句。这是 `write_config_error()` 唯一的调用点。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.json").write_text('{"turn": {"max_iterations": -1}}', encoding="utf-8")
    with pytest.raises(NucleaError) as caught:
        await _boot(tmp_path)
    assert caught.value.code is ErrorCode.CONFIG_INVALID
    logs = list((tmp_path / "logs").glob("config-errors-*.jsonl"))
    assert logs, "配置解析错误必须留在 logs/ 里"
    record = json.loads(logs[0].read_text(encoding="utf-8").splitlines()[0])
    assert record["error"]["code"] == ErrorCode.CONFIG_INVALID.value


async def test_the_original_config_file_is_never_rewritten(tmp_path: Path) -> None:
    """`EDG-501` 的前半句：拒绝启动，且原文件一个字节都不改。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    raw = '{"turn": {"max_iterations": -1}}'
    (tmp_path / "config.json").write_text(raw, encoding="utf-8")
    with pytest.raises(NucleaError):
        await _boot(tmp_path)
    assert (tmp_path / "config.json").read_text(encoding="utf-8") == raw


# ------------------------------------------------------------------ 步骤 3：清单与 EDG-108


async def test_disabling_the_cli_entry_is_rejected(tmp_path: Path) -> None:
    """`EDG-108`：配置试图禁用 CLI 入口时显式拒绝并说明原因。"""
    write_config(tmp_path, plugins={"disable": ["cli-entry"]})
    with pytest.raises(NucleaError) as caught:
        await _boot(tmp_path)
    assert caught.value.code is ErrorCode.CONFIG_INVALID
    assert caught.value.detail["pointer"] == "/plugins/disable"
    assert caught.value.detail["plugin"] == "cli-entry"


async def test_disabling_an_optional_builtin_is_honoured(tmp_path: Path) -> None:
    write_config(tmp_path, plugins={"disable": ["tools-shell"]})
    instance = await _boot(tmp_path)
    try:
        assert "shell.exec" not in [spec.name for spec in instance.deps.tool_specs]
    finally:
        await instance.stop()


async def test_the_cli_stays_usable_with_every_disableable_builtin_off(tmp_path: Path) -> None:
    """§16.1 第 5 条：把能关的内建全关掉，CLI 仍然可用。"""
    write_config(
        tmp_path,
        plugins={"disable": ["tools-fs", "tools-file", "tools-shell", "commands-core"]},
    )
    instance = await _boot(tmp_path)
    try:
        assert instance.deps.tool_specs == ()
        code = await instance.run_cli(["-p", "在吗"], CancelToken())
        assert code == 0
    finally:
        await instance.stop()


# --------------------------------------------------------------- 步骤 4–7：配置块与裁剪


async def test_each_builtin_gets_its_own_config_block(tmp_path: Path) -> None:
    """九份内建共用一个 `Builtin()` ProviderId，配置块**只能**按 manifest 索引。"""
    write_config(tmp_path)
    instance = await _boot(tmp_path)
    try:
        blocks = {ctx.plugin_id: dict(ctx.config) for ctx in instance.contexts}
        assert blocks["session-jsonl"]["dir"] == str(instance.layout.sessions_dir)
        assert blocks["tools-fs"]["workspace"] == str(instance.config.workspace_root)
        assert "dir" not in blocks["tools-fs"]
    finally:
        await instance.stop()


async def test_derived_blocks_hand_down_what_builtins_cannot_know(tmp_path: Path) -> None:
    """`R4` 让内建够不着实例布局与 routing 小节，这四个键因此必须由装配根填。"""
    write_config(tmp_path, routing={"command_prefix": "!"})
    instance = await _boot(tmp_path)
    try:
        derived = builtin_config_blocks(
            instance.config.config, instance.layout, instance.config.workspace_root
        )
        assert derived["commands-core"]["prefix"] == "!"
        assert derived["cli-entry"]["instance_id"] == instance.layout.root.name
        assert derived["tools-shell"]["workspace"] == str(instance.config.workspace_root)
    finally:
        await instance.stop()


async def test_user_config_beats_the_derived_default(tmp_path: Path) -> None:
    """派生的是默认位置，不是不可覆盖的策略。"""
    elsewhere = tmp_path / "elsewhere"
    write_config(tmp_path, plugins={"session-jsonl": {"config": {"dir": str(elsewhere)}}})
    instance = await _boot(tmp_path)
    try:
        block = {ctx.plugin_id: dict(ctx.config) for ctx in instance.contexts}["session-jsonl"]
        assert block["dir"] == str(elsewhere)
    finally:
        await instance.stop()


async def test_disabling_one_tool_filters_both_the_declaration_and_the_registration(
    tmp_path: Path,
) -> None:
    """`TOL-006`：`keep` 与 `setup()` 同源于同一份配置，否则 `finish()` 会当场报错。"""
    write_config(tmp_path, plugins={"tools-fs": {"config": {"disable": ["fs.write", "fs.edit"]}}})
    instance = await _boot(tmp_path)
    try:
        names = {spec.name for spec in instance.deps.tool_specs}
        assert "fs.write" not in names
        assert "fs.read" in names
        active = {binding.name for binding in instance.report.active}
        assert "fs.write" not in active
    finally:
        await instance.stop()


async def test_disabling_one_command_keeps_the_rest(tmp_path: Path) -> None:
    write_config(tmp_path, plugins={"commands-core": {"config": {"disable": ["config"]}}})
    instance = await _boot(tmp_path)
    try:
        view = instance.runtime.instance_view
        assert view is not None
        names = {spec.name for spec in view.commands()}
        assert "config" not in names
        assert "help" in names
    finally:
        await instance.stop()


async def test_a_secret_reference_reaches_the_plugin(tmp_path: Path) -> None:
    """`plugins.<id>.secrets` → `ctx.secret()` → `${VAR}`，明文不进配置文档（`CFG-003`）。"""
    write_config(
        tmp_path,
        plugins={"tools-fs": {"secrets": {"api_key": "${NM_TEST_TOKEN}"}}},
    )
    instance = await _boot(tmp_path, env={"NM_TEST_TOKEN": "sk-0123456789abcdef"})
    try:
        document = json.dumps(instance.config.to_json(), ensure_ascii=False)
        assert "sk-0123456789abcdef" not in document
        assert "${NM_TEST_TOKEN}" in document
    finally:
        await instance.stop()


# ------------------------------------------------------------------ 步骤 8：必需能力


@pytest.mark.parametrize(
    ("missing", "kind"),
    [
        ("session-jsonl", "SESSION_STORE"),
        ("model-openai", "MODEL"),
        ("context-compact-basic", "TURN_COMPACTOR"),
        ("cli-entry", "CLI_ENTRY"),
    ],
)
async def test_missing_required_capabilities_fail_the_start(
    tmp_path: Path, missing: str, kind: str
) -> None:
    """§10.1 步骤 8：四项必需能力各须有一个生效实现，缺一即以 `CAPABILITY_MISSING` 终止。"""
    write_config(tmp_path)
    with pytest.raises(NucleaError) as caught:
        await _boot(tmp_path, manifests=manifests_without(missing))
    assert caught.value.code is ErrorCode.CAPABILITY_MISSING
    assert caught.value.detail["kind"] == kind


async def test_a_missing_model_name_names_the_field(tmp_path: Path) -> None:
    """「缺什么、去哪儿补」是启动错误的全部价值（`BAS-006` 的前身）。"""
    write_config(tmp_path, model={"provider": "fake"})
    with pytest.raises(NucleaError) as caught:
        await _boot(tmp_path)
    assert caught.value.code is ErrorCode.CONFIG_INVALID
    assert caught.value.detail["pointer"] == "/model/name"


async def test_a_failing_cli_override_falls_back_to_the_builtin(tmp_path: Path) -> None:
    """`EDG-108`/`BAS-010`：覆盖 CLI 的提供方没交出实现时强制回落，实例仍然有入口。"""
    broken = PluginManifest(
        id="cli-broken",
        version="0.1.0",
        sdk_range=">=4.0.0,<5.0.0",
        setup="tests.runtime.test_bootstrap:setup_broken_cli",
        capabilities=(
            CapabilityDecl(
                kind=CapabilityKind.CLI_ENTRY, name="broken", overrides="builtin:stdio"
            ),
        ),
    )
    write_config(tmp_path)
    instance = await _boot(tmp_path, manifests=(*TEST_MANIFESTS, broken))
    try:
        entries = [b for b in instance.report.active if b.kind is CapabilityKind.CLI_ENTRY]
        assert [binding.name for binding in entries] == ["stdio"]
        assert await instance.run_cli(["-p", "在吗"], CancelToken()) == 0
    finally:
        await instance.stop()


async def test_cli_fallback_stops_the_discarded_setup_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回落会重跑 setup；第一轮产生的任务和订阅必须先被完整清理。"""
    _SETUP_CONTEXTS.clear()
    tracked = PluginManifest(
        id="tracked-setup",
        version="0.1.0",
        sdk_range=">=4.0.0,<5.0.0",
        setup="tests.runtime.test_bootstrap:setup_with_side_effects",
        capabilities=(CapabilityDecl(kind=CapabilityKind.TOOL, name="startup.probe"),),
    )
    broken = PluginManifest(
        id="cli-broken",
        version="0.1.0",
        sdk_range=">=4.0.0,<5.0.0",
        setup="tests.runtime.test_bootstrap:setup_broken_cli",
        capabilities=(
            CapabilityDecl(
                kind=CapabilityKind.CLI_ENTRY, name="broken", overrides="builtin:stdio"
            ),
        ),
    )
    write_config(tmp_path)
    actual_cli_entry_from = bootstrap_module.cli_entry_from
    lookups = 0

    def miss_the_first_cli(registry: object) -> object:
        nonlocal lookups
        lookups += 1
        if lookups == 1:
            return None
        return actual_cli_entry_from(registry)  # type: ignore[arg-type]

    monkeypatch.setattr(bootstrap_module, "cli_entry_from", miss_the_first_cli)

    instance = await _boot(tmp_path, manifests=(*TEST_MANIFESTS, tracked, broken))
    try:
        assert len(_SETUP_CONTEXTS) == 2
        discarded, active = _SETUP_CONTEXTS
        assert discarded.stopping
        assert discarded.bridge._subscription is None  # noqa: SLF001 - 验失败回滚
        assert not discarded.pending_tasks
        assert not active.stopping
        assert len(active.pending_tasks) == 1
        await instance.start()
        (active_task,) = active.tasks
        assert not active_task.done()
    finally:
        await instance.stop()
    assert active_task.cancelled()


async def test_optional_setup_failure_keeps_prior_plugin_resources_owned(
    tmp_path: Path,
) -> None:
    """可选提供方失败后，已成功装配的插件资源仍由返回的实例持有。"""
    _SETUP_CONTEXTS.clear()
    tracked = PluginManifest(
        id="tracked-setup",
        version="0.1.0",
        sdk_range=">=4.0.0,<5.0.0",
        setup="tests.runtime.test_bootstrap:setup_with_side_effects",
        capabilities=(CapabilityDecl(kind=CapabilityKind.TOOL, name="startup.probe"),),
    )
    failing = PluginManifest(
        id="optional-failure",
        version="0.1.0",
        sdk_range=">=4.0.0,<5.0.0",
        setup="tests.runtime.test_bootstrap:setup_optional_failure",
        capabilities=(CapabilityDecl(kind=CapabilityKind.TOOL, name="startup.fail"),),
    )
    write_config(tmp_path)
    instance = await _boot(tmp_path, manifests=(*TEST_MANIFESTS, tracked, failing))
    tracked_ctx = _SETUP_CONTEXTS[0]
    try:
        assert not tracked_ctx.stopping
        assert tracked_ctx.bridge._subscription is not None  # noqa: SLF001 - 实例仍持有
        assert len(tracked_ctx.pending_tasks) == 1
        assert any(
            outcome.error is not None
            and outcome.error.code is ErrorCode.PLUGIN_LOAD_FAILED
            for outcome in instance.outcomes
        )
        await instance.start()
        (tracked_task,) = tracked_ctx.tasks
        assert not tracked_task.done()
    finally:
        await instance.stop()
    assert tracked_task.cancelled()


def setup_broken_cli(api: object) -> None:
    """声明了 CLI 入口却一项都不注册。`CapabilityHost.finish()` 会如实报出来。"""
    del api


_SETUP_CONTEXTS: list[RuntimePluginContext] = []


class _TrackingSink:
    def __init__(self) -> None:
        self.closed = False

    def __call__(self, event: object) -> None:
        del event

    def close(self) -> None:
        self.closed = True


class _StartupProbe:
    async def execute(self, invocation: ToolInvocation, cancel: CancelSignal) -> ToolResult:
        del cancel
        return ToolResult(
            call_id=invocation.call.call_id,
            ok=True,
            content="ok",
            truncated=False,
            side_effect=SideEffect.NONE,
        )


async def _wait_forever() -> None:
    await asyncio.Event().wait()


def setup_with_side_effects(api: NucleaAPI) -> None:
    """留下两类必须回滚的 setup 副作用：事件订阅与后台任务。"""
    ctx = api.ctx
    assert isinstance(ctx, RuntimePluginContext)
    ctx.events.subscribe(EventName.INSTANCE_READY, lambda event: None)
    ctx.spawn_task(_wait_forever(), name="startup-probe")
    _SETUP_CONTEXTS.append(ctx)
    api.register_tool(
        ToolSpec(
            name="startup.probe",
            description="启动回滚测试探针。",
            parameters={"type": "object", "additionalProperties": False},
        ),
        _StartupProbe(),
    )


def setup_optional_failure(api: NucleaAPI) -> None:
    del api
    raise RuntimeError("boom")


# ------------------------------------------------------------------ 步骤 9–10 与运行


async def test_the_startup_sequence_is_traceable_through_events(tmp_path: Path) -> None:
    """开发方案的验收：十步可通过事件序列逐步追踪。"""
    write_config(tmp_path)
    instance = await _boot(tmp_path)
    try:
        await instance.start()
        names = [event.name for event in instance.diagnostics.events.events()]
        assert names[0] is EventName.INSTANCE_STARTING
        assert EventName.PLUGIN_DISCOVERED in names
        assert EventName.PLUGIN_LOADED in names
        assert names[-1] is EventName.INSTANCE_READY
    finally:
        await instance.stop()
    names = [event.name for event in instance.diagnostics.events.events()]
    # `D28`：停止序列夹在两条实例事件之间——每个提供方停下来时各发一条
    # `plugin.deactivated`，因此这里断言的是首尾与包含关系，不是末两条。
    stopping = names.index(EventName.INSTANCE_STOPPING)
    assert names[-1] is EventName.INSTANCE_STOPPED
    assert EventName.PLUGIN_DEACTIVATED in names[stopping:]


async def test_external_plugin_discovery_reaches_the_diagnostics(tmp_path: Path) -> None:
    """`D25`：`plugins.enabled` 不是一个没人读的键——`/plugins` 列得出候选与原因。"""
    source = tmp_path / "ext"
    package = source / "acme"
    package.mkdir(parents=True)
    manifest_path = package / "plugin.toml"
    manifest_path.write_text(
        'id = "acme"\nversion = "2.0.0"\nsdk_range = ">=0.1"\n'
        'setup = "acme.plugin:setup"\n\n'
        '[[capabilities]]\nkind = "tool"\nname = "acme.ping"\n',
        encoding="utf-8",
    )
    register_test_manifest(manifest_path)
    write_config(tmp_path, plugins={"enabled": ["acme"]})
    instance = await _boot(tmp_path)
    try:
        (status,) = instance.diagnostics.plugins()
        assert status.plugin_id == "acme"
        assert status.version == "2.0.0"
        assert status.capabilities == ("tool:acme.ping",)
        # **发现不是加载**：`setup` 指向一个根本不存在的模块，实例照样起来了（`D27`）。
        assert "acme.ping" not in [spec.name for spec in instance.deps.tool_specs]
    finally:
        await instance.stop()


async def test_a_cli_turn_goes_through_the_channel_pump(tmp_path: Path) -> None:
    """`MSG-007`：CLI 的输入是一条真的 `InboundMessage`，出站经同一条 `deliver` 回来。"""
    write_config(tmp_path)
    instance = await _boot(tmp_path)
    try:
        code = await instance.run_cli(["-p", "统计一下"], CancelToken())
        assert code == 0
        channel = dict(instance.channels)["cli"]
        console = channel._console  # noqa: SLF001 - 断言渲染结果需要它
        assert console.rendered == ["好的。"]
        assert console.last_state is StreamState.FINAL
    finally:
        await instance.stop()


async def test_history_lands_in_the_instance_sessions_directory(tmp_path: Path) -> None:
    """`D17` 点名的那个坑：没把 `dir` 交下去，会话会写进插件私有目录。"""
    write_config(tmp_path)
    instance = await _boot(tmp_path)
    try:
        await instance.run_cli(["-p", "记住这句话"], CancelToken())
    finally:
        await instance.stop()
    key = SessionKey(channel_id="cli", conversation_id="local")
    history, _ = instance.layout.session_paths(key.storage_id())
    assert history.exists(), "会话历史必须落在实例的 sessions/ 目录"


async def test_stopping_twice_is_safe(tmp_path: Path) -> None:
    write_config(tmp_path)
    instance = await _boot(tmp_path)
    await instance.stop()
    await instance.stop()


def test_the_fake_model_manifest_keeps_the_real_shape() -> None:
    """本套用例只换模型这一项——换多了它就退化成 `tests/kernel/` 的重复。"""
    real = {manifest.id for manifest in BUILTIN_MANIFESTS}
    assert {manifest.id for manifest in TEST_MANIFESTS} == real
    fake = next(m for m in TEST_MANIFESTS if m.id == "model-openai")
    assert fake.setup.startswith("tests.")
    assert FAKE_MODEL_ID == "fake-model"


# ------------------------------------------- 步骤 8 的第三项：长期记忆的召回（`D44`）


async def test_no_memory_section_means_no_recall_at_all(tmp_path: Path) -> None:
    """默认不启用。**装上一个记忆插件不该悄悄改变每一轮请求的内容**——那是运维必须
    显式说出的决定，而 `memory_providers_from()` 有返回值不等于用户想用它。"""
    MEMORIES.clear()
    MEMORIES["r1"] = memory_fragment("用户偏好简短回答")
    write_config(tmp_path)
    instance = await _boot(tmp_path, manifests=manifests_with_memory())
    try:
        assert instance.deps.memory is None
    finally:
        await instance.stop()


async def test_a_named_memory_backend_is_wired_into_the_orchestrator(tmp_path: Path) -> None:
    """`D39` 留下的缺口在这里合上：注册一条 `MEMORY`、在配置里点名，它就真的接上了。

    这条断言的是**装配**；「记忆真的进了模型消息」在
    `tests/kernel/test_memory_recall.py` 里验（那一层看得见组装结果）。
    """
    MEMORIES.clear()
    MEMORIES["r1"] = memory_fragment("用户偏好简短回答")
    write_config(
        tmp_path,
        plugins={"enabled": []},
        memory={"provider": MEMORY_NAME, "recall_limit": 2, "on_failure": "fail"},
    )
    instance = await _boot(tmp_path, manifests=manifests_with_memory())
    try:
        recall = instance.deps.memory
        assert recall is not None
        assert recall.name == MEMORY_NAME
        assert recall.limit == 2
        # `on_failure: fail` → `critical=True`。那个字面量的唯一消费点是
        # `MemorySection.critical`。
        assert recall.critical is True
        assert isinstance(recall.provider, FakeMemoryProvider)
    finally:
        await instance.stop()


async def test_naming_a_backend_that_is_not_registered_refuses_to_start(tmp_path: Path) -> None:
    """静默退回「没有记忆」会让用户以为记忆在工作。指针指向那一个要改的键。"""
    write_config(tmp_path, memory={"provider": "sqlite"})
    with pytest.raises(NucleaError) as caught:
        await _boot(tmp_path, manifests=manifests_with_memory())
    assert caught.value.code is ErrorCode.CAPABILITY_MISSING
    assert caught.value.detail["field"] == "/memory/provider"
    assert caught.value.detail["available"] == [MEMORY_NAME]


async def test_recall_really_runs_during_a_turn(tmp_path: Path) -> None:
    """功能形态：跑一次真 turn，后端收到的查询词就是这一轮的输入。"""
    MEMORIES.clear()
    MEMORIES["r1"] = memory_fragment("用户偏好简短回答")
    write_config(tmp_path, memory={"provider": MEMORY_NAME})
    instance = await _boot(tmp_path, manifests=manifests_with_memory())
    try:
        recall = instance.deps.memory
        assert recall is not None
        provider = recall.provider
        assert isinstance(provider, FakeMemoryProvider)
        await instance.submit(inbound("c0", "记得我的偏好吗", message_id="m0"))
        assert provider.queries == ["记得我的偏好吗"]
    finally:
        await instance.stop()
