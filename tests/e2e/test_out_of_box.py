"""需求 §16.1 的五条开箱可用里程碑，逐条一个分节（`D24` ★）。

职责：验「装好之后只配一个凭据就能用」这件事在**真实内建清单**上成立，并覆盖首次运行、
缺凭据、可中断、禁用内建四条边界。
不负责：单元级断言（`tests/kernel/`、`tests/builtins/`）、装配链内部结构
（`tests/runtime/`）、线格式细节（`tests/builtins/test_model_openai.py`）。

**这里唯一的替身是传输层**（`conftest.recorder`）。模型供应商、会话存储、上下文组装、
文件工具、命令、CLI 入口与装配根全部是生产实现——里程碑说的是「全新环境」，用 Fake 能力
验它等于验了一台不存在的机器。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from nucleamind.contracts import (
    CancelReason,
    ErrorCode,
    InboundMessage,
    NucleaError,
    Sender,
    TurnStatus,
)
from nucleamind.kernel.config import load_config
from nucleamind.kernel.turn import CancelToken
from nucleamind.runtime.bootstrap import bootstrap
from nucleamind.runtime.cli.main import app
from nucleamind.runtime.first_run import MODEL_API_KEY_ENV, MODEL_PLUGIN_ID, MODEL_SECRET_NAME
from nucleamind.runtime.instance import AgentInstance

from ._support import say, use_tool
from .conftest import Recorder

#: 哨兵凭据。必须匹配 `contracts/errors.py::_SECRET_VALUE_PATTERNS`（`sk-` + ≥16 字符），
#: 否则「没泄漏」可能只是因为它压根不长得像密钥。
SENTINEL_KEY = "sk-e2e0123456789abcdefghij"


@pytest.fixture
def instance_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一台「全新机器」：空实例目录 + 临时 HOME + 没有导出任何凭据。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv(MODEL_API_KEY_ENV, raising=False)
    return tmp_path / "instance"


def init(instance_dir: Path) -> int:
    """跑一次真的 `nm init`。"""
    return app(["init", "--instance-dir", str(instance_dir)])


async def run_prompt(instance_dir: Path, prompt: str) -> int:
    """装配一次真实例并跑一条单次执行（`nm run -p` 的正文）。"""
    instance = await bootstrap(instance_dir=instance_dir)
    try:
        return await instance.run_cli(["-p", prompt], CancelToken())
    finally:
        await instance.stop()


# ------------------------------------------------------- ① 只配凭据即可完成一次带工具的 turn


def test_a_fresh_environment_completes_a_tool_calling_turn_with_only_a_credential(
    instance_dir: Path,
    recorder: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """§16.1 第 1 条。**全程只有一次人工动作**：导出 `OPENAI_API_KEY`。

    turn 里真的落了一个文件——「带工具调用」若只断言模型收到了 tool 消息，一个把工具结果
    编出来的实现也能通过。
    """
    assert init(instance_dir) == 0
    monkeypatch.setenv(MODEL_API_KEY_ENV, SENTINEL_KEY)
    recorder.script(
        use_tool("fs.write", {"path": "note.txt", "content": "开箱可用"}),
        say("已经写好了。"),
    )

    assert asyncio.run(run_prompt(instance_dir, "把「开箱可用」写进 note.txt")) == 0

    written = instance_dir / "workspace" / "note.txt"
    assert written.read_text(encoding="utf-8") == "开箱可用"
    assert "已经写好了。" in capsys.readouterr().out
    # 两次请求：一次拿到 tool_call，一次拿到最终回答。工具结果确实回给了模型。
    assert len(recorder.requests) == 2
    second = json.loads(recorder.requests[1].content)
    assert any(message.get("role") == "tool" for message in second["messages"])


def test_the_generated_config_is_loadable_and_the_session_survives(
    instance_dir: Path, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """生成的配置要能被**自己**加载，且那次 turn 真的进了会话历史。

    「生成了一份自己读不了的配置」是首次运行最难堪的失败，而它只有在这一层才看得见。
    """
    assert init(instance_dir) == 0
    monkeypatch.setenv(MODEL_API_KEY_ENV, SENTINEL_KEY)
    loaded = load_config(instance_dir=instance_dir)
    assert loaded.config.model.name
    assert loaded.origin_of("model", "name") == "config.json"

    recorder.script(say("记住了。"))
    assert asyncio.run(run_prompt(instance_dir, "记住这句话")) == 0

    sessions = sorted((instance_dir / "sessions").glob("*.jsonl"))
    assert sessions, "会话历史没有落盘"
    assert "记住这句话" in sessions[0].read_text(encoding="utf-8")


# ------------------------------------------------------------- ② 无插件时列得出全部内建能力


def test_capabilities_lists_every_builtin_with_its_provider(
    instance_dir: Path, recorder: Recorder, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """§16.1 第 2 条。一个插件都没装时，`/capabilities` 要列出全部内建能力及提供方。"""
    assert init(instance_dir) == 0
    monkeypatch.setenv(MODEL_API_KEY_ENV, SENTINEL_KEY)
    # 命令不进模型：录制脚本是空的，多出任何一次请求都会当场失败。
    recorder.script()

    assert asyncio.run(run_prompt(instance_dir, "/capabilities")) == 0

    listed = capsys.readouterr().out
    for entry in (
        "tool:fs.read",
        "tool:fs.write",
        "tool:shell.exec",
        "command:help",
        "model:openai",
        "session_store:jsonl",
        "context:basic",
        "cli_entry:stdio",
        "channel:cli",
    ):
        assert entry in listed, f"{entry} 没有出现在 /capabilities 的输出里"
    assert "builtin" in listed


# ------------------------------------------------- ③ 缺凭据的错误指向文件与字段名且不泄露值


def test_a_missing_credential_points_at_the_file_and_the_field(
    instance_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """§16.1 第 3 条、`BAS-006`、`EDG-502`。

    「可操作」的可断言形态是三样都在：**哪个文件**、**哪个字段**、**哪个环境变量**。
    """
    assert init(instance_dir) == 0

    with pytest.raises(NucleaError) as caught:
        asyncio.run(run_prompt(instance_dir, "你好"))

    error = caught.value
    assert error.code is ErrorCode.CONFIG_SECRET_MISSING
    rendered = _render(error)
    assert "config.json" in rendered
    assert f"/plugins/{MODEL_PLUGIN_ID}/secrets/{MODEL_SECRET_NAME}" in rendered
    assert MODEL_API_KEY_ENV in rendered


def _render(error: NucleaError) -> str:
    """把一条错误的**全部**可见面渲染成一段文本。

    `detail` 里有 `MappingProxyType`（契约层的冻结快照），`json.dumps` 不认得它，
    因此用 `default=str` ——这套用例要的是「这几个字符串在不在」，不是一份严格的 JSON。
    """
    return json.dumps(
        {"message": error.user_message, "detail": dict(error.detail)},
        ensure_ascii=False,
        default=str,
    )


def test_a_missing_credential_never_prints_the_value(
    instance_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """把凭据导出成一个**空串**——「配置了但取不到」，错误里仍然不得出现任何值。

    这条与上一条分开：值泄漏与指引缺失是两种不同的失败，合在一个用例里会让其中一种在
    另一种失败时被掩盖。
    """
    assert init(instance_dir) == 0
    monkeypatch.setenv(MODEL_API_KEY_ENV, "")
    capsys.readouterr()

    assert app(["run", "--instance-dir", str(instance_dir)]) == 2

    captured = capsys.readouterr()
    assert MODEL_API_KEY_ENV in captured.err
    assert SENTINEL_KEY not in captured.err + captured.out


# --------------------------------------------------------- ④ 交互式入口可中断，会话可继续


async def _cancel_then_continue(
    instance_dir: Path, gate: asyncio.Event
) -> tuple[TurnStatus | None, TurnStatus | None]:
    """跑一个卡在等模型的 turn、中断它，再跑一个正常的 turn。返回两次的终态。"""
    instance = await bootstrap(instance_dir=instance_dir)
    await instance.start()
    try:
        first = asyncio.create_task(
            instance.submit(_message(instance, "第一句", message_id="m1"))
        )
        await _wait_for_a_live_turn(instance)
        for turn_id in instance.orchestrator.live_turns:
            instance.orchestrator.cancel(turn_id, CancelReason.USER)
        # 取消**已经登记**之后才放行下一片：engine 在收到分片时做检查点 3，因此这个顺序
        # 让「取消赢了」成为确定结论而不是赛跑的结果。放行还让那条流正常收尾——
        # 挂着一个永不结束的响应会让下一次请求卡在同一个连接上（第一版就是这么写的，
        # 表现是第二个 turn 等满 60 s 的流空闲看门狗）。
        gate.set()
        cancelled = await first
        second = await instance.submit(_message(instance, "第二句", message_id="m2"))
        return (
            None if cancelled.outcome is None else cancelled.outcome.status,
            None if second.outcome is None else second.outcome.status,
        )
    finally:
        await instance.stop()


def _message(instance: AgentInstance, text: str, *, message_id: str) -> InboundMessage:
    """构造一条与 CLI 完全同形的入站消息（`MSG-007`：没有绕过契约的旁路）。"""
    return InboundMessage(
        message_id=message_id,
        instance_id=instance.instance_id,
        channel_id="cli",
        conversation_id="local",
        sender=Sender(user_id="local", is_operator=True),
        content=text,
        timestamp=datetime.now(UTC),
    )


async def _wait_for_a_live_turn(instance: AgentInstance) -> None:
    """等到 orchestrator 真的开始跑一个 turn。**不用 sleep 定长**：那会让这条用例的
    稳定性取决于机器快慢。"""
    for _ in range(2000):
        if instance.orchestrator.live_turns:
            return
        await asyncio.sleep(0)
    raise AssertionError("没有等到任何在跑的 turn")


# ------------------------------------------------------ ⑤ 禁用全部可禁用内建后 CLI 仍可用


#: 可禁用的内建。`session-jsonl`（会话存储）、`context-basic`（`CTX-006` 的兜底）、
#: `context-compact-basic`（Turn 内请求压缩）与 `model-openai` 是必需能力的唯一
#: 提供方，禁用它们等于要一个不能正常回答的实例；
#: `cli-entry` 由 `EDG-108` 显式拒绝，见下一条用例。
DISABLEABLE = ("tools-fs", "tools-file", "tools-shell", "commands-core")


def _write_config(instance_dir: Path, document: dict[str, object]) -> None:
    instance_dir.mkdir(parents=True, exist_ok=True)
    (instance_dir / "config.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )


def test_the_cli_still_works_with_every_disableable_builtin_turned_off(
    instance_dir: Path, recorder: Recorder, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """§16.1 第 5 条：没装任何 Channel 插件、且关掉全部可禁用内建时，CLI 仍然可用。"""
    monkeypatch.setenv(MODEL_API_KEY_ENV, SENTINEL_KEY)
    _write_config(
        instance_dir,
        {
            "model": {"name": "gpt-4o-mini", "provider": "openai"},
            "plugins": {
                "disable": list(DISABLEABLE),
                MODEL_PLUGIN_ID: {"secrets": {MODEL_SECRET_NAME: f"${{{MODEL_API_KEY_ENV}}}"}},
            },
        },
    )
    recorder.script(say("我还在。"))

    assert asyncio.run(run_prompt(instance_dir, "还在吗")) == 0
    assert "我还在。" in capsys.readouterr().out


def test_disabling_the_cli_entry_is_refused_with_a_reason(
    instance_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`EDG-108`：静默忽略会让用户以为自己关掉了 CLI，照办则让实例没有任何入口。"""
    monkeypatch.setenv(MODEL_API_KEY_ENV, SENTINEL_KEY)
    _write_config(
        instance_dir,
        {
            "model": {"name": "gpt-4o-mini", "provider": "openai"},
            "plugins": {
                "disable": ["cli-entry"],
                MODEL_PLUGIN_ID: {"secrets": {MODEL_SECRET_NAME: f"${{{MODEL_API_KEY_ENV}}}"}},
            },
        },
    )

    with pytest.raises(NucleaError) as caught:
        asyncio.run(bootstrap(instance_dir=instance_dir))
    assert caught.value.code is ErrorCode.CONFIG_INVALID
    assert "/plugins/disable" in _render(caught.value)


