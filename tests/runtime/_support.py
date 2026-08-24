"""`tests/runtime/` 与 `tests/embed/` 的公共装配：把真实内建清单的模型换成 Fake。

职责：提供 `TEST_MANIFESTS`（真实的 `BUILTIN_MANIFESTS`，但 `model-openai` 换成一个
注册 `FakeModelProvider` 的假插件）、写一份最小 `config.json` 的助手，以及脚本化模型回复
的入口。
不负责：任何断言。

**只换模型这一项**：`D23` 要验的是装配链本身（配置块怎么交下去、能力怎么取回、Channel
泵怎么转），把内建换成 Fake 就等于让这套用例证明一条没人会走的路（`tests/integration/`
的同一条判据）。模型是唯一必须换掉的——它是清单里唯一会出网的那个。

`setup` 用 `"tests.runtime._support:setup_fake_model"` 引用本模块：`import_setup()` 接受
任何 `module:func`，内建与外部插件在这一点上没有区别（`SDK-007`）。
"""

from __future__ import annotations

import asyncio
import json
import tomllib
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

from nucleamind.builtins.registry import BUILTIN_MANIFESTS
from nucleamind.contracts import (
    CancelSignal,
    CapabilityKind,
    ContextFragment,
    FragmentKind,
    FragmentScope,
    InboundMessage,
    InstanceId,
    JsonValue,
    ModelResponse,
    OutboundMessage,
    Sender,
    TrustLevel,
)
from nucleamind.runtime.plugin_home import GlobalPluginHome, InstalledPlugin
from nucleamind.sdk import CapabilityDecl, NucleaAPI, PluginManifest, parse_manifest
from nucleamind.sdk.testing import (
    FAKE_MODEL_ID,
    FakeModelProvider,
    text_response,
    tool_call_response,
)

__all__ = [
    "FAKE_MEMORY",
    "FAKE_MODEL_ID",
    "MEMORIES",
    "MEMORY_NAME",
    "MULTI_CHANNEL_ID",
    "SCRIPT",
    "TEST_MANIFESTS",
    "FakeMemoryProvider",
    "ScriptedChannel",
    "inbound",
    "manifests_with_memory",
    "manifests_with_multi_channel",
    "manifests_without",
    "memory_fragment",
    "register_test_manifest",
    "setup_fake_memory",
    "setup_fake_model",
    "setup_multi_channel",
    "text_response",
    "tool_call_response",
    "write_config",
]

#: 假模型这次要按顺序返回的响应。用例在 `bootstrap()` **之前**改它。
#: 模块级可变状态在生产代码里是错的，在这里是必需的——`setup(api)` 的签名只有 `api`，
#: 而脚本必须由用例决定。每个用例自己 `SCRIPT[:] = [...]`。
SCRIPT: list[ModelResponse] = []


def setup_fake_model(api: NucleaAPI) -> None:
    """假模型插件的 `setup`。与任何内建同型：拿 Host、注册一次、返回。"""
    api.register_model_provider("fake", FakeModelProvider(list(SCRIPT)))


#: 假模型的 manifest。没有模型时由 Runtime 的基础能力校验拒绝启动。
FAKE_MODEL: PluginManifest = PluginManifest(
    id="model-openai",
    version="0.1.0",
    sdk_range=">=4.0.0,<5.0.0",
    setup="tests.runtime._support:setup_fake_model",
    capabilities=(CapabilityDecl(kind=CapabilityKind.MODEL, name="fake"),),
)

#: 真实内建清单，模型换成 Fake。其余六份**原封不动**。
TEST_MANIFESTS: tuple[PluginManifest, ...] = tuple(
    FAKE_MODEL if manifest.id == "model-openai" else manifest for manifest in BUILTIN_MANIFESTS
)


def manifests_without(plugin_id: str) -> tuple[PluginManifest, ...]:
    """去掉某一份 manifest 的清单，用来验「必需能力缺失」。"""
    return tuple(manifest for manifest in TEST_MANIFESTS if manifest.id != plugin_id)


def write_config(root: Path, **sections: JsonValue) -> Path:
    """在实例目录里写一份 `config.json`。默认已经指定了模型。"""
    document: dict[str, JsonValue] = {"model": {"name": FAKE_MODEL_ID, "provider": "fake"}}
    document.update(sections)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "config.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def register_test_manifest(path: Path) -> None:
    """把测试 TOML 转成全局安装目录中的 entry point 候选。"""
    manifest = parse_manifest(tomllib.loads(path.read_text(encoding="utf-8")), origin=str(path))
    attribute = f"_MANIFEST_{manifest.id.replace('-', '_').upper()}"
    globals()[attribute] = manifest
    home = GlobalPluginHome.resolve()
    home.package_dir(manifest.id).mkdir(parents=True, exist_ok=True)
    rows = [item for item in home.catalog() if item.plugin_id != manifest.id]
    home.write_catalog(
        (
            *rows,
            InstalledPlugin(
                manifest.id,
                manifest.version,
                "runtime-test",
                f"{__name__}:{attribute}",
                str(path),
            ),
        )
    )


# --------------------------------------------------------------- 多会话 Channel（`D33`）

#: 这条假 Channel 的 `channel_id`。内建 CLI 只有一个 conversation，验不了按 conversation
#: 扇出——那正是本 Channel 存在的理由。
MULTI_CHANNEL_ID: str = "multi"


