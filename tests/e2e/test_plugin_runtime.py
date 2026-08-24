"""需求 §16.2 的八条 Plugin Runtime 里程碑，逐条一个分节（`D30` ★）。

职责：验「插件体系可用」这件事在**真实安装的外部插件**上成立——`examples/plugins/` 下的
两个包经 entry point 被发现、被加载、参与真实 turn、覆盖内建能力、被禁用后按配置消失。
不负责：插件自身的行为（各插件 `tests/` 里的契约测试）、装配链内部结构
（`tests/runtime/`）、单元级断言（`tests/kernel/`）。

**这里唯一的替身是传输层**（`conftest.recorder`），与 `test_out_of_box.py` 同一条理由：
里程碑说的是「装上一个插件之后」，用 Fake 能力验它等于验了一台不存在的机器。因此这套
用例**要求两个示例插件已经由全局管理器安装**：

    nm plugins install --no-deps examples/plugins/nucleamind-plugin-echo-tool
    nm plugins install --no-deps examples/plugins/nucleamind-plugin-session-memory

没装时第一条用例会以一句明确的话失败，而不是安静地少验几件事。启动只读全局安装目录，
不会把环境里恰好存在的 Python 包误当成已安装插件。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from nucleamind.contracts import UNTRUSTED_DATA_PREFIX, ErrorCode, NucleaError
from nucleamind.kernel.turn import CancelToken
from nucleamind.runtime.bootstrap import bootstrap
from nucleamind.runtime.cli.main import app
from nucleamind.runtime.first_run import MODEL_API_KEY_ENV, MODEL_PLUGIN_ID, MODEL_SECRET_NAME
from nucleamind.runtime.inspect import inspect_capabilities, inspect_plugins
from nucleamind.runtime.plugin_home import GlobalPluginHome
from tests.runtime._support import register_test_manifest

from ._support import say, use_tool
from .conftest import Recorder

#: 与 `test_out_of_box.py` 同一个哨兵：必须长得像密钥，否则「没泄漏」可能只是因为它
#: 压根不匹配 `contracts/errors.py` 的脱敏形状。
SENTINEL_KEY = "sk-e2e0123456789abcdefghij"

ECHO_PLUGIN = "echo-tool"
ECHO_TOOL = "echo.say"
MEMORY_PLUGIN = "session-memory"


@pytest.fixture
def instance_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一台「全新机器」：空实例目录 + 临时 HOME + 没有导出任何凭据。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv(MODEL_API_KEY_ENV, raising=False)
    return tmp_path / "instance"


def write_config(instance_dir: Path, plugins: dict[str, object]) -> None:
    """写一份最小可用配置。`plugins` 那一段由每条用例自己给。"""
    instance_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "model": {"name": "gpt-4o-mini", "provider": "openai"},
        "plugins": {
            MODEL_PLUGIN_ID: {"secrets": {MODEL_SECRET_NAME: f"${{{MODEL_API_KEY_ENV}}}"}},
            **plugins,
        },
    }
    (instance_dir / "config.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )


async def run_prompt(instance_dir: Path, prompt: str) -> int:
    """装配一次真实例并跑一条单次执行（`nm run -p` 的正文）。"""
    instance = await bootstrap(instance_dir=instance_dir)
    try:
        return await instance.run_cli(["-p", prompt], CancelToken())
    finally:
        await instance.stop()


def render(error: NucleaError) -> str:
    """把一条错误的全部可见面渲染成文本，供「这几个字符串在不在」的断言用。"""
    return json.dumps(
        {"message": error.user_message, "detail": dict(error.detail)},
        ensure_ascii=False,
        default=str,
    )


# ------------------------------------------------------ ⓪ 前提：两个示例插件真的装上了


def test_both_example_plugins_are_installed_as_entry_points() -> None:
    """整套用例的前提。**放在最前面单独成一条**：装漏了要看到一句能照做的话，
    而不是后面十几条各失败一次。"""
    names = {item.plugin_id for item in GlobalPluginHome.resolve().catalog()}
    missing = {ECHO_PLUGIN, MEMORY_PLUGIN} - names
    assert not missing, (
        f"示例插件没装：{sorted(missing)}。请先跑 "
        "`nm plugins install --no-deps examples/plugins/nucleamind-plugin-echo-tool` 和 "
        "`nm plugins install --no-deps examples/plugins/nucleamind-plugin-session-memory`"
    )


def test_installing_is_not_enabling(instance_dir: Path) -> None:
    """`DST-002`：装上不等于启用。没写进 `plugins.enabled` 的候选连 manifest 都不读，
    可观察的证据就是它的 `version` 是空串。"""
    write_config(instance_dir, {})

    statuses = {str(row.plugin_id): row for row in inspect_plugins(instance_dir=instance_dir).statuses}

    assert statuses[ECHO_PLUGIN].version == ""
    assert statuses[ECHO_PLUGIN].reason == "未列入 plugins.enabled"


# ------------------------------------------- ① 不修改 engine / orchestrator 即可加载插件


#: turn 主循环的两个模块。它们是「Kernel 不认识任何具体插件」这条承诺的落点。
_MAIN_LOOP = (
    "src/nucleamind/kernel/turn/engine.py",
    "src/nucleamind/kernel/turn/orchestrator.py",
)


def test_the_main_loop_names_no_plugin() -> None:
    """§16.2 第 1 条的静态形态：主循环里不出现任何插件 id 或插件包名。

    功能形态是下一条用例（插件的工具真的参与了一次 turn）。两条都要：一个跑得通的 turn
    证明不了「没为它改过 engine」，而这条文本断言证明不了它真能跑。
    """
    repo_root = Path(__file__).resolve().parents[2]
    for relative in _MAIN_LOOP:
        source = (repo_root / relative).read_text(encoding="utf-8")
        for needle in (ECHO_PLUGIN, MEMORY_PLUGIN, "nucleamind_plugin_", "builtins"):
            assert needle not in source, f"{relative} 里出现了 {needle!r}"


def test_the_example_plugins_are_covered_by_the_import_guard() -> None:
    """§16.2 第 6 条：示例插件不导入 Kernel 私有模块。

    判定不在这里重写一遍——`R4` 的实现在 `tests/architecture/test_import_boundaries.py`，
    它作用于 `plugin_package_roots()` 交出的每一个目录。这条用例只证明**这两个包真的在
    那张名单里**：一条自己写的扫描通过了，不代表守卫扫过它们。
    """
    from tests.architecture._common import plugin_package_roots

    covered = {root.name for root in plugin_package_roots()}
    assert {"nucleamind-plugin-echo-tool", "nucleamind-plugin-session-memory"} <= covered


# --------------------------------------------------------- ② 插件注册的工具参与真实 turn


def test_a_plugin_tool_takes_part_in_a_real_turn(
    instance_dir: Path,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """§16.2 第 2 条。工具由插件注册、被模型调用、结果回到模型、答案印给用户。

    前缀取自 `plugins.echo-tool.config`——顺带证明 `ctx.config` 那一块真的交到了插件手上
    （`CFG-002`），而不是插件拿到了一份空配置照样「能跑」。
    """
    monkeypatch.setenv(MODEL_API_KEY_ENV, SENTINEL_KEY)
    write_config(
        instance_dir,
        {"enabled": [ECHO_PLUGIN], ECHO_PLUGIN: {"config": {"prefix": ">> "}}},
    )
    recorder.script(use_tool(ECHO_TOOL, {"text": "你好"}), say("回显完了。"))

    assert asyncio.run(run_prompt(instance_dir, "回显「你好」")) == 0

    # 模型看得见这个工具：它出现在第一次请求的工具清单里。
    first = json.loads(recorder.requests[0].content)
    assert ECHO_TOOL in {tool["function"]["name"] for tool in first["tools"]}
    # 工具真的跑了，结果真的回给了模型——而且带着配置里的前缀。
    second = json.loads(recorder.requests[1].content)
    tool_messages = [item for item in second["messages"] if item.get("role") == "tool"]
    assert len(tool_messages) == 1
    # 正文包在不可信数据块里（`D42`）：示例插件没有显式表态，因此走默认的 `UNTRUSTED`。
    # 这一条端到端地证明了包裹发生在**真实的线格式上**，而不只是 `fold_tool_result` 的
    # 单元测试里。
    assert ">> 你好" in tool_messages[0]["content"]
    assert tool_messages[0]["content"].startswith(UNTRUSTED_DATA_PREFIX)
    assert "回显完了。" in capsys.readouterr().out


def test_the_plugin_tool_shows_up_in_capabilities(
    instance_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`NFR-502`：能力表里每一条都带提供方标识，插件的那条写着 `plugin:echo-tool`。"""
    monkeypatch.setenv(MODEL_API_KEY_ENV, SENTINEL_KEY)
    write_config(instance_dir, {"enabled": [ECHO_PLUGIN]})

    assert app(["capabilities", "--instance-dir", str(instance_dir)]) == 0

    listed = capsys.readouterr().out
    assert f"tool:{ECHO_TOOL} ← plugin:{ECHO_PLUGIN}" in listed


