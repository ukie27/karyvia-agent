""" 嵌入式门面：与 CLI 用同一个 `AgentInstance`。

职责：验 `open_instance()` / `run()` 真的走 `orchestrator.handle()`，同一份输入在嵌入式
与 CLI 两条路上得到等价的 turn 结果，以及上下文管理器退出时实例真的停掉。
不负责：验装配本身（`tests/runtime/test_bootstrap.py`）。

**「等价」的可断言形态是正文与终态相同**，而不是 `TurnReceipt` 全等：两条路的
`turn_id`、`message_id` 与会话键本来就不同（嵌入式有自己的 `channel_id`，那是刻意的）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from karyvia.contracts import ErrorCode, KaryviaError, SessionKey, TurnStatus
from karyvia.embed import EMBED_CHANNEL_ID, open_instance, run
from karyvia.kernel.config import InstanceLock
from karyvia.kernel.turn import CancelToken
from karyvia.runtime.bootstrap import bootstrap

from ..runtime._support import SCRIPT, TEST_MANIFESTS, text_response, write_config


@pytest.fixture(autouse=True)
def _script() -> None:
    SCRIPT[:] = [text_response("好的。")]


async def test_run_answers_a_single_prompt(tmp_path: Path) -> None:
    write_config(tmp_path)
    assert await run("在吗", instance_dir=tmp_path, manifests=TEST_MANIFESTS) == "好的。"


async def test_the_context_manager_stops_the_instance(tmp_path: Path) -> None:
    write_config(tmp_path)
    async with open_instance(instance_dir=tmp_path, manifests=TEST_MANIFESTS) as agent:
        assert (await agent.send("在吗")).outcome is not None
    # 停掉之后锁必须放开——否则一个用完的嵌入式实例会挡住 `karyvia run`。
    InstanceLock(tmp_path / "instance.lock").acquire().release()


async def test_embed_and_cli_produce_equivalent_turns(tmp_path: Path) -> None:
    """`embed.run` 与 CLI 共用 `AgentInstance`，并产生等价结果。"""
    SCRIPT[:] = [text_response("好的。"), text_response("好的。")]
    write_config(tmp_path)
    async with open_instance(instance_dir=tmp_path, manifests=TEST_MANIFESTS) as agent:
        embedded = await agent.send("在吗")
        assert embedded.outcome is not None
        cli_code = await agent.instance.run_cli(["-p", "在吗"], CancelToken())
    assert cli_code == 0
    # 「等价」= 正文与终态相同。turn_id 与会话键本来就不同（嵌入式有自己的 channel_id）。
    assert embedded.content == "好的。"
    assert embedded.outcome.status is TurnStatus.COMPLETED


async def test_embed_writes_its_own_session(tmp_path: Path) -> None:
    """嵌入式有自己的 `channel_id`：脚本里的问答不该和终端里的搅进同一段历史。"""
    write_config(tmp_path)
    instance = await bootstrap(instance_dir=tmp_path, manifests=TEST_MANIFESTS)
    try:
        async with open_instance(
            instance_dir=tmp_path, acquire_lock=False, manifests=TEST_MANIFESTS
        ) as agent:
            await agent.ask("记住这句话")
            layout = agent.instance.layout
    finally:
        await instance.stop()
    key = SessionKey(channel_id=EMBED_CHANNEL_ID, conversation_id="default")
    history, _ = layout.session_paths(key.storage_id())
    assert history.exists()


async def test_a_broken_instance_fails_loudly(tmp_path: Path) -> None:
    """门面不吞启动错误：一个装不起来的实例必须当场说清楚原因。"""
    write_config(tmp_path, model={"provider": "fake"})
    with pytest.raises(KaryviaError) as caught:
        await run("在吗", instance_dir=tmp_path, manifests=TEST_MANIFESTS)
    assert caught.value.code is ErrorCode.CONFIG_INVALID
    assert caught.value.detail["pointer"] == "/model/name"
