"""`memory` 插件用例的替身与工厂。**模块名带插件前缀**是刻意的。

`testpaths` 一次收集整个 `plugins/`，而 pytest 按模块名去重：两个插件各有一个
`_fakes.py` 时，先导入的会顶掉后一个，另一棵测试树整体 `ImportError`。
**单独跑各自目录看不出来，跑全量才炸**（`D34` 就是这么发现的）。

职责：一个带 `state_dir` 的 `PluginContext`、一个可控时钟、以及构造
`ToolInvocation` / `CommandInvocation` / `SessionSnapshot` 的小工厂。
不负责：断言（在各 `test_memory_*.py` 里）。

**本插件不出网**，因此这里没有那份零网络 autouse 夹具——它连一行 httpx 都没有。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from karyvia_plugin_memory.record import SOURCE, estimate_tokens

from karyvia.contracts import (
    CommandInvocation,
    ContextFragment,
    Correlation,
    FragmentKind,
    FragmentScope,
    InboundMessage,
    InstanceId,
    JsonValue,
    Role,
    Sender,
    SessionKey,
    SessionMessage,
    SessionSnapshot,
    ToolCall,
    ToolInvocation,
    TrustLevel,
    TurnId,
)
from karyvia.sdk.testing import FakePluginContext

#: 用例统一用它。三个分量都是 `validate_identifier` 认得的普通标识。
KEY = SessionKey(channel_id="cli", conversation_id="local", scope="proj")

#: 一个固定的时间基点。**用例不依赖真实墙钟**：过期、排序与「新的在前」都要可控。
EPOCH = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)


class Clock:
    """每次调用前进一秒的假时钟。

    前进而不是恒定：`entries()` 按 `created_at` 倒序排，恒定时钟会让顺序变成
    「谁先写谁在前」的字典序偶然结果，而那不是被测行为。
    """

    def __init__(self, start: datetime = EPOCH) -> None:
        self.now = start

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


class MemoryContext(FakePluginContext):
    """带真实 `state_dir` 的上下文。"""

    def __init__(
        self,
        state_dir: Path,
        *,
        config: Mapping[str, JsonValue] | None = None,
    ) -> None:
        super().__init__(
            "memory",
            config=config,
            state_dir=state_dir,
        )


class Api:
    """一个只记录注册了什么的 `KaryviaAPI` 替身。

    刻意不用生产的 `CapabilityHost`：`R4` 禁止插件的测试树 import `kernel/`，而
    「声明 ⊆ 注册」那条不变量由 `test_memory_plugin.py` 自己按 manifest 对照。
    """

    def __init__(self, ctx: FakePluginContext) -> None:
        self._ctx = ctx
        self.tools: dict[str, object] = {}
        self.tool_specs: dict[str, object] = {}
        self.commands: dict[str, object] = {}
        self.command_specs: dict[str, object] = {}
        self.context_providers: dict[str, object] = {}
        self.memory_providers: dict[str, object] = {}

    @property
    def ctx(self) -> FakePluginContext:
        return self._ctx

    def register_tool(self, spec: object, handler: object) -> None:
        name = getattr(spec, "name")
        self.tools[name] = handler
        self.tool_specs[name] = spec

    def register_command(self, spec: object, handler: object) -> None:
        name = getattr(spec, "name")
        self.commands[name] = handler
        self.command_specs[name] = spec

    def register_context_provider(self, name: str, provider: object) -> None:
        self.context_providers[name] = provider

    def register_memory_provider(self, name: str, provider: object) -> None:
        self.memory_providers[name] = provider

    @property
    def registered(self) -> frozenset[tuple[str, str]]:
        """全部注册项的 `(kind, name)`，用于与 manifest 声明逐条对照。"""
        return frozenset(
            [
                *(("memory", name) for name in self.memory_providers),
                *(("context", name) for name in self.context_providers),
                *(("tool", name) for name in self.tools),
                *(("command", name) for name in self.commands),
            ]
        )


def make_fragment(
    content: str = "用户偏好深色模式",
    *,
    scope: FragmentScope = FragmentScope.AGENT,
    expires_at: datetime | None = None,
    **overrides: object,
) -> ContextFragment:
    fields: dict[str, object] = {
        "source": SOURCE,
        "kind": FragmentKind.MEMORY,
        "content": content,
        "priority": 50,
        "estimated_tokens": estimate_tokens(content),
        "scope": scope,
        "trust": TrustLevel.UNTRUSTED,
        "expires_at": expires_at,
    }
    fields.update(overrides)
    return ContextFragment(**fields)  # type: ignore[arg-type]


def make_correlation(key: SessionKey = KEY) -> Correlation:
    return Correlation(
        session_key=key, turn_id=TurnId("turn-1"), instance_id=InstanceId("inst-1")
    )


def make_invocation(
    name: str, arguments: Mapping[str, JsonValue], *, key: SessionKey = KEY
) -> ToolInvocation:
    return ToolInvocation(
        call=ToolCall(call_id="call-1", name=name, arguments=arguments),
        correlation=make_correlation(key),
        timeout_ms=5_000,
    )


def make_command(
    args: Sequence[str], *, is_operator: bool = False, key: SessionKey = KEY
) -> CommandInvocation:
    message = InboundMessage(
        message_id="msg-1",
        instance_id=InstanceId("inst-1"),
        channel_id=key.channel_id,
        conversation_id=key.conversation_id,
        sender=Sender(user_id="someone", is_operator=is_operator),
        content="/memory " + " ".join(args),
        timestamp=EPOCH,
    )
    return CommandInvocation(
        name="memory",
        args=tuple(args),
        raw_text=message.content,
        message=message,
        correlation=make_correlation(key),
    )


def make_snapshot(*contents: str, key: SessionKey = KEY) -> SessionSnapshot:
    """一段以 user 消息结尾的会话。空参数即空快照。"""
    messages = tuple(
        SessionMessage(
            message_id=f"m{index}",
            role=Role.USER,
            content=content,
            created_at=EPOCH + timedelta(seconds=index),
        )
        for index, content in enumerate(contents)
    )
    return SessionSnapshot(session_key=key, messages=messages)


class NoCancel:
    """从不取消的 `CancelSignal`。"""

    requested = False

    def raise_if_requested(self) -> None:
        return None


class Cancelled:
    """已经被请求取消的 `CancelSignal`。"""

    requested = True

    def raise_if_requested(self) -> None:
        from karyvia.contracts import ErrorCode, KaryviaError

        raise KaryviaError(ErrorCode.CANCELLED_BY_USER, "已请求取消。")