def inbound(conversation: str, text: str, *, message_id: str | None = None) -> InboundMessage:
    """造一条入站消息。`message_id` 默认唯一，避免撞上去重。"""
    return InboundMessage(
        message_id=message_id or f"{conversation}-{text}",
        instance_id=InstanceId("test"),
        channel_id=MULTI_CHANNEL_ID,
        conversation_id=conversation,
        sender=Sender(user_id="u1"),
        content=text,
        timestamp=datetime.now(UTC),
    )


class ScriptedChannel:
    """一条由用例驱动的 Channel：想推几条推几条，投递结果全留在 `delivered` 里。

    `receive()` 在 `close()` 之前不会结束——真实平台的连接就是这样，而「泵还活着」
    正是并发用例需要的前提。
    """

    def __init__(self) -> None:
        self.delivered: list[OutboundMessage] = []
        self.started = 0
        self.stopped = 0
        #: 投递时抛的东西（`D43`）。契约允许 `Channel.deliver` 抛 `EXTERNAL_CHANNEL`——
        #: 这个开关就是为了驱动那条路径：路由点必须捕获它、发一条
        #: `channel.delivery_failed`，而 turn 照样走到自己的终态（`EDG-204`）。
        self.fail_delivery_with: Exception | None = None
        self._inbox: asyncio.Queue[InboundMessage | None] = asyncio.Queue()

    @property
    def channel_id(self) -> str:
        return MULTI_CHANNEL_ID

    def push(self, message: InboundMessage) -> None:
        self._inbox.put_nowait(message)

    def close(self) -> None:
        self._inbox.put_nowait(None)

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1
        self.close()

    async def receive(self) -> AsyncIterator[InboundMessage]:
        while True:
            message = await self._inbox.get()
            if message is None:
                return
            yield message

    async def deliver(self, message: OutboundMessage) -> None:
        # **先记再抛**：真实实现失败时也可能已经发出去一部分，而用例要能看到「它试过」。
        self.delivered.append(message)
        if self.fail_delivery_with is not None:
            raise self.fail_delivery_with


#: 装配根按 manifest 索引交配置块，因此这条 Channel 也要有自己的 manifest。
#: 用例通过 `dict(instance.channels)["multi"]` 拿到实例，直接往里推消息。
MULTI_CHANNEL: PluginManifest = PluginManifest(
    id="multi-channel",
    version="0.1.0",
    sdk_range=">=4.0.0,<5.0.0",
    setup="tests.runtime._support:setup_multi_channel",
    capabilities=(CapabilityDecl(kind=CapabilityKind.CHANNEL, name=MULTI_CHANNEL_ID),),
)


def setup_multi_channel(api: NucleaAPI) -> None:
    api.register_channel(MULTI_CHANNEL_ID, ScriptedChannel())


def manifests_with_multi_channel() -> tuple[PluginManifest, ...]:
    return (*TEST_MANIFESTS, MULTI_CHANNEL)


# ----------------------------------------------------------- 假记忆后端（`D44`）

#: 这条假 `MEMORY` 能力的名字。用例把它写进 `memory.provider`。
MEMORY_NAME: str = "fake-memory"

#: 这个后端每次 `recall()` 交出的记录。用例在 `bootstrap()` **之前**改它，
#: 理由与 `SCRIPT` 完全相同（`setup(api)` 的签名只有 `api`）。
MEMORIES: dict[str, ContextFragment] = {}


def memory_fragment(content: str, *, priority: int = 100) -> ContextFragment:
    """造一条 `agent` 范围的记忆片段。"""
    return ContextFragment(
        source="plugin:fake-memory",
        kind=FragmentKind.MEMORY,
        content=content,
        priority=priority,
        estimated_tokens=8,
        scope=FragmentScope.AGENT,
        trust=TrustLevel.UNTRUSTED,
    )


class FakeMemoryProvider:
    """`contracts.MemoryProvider`。只实现 `recall()`——这条路径不写记忆。"""

    def __init__(self, records: dict[str, ContextFragment]) -> None:
        self.records = records
        self.queries: list[str] = []

    async def remember(self, fragment: ContextFragment, cancel: CancelSignal) -> str:
        raise NotImplementedError

    async def recall(
        self,
        query: str,
        *,
        scope: FragmentScope,
        limit: int,
        cancel: CancelSignal,
    ) -> Mapping[str, ContextFragment]:
        self.queries.append(query)
        assert scope is FragmentScope.AGENT, "kernel 只该按 agent 范围召回"
        return dict(list(self.records.items())[:limit])

    async def forget(self, record_id: str) -> bool:
        raise NotImplementedError


FAKE_MEMORY: PluginManifest = PluginManifest(
    id="fake-memory",
    version="0.1.0",
    sdk_range=">=4.0.0,<5.0.0",
    setup="tests.runtime._support:setup_fake_memory",
    capabilities=(CapabilityDecl(kind=CapabilityKind.MEMORY, name=MEMORY_NAME),),
)


def setup_fake_memory(api: NucleaAPI) -> None:
    api.register_memory_provider(MEMORY_NAME, FakeMemoryProvider(dict(MEMORIES)))


def manifests_with_memory() -> tuple[PluginManifest, ...]:
    return (*TEST_MANIFESTS, FAKE_MEMORY)
