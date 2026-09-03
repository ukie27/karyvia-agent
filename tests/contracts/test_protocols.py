"""能力接口测试（`D04`，技术方案 §5.1、需求 `SDK-001`、`NFR-104`）。

三件事：每个 Protocol 有一个最小 Fake 通过结构检查；方法数快照锁住公开表面
（`NFR-104`：新增接口必须显式改快照，等于强制走评审）；`protocols.py` 里没有实现。
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

from nucleamind.contracts import (
    CancelReason,
    CancelSignal,
    Channel,
    ChunkKind,
    CliEntry,
    CommandHandler,
    CommandInvocation,
    CommandResult,
    CommandSpec,
    CompactionModel,
    CompactionRequest,
    CompactionResult,
    ContextCompactor,
    ContextFragment,
    ContextProvider,
    Correlation,
    Disposition,
    FragmentScope,
    HookContext,
    HookHandler,
    HookOutcome,
    InboundMessage,
    InstanceView,
    MemoryProvider,
    ModelChunk,
    ModelInfo,
    ModelMessage,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    OutboundMessage,
    SessionKey,
    SessionMessage,
    SessionSnapshot,
    SessionStore,
    StopReason,
    ToolHandler,
    ToolInvocation,
    ToolResult,
    TurnCompactionRequest,
    TurnCompactionResult,
    TurnContextCompactor,
    TurnControl,
    TurnId,
)
from nucleamind.contracts.tool import SideEffect

#: 公开表面快照：Protocol -> 成员名集合。新增或删除方法必须同步改这里（`NFR-104`）。
#: `CancelSignal` / `InstanceView` / `TurnControl` 单列在 `SUPPORT_PROTOCOLS`：它们是
#: 支撑类型（取消语义、实例只读视图、turn 控制面），**不是可注册能力**——
#: `CAPABILITY_PROTOCOLS` 必须恒为 11，与 `CapabilityKind` 的 11 个取值一一对应。
CAPABILITY_PROTOCOLS: Final[dict[type, frozenset[str]]] = {
    ModelProvider: frozenset({"describe", "complete", "stream"}),
    ToolHandler: frozenset({"execute"}),
    ContextProvider: frozenset({"provide"}),
    ContextCompactor: frozenset({"compact"}),
    TurnContextCompactor: frozenset({"compact"}),
    SessionStore: frozenset({"load", "append", "compact", "delete", "list_keys"}),
    MemoryProvider: frozenset({"remember", "recall", "forget"}),
    Channel: frozenset({"channel_id", "start", "stop", "receive", "deliver"}),
    CommandHandler: frozenset({"handle"}),
    HookHandler: frozenset({"handle"}),
    CliEntry: frozenset({"run"}),
}

SUPPORT_PROTOCOLS: Final[dict[type, frozenset[str]]] = {
    CancelSignal: frozenset({"requested", "raise_if_requested"}),
    CompactionModel: frozenset({"info", "complete"}),
    InstanceView: frozenset(
        {"commands", "capabilities", "plugins", "config_document", "session_snapshot"}
    ),
    TurnControl: frozenset({"live_turns", "cancel_turn"}),
}

ALL_PROTOCOLS: Final[dict[type, frozenset[str]]] = {**CAPABILITY_PROTOCOLS, **SUPPORT_PROTOCOLS}

NOW = datetime(2026, 8, 11, tzinfo=UTC)


def _members(protocol: type) -> frozenset[str]:
    """Protocol 类体里声明的成员名。

    用 `vars()` 而不是 `__protocol_attrs__`：后者是 3.12 才有的实现细节，
    而本项目声明支持 3.11+。
    """
    return frozenset(name for name in vars(protocol) if not name.startswith("_"))


# --------------------------------------------------------------------------- 快照


def test_capability_protocol_count_is_eleven() -> None:
    """`SDK-001` 的扩展类型数；它与 `sdk.NucleaAPI` 的注册方法一一对应。

    `D04` 冻结了 8 个，`D05` 补上第 9 个 `CliEntry`，`D51` 新增第 10 个 Compactor。
    """
    assert len(CAPABILITY_PROTOCOLS) == 11


@pytest.mark.parametrize(
    ("protocol", "expected"),
    list(ALL_PROTOCOLS.items()),
    ids=[p.__name__ for p in ALL_PROTOCOLS],
)
def test_protocol_surface_matches_snapshot(protocol: type, expected: frozenset[str]) -> None:
    assert _members(protocol) == expected


def test_total_capability_members_is_twenty_three() -> None:
    """整体规模也进快照：接口数量受控是 `NFR-104` 的原话。"""
    assert sum(len(names) for names in CAPABILITY_PROTOCOLS.values()) == 23


@pytest.mark.parametrize(
    "protocol", list(ALL_PROTOCOLS), ids=[p.__name__ for p in ALL_PROTOCOLS]
)
def test_every_protocol_is_runtime_checkable(protocol: type) -> None:
    """`runtime_checkable` 只用于诊断输出，不用于控制流——但它必须在，否则诊断没法做。"""
    assert getattr(protocol, "_is_runtime_protocol", False)


def test_module_contains_no_implementation() -> None:
    """契约层不出现 IO：本模块的每个函数体只允许是 docstring 与 `...`。"""
    from nucleamind.contracts import protocols

    source = Path(inspect.getfile(protocols)).read_text(encoding="utf-8")
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = [stmt for stmt in node.body if not _is_docstring(stmt)]
        if len(body) != 1 or not _is_ellipsis(body[0]):
            offenders.append(node.name)
    assert not offenders, f"protocols.py 中出现了实现：{offenders}"


def _is_docstring(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(
        stmt.value.value, str
    )


def _is_ellipsis(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and (
        stmt.value.value is Ellipsis
    )


def test_every_method_documents_exceptions_and_cancellation() -> None:
    """`D04` 的要点：每个方法都要写明异常约定与取消语义。"""
    missing: list[str] = []
    for protocol in ALL_PROTOCOLS:
        for name in sorted(_members(protocol)):
            doc = inspect.getdoc(getattr(protocol, name)) or ""
            if "**异常约定**" not in doc or "**取消语义**" not in doc:
                missing.append(f"{protocol.__name__}.{name}")
    assert sorted(missing) == [
        # 三个豁免项：`CancelSignal` 自己就是取消原语，写「取消语义」是循环定义；
        # 两个只读属性没有异常可抛，约定写在所属 Protocol 的 docstring 里。
        "CancelSignal.raise_if_requested",
        "CancelSignal.requested",
        "Channel.channel_id",
        "CompactionModel.info",
    ]


# ------------------------------------------------------------------- 最小 Fake 实现


class FakeCancel:
    @property
    def requested(self) -> bool:
        return False

    def raise_if_requested(self) -> None:
        return None


class FakeModelProvider:
    def describe(self, model_id: str) -> ModelInfo:
        return ModelInfo(model_id=model_id, provider="fake")

    async def complete(self, request: ModelRequest, cancel: CancelSignal) -> ModelResponse:
        return ModelResponse(request.model_id, StopReason.END_TURN, content="ok")

    def stream(self, request: ModelRequest, cancel: CancelSignal) -> AsyncIterator[ModelChunk]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[ModelChunk]:
        yield ModelChunk(kind=ChunkKind.DONE, stop_reason=StopReason.END_TURN)


class FakeToolHandler:
    async def execute(self, invocation: ToolInvocation, cancel: CancelSignal) -> ToolResult:
        return ToolResult(
            invocation.call.call_id,
            ok=True,
            content="ok",
            truncated=False,
            side_effect=SideEffect.NONE,
        )


class FakeContextProvider:
    async def provide(
        self, snapshot: SessionSnapshot, correlation: Correlation, cancel: CancelSignal
    ) -> tuple[ContextFragment, ...]:
        return ()


class FakeContextCompactor:
    async def compact(
        self, request: CompactionRequest, cancel: CancelSignal
    ) -> CompactionResult | None:
        return None


class FakeCompactionModel:
    @property
    def info(self) -> ModelInfo:
        return ModelInfo(model_id="fake", provider="fake")

    async def complete(
        self,
        messages: Sequence[ModelMessage],
        cancel: CancelSignal,
        *,
        max_output_tokens: int | None = None,
    ) -> ModelResponse:
        del messages, cancel, max_output_tokens
        return ModelResponse("fake", StopReason.END_TURN, content="summary")


class FakeTurnContextCompactor:
    async def compact(
        self,
        request: TurnCompactionRequest,
        model: CompactionModel,
        cancel: CancelSignal,
    ) -> TurnCompactionResult:
        del request, model, cancel
        return TurnCompactionResult(1, "summary")


class FakeSessionStore:
    async def load(self, key: SessionKey) -> SessionSnapshot:
        return SessionSnapshot(session_key=key)

    async def append(self, key: SessionKey, messages: Sequence[SessionMessage]) -> None:
        return None

    async def compact(self, key: SessionKey, through: int, summary: SessionMessage) -> None:
        return None

    async def delete(self, key: SessionKey) -> bool:
        return False

    async def list_keys(self) -> tuple[SessionKey, ...]:
        return ()


class FakeMemoryProvider:
    async def remember(self, fragment: ContextFragment, cancel: CancelSignal) -> str:
        return "r-1"

    async def recall(
        self, query: str, *, scope: FragmentScope, limit: int, cancel: CancelSignal
    ) -> Mapping[str, ContextFragment]:
        return {}

    async def forget(self, record_id: str) -> bool:
        return False


class FakeChannel:
    @property
    def channel_id(self) -> str:
        return "fake"

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def receive(self) -> AsyncIterator[InboundMessage]:
        return self._receive()

    async def _receive(self) -> AsyncIterator[InboundMessage]:
        return
        yield  # pragma: no cover - 让本方法成为异步生成器

    async def deliver(self, message: OutboundMessage) -> None:
        return None


class FakeCommandHandler:
    async def handle(self, invocation: CommandInvocation, cancel: CancelSignal) -> CommandResult:
        return CommandResult(Disposition.COMMAND_HANDLED, content="ok")


class FakeHookHandler:
    async def handle(self, context: HookContext) -> HookOutcome | None:
        return None


class FakeCliEntry:
    async def run(self, argv: Sequence[str], cancel: CancelSignal) -> int:
        return 0


class FakeInstanceView:
    def commands(self) -> tuple[CommandSpec, ...]:
        return ()

    def capabilities(self) -> Mapping[str, object]:
        return {}

    def plugins(self) -> tuple[Mapping[str, object], ...]:
        return ()

    def config_document(self) -> Mapping[str, object]:
        return {}

    async def session_snapshot(self, key: SessionKey) -> SessionSnapshot:
        return SessionSnapshot(session_key=key)


class FakeTurnControl:
    def live_turns(self) -> tuple[TurnId, ...]:
        return ()

    def cancel_turn(self, turn_id: TurnId, reason: CancelReason = CancelReason.USER) -> bool:
        return False


FAKES: Final[list[tuple[type, object]]] = [
    (CancelSignal, FakeCancel()),
    (ModelProvider, FakeModelProvider()),
    (ToolHandler, FakeToolHandler()),
    (ContextProvider, FakeContextProvider()),
    (ContextCompactor, FakeContextCompactor()),
    (CompactionModel, FakeCompactionModel()),
    (TurnContextCompactor, FakeTurnContextCompactor()),
    (SessionStore, FakeSessionStore()),
    (MemoryProvider, FakeMemoryProvider()),
    (Channel, FakeChannel()),
    (CommandHandler, FakeCommandHandler()),
    (HookHandler, FakeHookHandler()),
    (CliEntry, FakeCliEntry()),
    (InstanceView, FakeInstanceView()),
    (TurnControl, FakeTurnControl()),
]


@pytest.mark.parametrize(
    ("protocol", "fake"), FAKES, ids=[protocol.__name__ for protocol, _ in FAKES]
)
def test_minimal_fake_satisfies_the_protocol(protocol: type, fake: object) -> None:
    """结构化子类型：Fake 没有继承任何宿主基类，照样满足接口（`PLG-002`）。"""
    assert isinstance(fake, protocol)
    assert not isinstance(object(), protocol)


def test_protocols_are_not_inherited_by_the_fakes() -> None:
    for protocol, fake in FAKES:
        assert protocol not in type(fake).__mro__