def test_every_single_tool_and_command_can_be_turned_off(
    instance_dir: Path, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`TOL-006` 的另一半：逐个关掉工具与命令（而不是整份提供方）后实例照样起得来。

    这条与上一条不同——它走的是 `wire_capabilities(keep=...)` 那条声明过滤路径，
    忘了传 `keep` 时这里会以 `PLUGIN_LOAD_FAILED` 失败。
    """
    from nucleamind.builtins import commands_core, tools_fs, tools_shell

    monkeypatch.setenv(MODEL_API_KEY_ENV, SENTINEL_KEY)
    _write_config(
        instance_dir,
        {
            "model": {"name": "gpt-4o-mini", "provider": "openai"},
            "plugins": {
                MODEL_PLUGIN_ID: {"secrets": {MODEL_SECRET_NAME: f"${{{MODEL_API_KEY_ENV}}}"}},
                "tools-fs": {"config": {"disable": list(tools_fs.TOOL_NAMES)}},
                "tools-shell": {"config": {"disable": [tools_shell.TOOL_NAME]}},
                "commands-core": {"config": {"disable": list(commands_core.COMMAND_NAMES)}},
            },
        },
    )
    recorder.script(say("好。"))

    assert asyncio.run(run_prompt(instance_dir, "在吗")) == 0


def test_the_interrupted_turn_ends_cancelled_and_the_session_continues(
    instance_dir: Path, recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§16.1 第 4 条：中断进行中的 turn，会话仍可继续。

    中断由 `TurnOrchestrator.cancel()` 发起——那正是 `runtime/cli/commands/run.py`
    的 `Ctrl-C` 状态机调的那个方法，只是这里不必真的发一个信号。
    """
    assert init(instance_dir) == 0
    monkeypatch.setenv(MODEL_API_KEY_ENV, SENTINEL_KEY)
    gate = asyncio.Event()
    recorder.script(_gated_stream(gate), say("第二轮好了。"))

    first, second = asyncio.run(_cancel_then_continue(instance_dir, gate))
    assert first is TurnStatus.CANCELLED
    assert second is TurnStatus.COMPLETED


def _gated_stream(gate: asyncio.Event) -> Callable[[httpx.Request], httpx.Response]:
    """一条吐了半句就停住、等 `gate` 放行的流。

    用它而不是 `sleep`：取消要发生在**真实的等待点**上，而那个点就是下一片 SSE。
    """

    async def body() -> AsyncIterator[bytes]:
        yield b'data: {"choices": [{"index": 0, "delta": {"content": "\xe5\x8d\x8a"}}]}\n\n'
        await gate.wait()
        yield b'data: {"choices": [{"index": 0, "delta": {"content": "\xe5\x8f\xa5"}}]}\n\n'
        yield b'data: {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}\n\n'
        yield b"data: [DONE]\n\n"

    return lambda _: httpx.Response(
        200, content=body(), headers={"content-type": "text/event-stream"}
    )
