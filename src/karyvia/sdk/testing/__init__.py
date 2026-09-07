"""公开测试工具包：Fake 实现与契约测试基类。

职责：把 `fakes.py` 的 Fake 能力与 `contracts.py` 的 8 个契约测试基类作为一个入口导出。
不负责：任何生产行为——本包只应出现在测试代码里。

刻意**不**被 `karyvia.sdk` 的包根导入：夹具只在测试期需要，让
`import karyvia.sdk` 顺带拉起它们不合理。用
`from karyvia.sdk.testing import FakeModelProvider` 显式获取。
"""

from __future__ import annotations

from .capabilities import (
    ECHO_SPEC,
    EchoTool,
    FakeCliEntry,
    FakeInstanceView,
    FakeMemoryProvider,
    FakePluginContext,
    FakeTurnControl,
    NullChannel,
    RecordingEventSubscriber,
    StaticContextProvider,
    StaticTurnContextCompactor,
)
from .contracts import (
    ChannelContract,
    ContextProviderContract,
    MemoryProviderContract,
    ModelProviderContract,
    SessionStoreContract,
    ToolContract,
    TurnContextCompactorContract,
)
from .fakes import (
    FAKE_MODEL_ID,
    FakeModelProvider,
    InMemorySessionStore,
    ManualCancel,
    RecordingHook,
    make_correlation,
    text_response,
    tool_call_response,
)

__all__ = [
    "ECHO_SPEC",
    "FAKE_MODEL_ID",
    "ChannelContract",
    "ContextProviderContract",
    "EchoTool",
    "FakeCliEntry",
    "FakeInstanceView",
    "FakeMemoryProvider",
    "FakeModelProvider",
    "FakePluginContext",
    "FakeTurnControl",
    "InMemorySessionStore",
    "ManualCancel",
    "MemoryProviderContract",
    "ModelProviderContract",
    "NullChannel",
    "RecordingEventSubscriber",
    "RecordingHook",
    "SessionStoreContract",
    "StaticContextProvider",
    "StaticTurnContextCompactor",
    "ToolContract",
    "TurnContextCompactorContract",
    "make_correlation",
    "text_response",
    "tool_call_response",
]
