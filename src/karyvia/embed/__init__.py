"""嵌入式 Python SDK：把 Karyvia 当库用的薄门面。

职责：暴露 `open_instance()`（异步上下文管理器）与 `run()`（一次性问答），两者都只是
`runtime.bootstrap()` + `AgentInstance` 的包装。
不负责：复制任何 turn 编排逻辑、导入 `builtins/`、提供第二套配置或注册路径。

**它与 CLI 使用同一个 `AgentInstance`**。门面里不复制 Turn 逻辑，否则嵌入式调用与 `karyvia`
就可能产生不同结果。

```python
async with open_instance(instance="default") as agent:
    print(await agent.ask("统计一下仓库里的 Python 文件"))
```
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from karyvia.contracts import InboundMessage, InstanceId, Sender
from karyvia.runtime.bootstrap import BUILTIN_MANIFESTS, PluginManifest, bootstrap
from karyvia.runtime.instance import AgentInstance, TurnReceipt

__all__ = ["EmbeddedAgent", "open_instance", "run"]

#: 嵌入式调用的默认渠道标识。它是一个**真的** `channel_id`：会话键因此与 CLI 的那条
#: 分开，一个脚本里的问答不会和终端里的对话搅进同一段历史。
EMBED_CHANNEL_ID = "embed"


class EmbeddedAgent:
    """一个已就绪实例的最小调用面。"""

    def __init__(self, instance: AgentInstance, *, conversation_id: str = "default") -> None:
        self.instance = instance
        self.conversation_id = conversation_id
        self._counter = 0

    async def ask(self, content: str, *, conversation_id: str | None = None) -> str:
        """问一句，拿正文。

        **走的是 `orchestrator.handle()`**，与 CLI 与任何 Channel 完全同一个入口
        。被去重或被队列拒时返回空串，诊断在
        `TurnReceipt`（用 `send()` 拿它）。
        """
        receipt = await self.send(content, conversation_id=conversation_id)
        return receipt.content

    async def send(self, content: str, *, conversation_id: str | None = None) -> TurnReceipt:
        """同 `ask()`，但交回完整的 `TurnReceipt`（终态、错误、全部出站消息）。"""
        self._counter += 1
        message = InboundMessage(
            message_id=f"embed-{self._counter}",
            instance_id=InstanceId(str(self.instance.instance_id)),
            channel_id=EMBED_CHANNEL_ID,
            conversation_id=conversation_id or self.conversation_id,
            # 嵌入式调用方就是实例拥有者：它已经拿到了进程内的一切。
            sender=Sender(user_id="embed", is_operator=True),
            content=content,
            timestamp=datetime.now(UTC),
        )
        return await self.instance.submit(message)


@asynccontextmanager
async def open_instance(
    *,
    instance: str | None = None,
    instance_dir: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    overrides: Sequence[str] | None = None,
    conversation_id: str = "default",
    acquire_lock: bool = True,
    manifests: Sequence[PluginManifest] = BUILTIN_MANIFESTS,
) -> AsyncGenerator[EmbeddedAgent]:
    """装配、启动、用完即停。

    **默认取实例锁**：嵌入式调用与 `karyvia run` 会写同一份会话历史，同时跑两个写者正是
     要挡的。明知在做只读实验时可以传 `acquire_lock=False`。

    `manifests` 让嵌入方换掉能力清单（默认就是内建那一份）。它**不是**第二套注册路径——
    交出去的仍然是同一个 `bootstrap()`，只是清单换了。测试用它把模型换成 Fake。
    """
    agent_instance = await bootstrap(
        instance=instance,
        instance_dir=instance_dir,
        env=env,
        overrides=overrides,
        acquire_lock=acquire_lock,
        manifests=manifests,
    )
    try:
        await agent_instance.start()
        yield EmbeddedAgent(agent_instance, conversation_id=conversation_id)
    finally:
        await agent_instance.stop()


async def run(
    prompt: str,
    *,
    instance: str | None = None,
    instance_dir: Path | str | None = None,
    conversation_id: str = "default",
    manifests: Sequence[PluginManifest] = BUILTIN_MANIFESTS,
) -> str:
    """一次性问答：装配 → 问一句 → 停。脚本里最常见的那种用法。"""
    async with open_instance(
        instance=instance,
        instance_dir=instance_dir,
        conversation_id=conversation_id,
        manifests=manifests,
    ) as agent:
        return await agent.ask(prompt)