# ------------------------------------------------- ③ 覆盖内建 session store 且覆盖可见


def test_a_plugin_overrides_the_builtin_session_store(
    instance_dir: Path, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§16.2 第 3 条。覆盖生效的可观察形态：会话历史**不再落盘**。

    只断言 `nm capabilities` 里有那对关系是不够的——报告说了什么与真的用了谁是两件事。
    """
    monkeypatch.setenv(MODEL_API_KEY_ENV, SENTINEL_KEY)
    write_config(instance_dir, {"enabled": [MEMORY_PLUGIN]})
    recorder.script(say("记住了。"))

    assert asyncio.run(run_prompt(instance_dir, "记住这句话")) == 0

    assert not list((instance_dir / "sessions").glob("*.jsonl")), "内存实现不该往磁盘写会话"


def test_the_override_relation_is_printed_with_both_providers(
    instance_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """§16.2 第 3 条的另一半、技术方案 §8.3 第 4 条：**不静默替换**。

    被覆盖的那一方仍然可见，只是不生效——用户要能一眼看出自己的会话历史换了后端。
    """
    monkeypatch.setenv(MODEL_API_KEY_ENV, SENTINEL_KEY)
    write_config(instance_dir, {"enabled": [MEMORY_PLUGIN]})

    assert app(["capabilities", "--instance-dir", str(instance_dir)]) == 0

    listed = capsys.readouterr().out
    assert "session_store:jsonl ← builtin" in listed
    assert f"session_store:memory ← plugin:{MEMORY_PLUGIN}" in listed
    # 生效的是插件那一份，被覆盖的是内建那一份——顺序不能反。
    shadowed = listed.split("被覆盖（")[1]
    assert shadowed.index("session_store:jsonl") < shadowed.index("session_store:memory")


# ---------------------------------------- ④ 禁用后能力消失，恢复内建与否由 on_disable 决


def _disabled(choice: str | None) -> dict[str, object]:
    """启用又禁用同一个插件的那份配置。`choice` 为 `None` 表示不写 `on_disable`。"""
    entry: dict[str, object] = {} if choice is None else {"on_disable": choice}
    return {"enabled": [MEMORY_PLUGIN], "disable": [MEMORY_PLUGIN], MEMORY_PLUGIN: entry}


def test_disabling_an_overriding_plugin_without_a_choice_is_refused(
    instance_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§16.2 第 4 条、`BAS-004`。禁用一个覆盖者之后**内建不得隐式复活**。

    今天不做判定的话，被禁用的插件根本不注册、覆盖关系不存在，内建就自动回来了——那正是
    `BAS-004` 禁止的隐式恢复。错误指向**那一个要写的键**，不是「配置有问题」。
    """
    monkeypatch.setenv(MODEL_API_KEY_ENV, SENTINEL_KEY)
    write_config(instance_dir, _disabled(None))

    with pytest.raises(NucleaError) as caught:
        asyncio.run(bootstrap(instance_dir=instance_dir))

    assert caught.value.code is ErrorCode.CONFIG_INVALID
    rendered = render(caught.value)
    assert f"/plugins/{MEMORY_PLUGIN}/on_disable" in rendered
    assert "session_store:jsonl" in rendered


def test_restore_builtin_brings_the_jsonl_store_back(
    instance_dir: Path, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`restore_builtin`：用户明确说「退回去」，会话重新落盘。"""
    monkeypatch.setenv(MODEL_API_KEY_ENV, SENTINEL_KEY)
    write_config(instance_dir, _disabled("restore_builtin"))
    recorder.script(say("记住了。"))

    assert asyncio.run(run_prompt(instance_dir, "记住这句话")) == 0

    sessions = sorted((instance_dir / "sessions").glob("*.jsonl"))
    assert sessions, "restore_builtin 之后会话应当重新落盘"
    assert "记住这句话" in sessions[0].read_text(encoding="utf-8")


def test_leave_missing_keeps_the_capability_gone(
    instance_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`leave_missing`：那项能力保持缺失，实例以 `CAPABILITY_MISSING` 拒绝启动。

    这**不是**事故——用户要的就是「现在没有会话存储了」，而 `SES-003` 不允许把没有历史
    伪装成有历史。诊断指出缺的是哪一项。
    """
    monkeypatch.setenv(MODEL_API_KEY_ENV, SENTINEL_KEY)
    write_config(instance_dir, _disabled("leave_missing"))

    with pytest.raises(NucleaError) as caught:
        asyncio.run(bootstrap(instance_dir=instance_dir))

    assert caught.value.code is ErrorCode.CAPABILITY_MISSING
    assert "SESSION_STORE" in render(caught.value)


def test_leave_missing_shows_up_as_disabled_not_as_absent(
    instance_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """能力表里要**看得见它为什么不在**。

    抑制作用在解析而不是注册上，因此内建照常注册、照常出现在报告里，只是标着「已禁用」。
    一项从未注册过的能力在报告里连一行都没有，用户无从判断是没装还是被关了。
    """
    from nucleamind.runtime.plugin_disable import LEAVE_MISSING_REASON

    monkeypatch.setenv(MODEL_API_KEY_ENV, SENTINEL_KEY)
    write_config(instance_dir, _disabled("leave_missing"))

    report = asyncio.run(inspect_capabilities(instance_dir=instance_dir)).report
    assert report is not None
    disabled = {ref.name: reason for ref, reason in report.disabled}
    assert disabled == {"jsonl": LEAVE_MISSING_REASON}
    assert not [ref for ref in report.active if ref.kind.value == "session_store"]


def test_a_disabled_plugin_without_overrides_needs_no_choice(
    instance_dir: Path, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`on_disable` 只在真的发生过覆盖时才要求表态。

    `echo-tool` 只是新增一项工具，禁用它就是少一项工具——没有「要不要回来」这个问题，
    为它也要求一次表态只会让这个键变成噪声。
    """
    monkeypatch.setenv(MODEL_API_KEY_ENV, SENTINEL_KEY)
    write_config(instance_dir, {"enabled": [ECHO_PLUGIN], "disable": [ECHO_PLUGIN]})
    recorder.script(say("好。"))

    assert asyncio.run(run_prompt(instance_dir, "在吗")) == 0
    first = json.loads(recorder.requests[0].content)
    assert ECHO_TOOL not in {tool["function"]["name"] for tool in first["tools"]}


# ------------------------------------------- ⑤ 三类失败各有稳定错误码与可诊断的输出


def test_a_bad_plugin_config_is_reported_and_the_instance_still_starts(
    instance_dir: Path, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第一类：**配置错误**。`prefix` 声明为 string，给一个整数。

    插件写错配置时实例仍要起得来（`PLG-004`），那个插件被丢掉并留下一条带 JSON
    Pointer 的记录。
    """
    monkeypatch.setenv(MODEL_API_KEY_ENV, SENTINEL_KEY)
    write_config(
        instance_dir, {"enabled": [ECHO_PLUGIN], ECHO_PLUGIN: {"config": {"prefix": 42}}}
    )
    recorder.script(say("好。"))

    assert asyncio.run(run_prompt(instance_dir, "在吗")) == 0

    (failure,) = inspect_plugins(instance_dir=instance_dir).inventory.failures
    assert failure.plugin_id == ECHO_PLUGIN
    assert failure.error.code is ErrorCode.CONFIG_INVALID
    assert f"/plugins/{ECHO_PLUGIN}/config/prefix" in render(failure.error)


def _toml_value(value: object) -> str:
    """把一个 JSON 值渲染成 TOML 字面量。

    只覆盖本文件用到的三种形状（字符串、列表、内联表）。**不用 `json.dumps` 糊过去**：
    JSON 的对象是 `{"k": v}`，而 TOML 的内联表是 `{ k = v }`——第一版就是那么写的，
    结果两条用例都在「TOML 语法错误」上失败，验不到它们本来要验的东西。
    """
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        items = ", ".join(f"{key} = {_toml_value(item)}" for key, item in value.items())
        return "{ " + items + " }"
    raise AssertionError(f"这个夹具不认识的 TOML 值：{value!r}")


def _directory_plugin(root: Path, plugin_id: str, manifest: dict[str, object]) -> Path:
    """把测试声明登记成一个全局候选。返回写下的 manifest 路径。

    用目录形态而不是再发一个包：这两条用例要的是**坏掉的** manifest，而一个装得上的坏插件
    没法同时留在 `examples/` 里当范例。目录形态的 manifest 是 `plugin.toml`，读它连一次
    import 都不需要。
    """
    directory = root / plugin_id
    directory.mkdir(parents=True, exist_ok=True)
    lines = [f"{key} = {_toml_value(value)}" for key, value in manifest.items()]
    path = directory / "plugin.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    register_test_manifest(path)
    return path


def test_an_incompatible_sdk_range_is_refused_with_its_own_code(
    instance_dir: Path, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第二类：**SDK 不兼容**（`SDK-005`）。不带病加载，错误码与配置错误分得开。"""
    monkeypatch.setenv(MODEL_API_KEY_ENV, SENTINEL_KEY)
    _directory_plugin(
        instance_dir / "external",
        "from-the-future",
        {
            "id": "from-the-future",
            "version": "1.0.0",
            "sdk_range": ">=99.0.0,<100.0.0",
            "setup": "nucleamind_plugin_echo_tool:setup",
            "capabilities": [{"kind": "tool", "name": "future.thing"}],
        },
    )
    write_config(instance_dir, {"enabled": ["from-the-future"]})
    recorder.script(say("好。"))

    assert asyncio.run(run_prompt(instance_dir, "在吗")) == 0

    (failure,) = inspect_plugins(instance_dir=instance_dir).inventory.failures
    assert failure.error.code is ErrorCode.PLUGIN_SDK_INCOMPATIBLE
    assert ">=99.0.0,<100.0.0" in render(failure.error)


def test_a_setup_that_cannot_be_loaded_is_reported_per_provider(
    instance_dir: Path, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第三类：**运行失败**。manifest 过了阶段 A，`setup` 却跑不起来。

    与前两类的区别是它发生在**加载阶段**，因此结论在 `Wiring.outcomes` 里按提供方列出，
    而不是在阶段 A 的清单里——`nm capabilities` 把这一段单独印成「加载失败的提供方」。
    """
    monkeypatch.setenv(MODEL_API_KEY_ENV, SENTINEL_KEY)
    _directory_plugin(
        instance_dir / "external",
        "broken-setup",
        {
            "id": "broken-setup",
            "version": "1.0.0",
            "sdk_range": ">=4.0.0,<5.0.0",
            "setup": "nucleamind_plugin_echo_tool:no_such_function",
            "capabilities": [{"kind": "tool", "name": "broken.thing"}],
        },
    )
    write_config(instance_dir, {"enabled": ["broken-setup"]})
    recorder.script(say("好。"))

    assert asyncio.run(run_prompt(instance_dir, "在吗")) == 0

    inspection = asyncio.run(inspect_capabilities(instance_dir=instance_dir))
    failed = [item for item in inspection.outcomes if item.error is not None]
    assert [str(item.provider) for item in failed] == ["plugin:broken-setup"]
    assert failed[0].error is not None
    assert failed[0].error.code is ErrorCode.PLUGIN_LOAD_FAILED


def test_the_three_failure_classes_have_distinct_codes() -> None:
    """三类失败必须**分得开**：三个码相等的话，「该去改什么」就要靠读消息猜。"""
    codes = {
        ErrorCode.CONFIG_INVALID,
        ErrorCode.PLUGIN_SDK_INCOMPATIBLE,
        ErrorCode.PLUGIN_LOAD_FAILED,
    }
    assert len(codes) == 3


# ------------------------------------- ⑦⑧ 同一套契约测试，与被覆盖能力的关键行为回归


def test_both_session_stores_run_the_same_contract_suite() -> None:
    """§16.2 第 7 条：内建默认实现与同类插件通过**同一套** `SessionStoreContract`。

    断言的是「两个测试类的基类是同一个」而不是「两边都有测试」——后者用两套各自宽松的
    断言也能满足，而那恰恰是契约测试要防的事（`NFR-702`）。
    """
    import importlib.util

    from nucleamind.sdk.testing import SessionStoreContract
    from tests.builtins.test_session_jsonl import TestJsonlSessionStore

    root = Path(__file__).resolve().parents[2]
    path = (
        root
        / "examples/plugins/nucleamind-plugin-session-memory/tests/test_session_memory.py"
    )
    spec = importlib.util.spec_from_file_location("_session_memory_tests", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert SessionStoreContract in TestJsonlSessionStore.__bases__
    assert SessionStoreContract in module.TestMemorySessionStore.__bases__


def test_history_written_before_the_override_is_still_readable_after_restoring(
    instance_dir: Path, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§16.2 第 8 条：被覆盖能力的关键行为有回归。

    三段：内建写一句 → 插件接管（那句不该出现在插件的内存里）→ `restore_builtin` 之后
    原来那句仍然读得到。会话历史是用户资产，一次覆盖不该把它弄丢。
    """
    monkeypatch.setenv(MODEL_API_KEY_ENV, SENTINEL_KEY)

    write_config(instance_dir, {})
    recorder.script(say("第一句好了。"))
    assert asyncio.run(run_prompt(instance_dir, "第一句")) == 0
    (session,) = sorted((instance_dir / "sessions").glob("*.jsonl"))
    before = session.read_text(encoding="utf-8")
    assert "第一句" in before

    write_config(instance_dir, {"enabled": [MEMORY_PLUGIN]})
    recorder.script(say("第二句好了。"))
    assert asyncio.run(run_prompt(instance_dir, "第二句")) == 0
    # 插件接管期间那份 JSONL 一个字节都没动过。
    assert session.read_text(encoding="utf-8") == before

    write_config(instance_dir, _disabled("restore_builtin"))
    recorder.script(say("第三句好了。"))
    assert asyncio.run(run_prompt(instance_dir, "第三句")) == 0
    after = session.read_text(encoding="utf-8")
    assert "第一句" in after and "第三句" in after
    # 「第二句」进的是内存实现，进程结束就没了——这正是那个插件的语义。
    assert "第二句" not in after
