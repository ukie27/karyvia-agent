"""默认 `file.send` 插件的契约、文件边界与注册验收。"""

from __future__ import annotations

import inspect

from karyvia.builtins.registry import BUILTIN_MANIFESTS, TOOLS_FILE
from karyvia.builtins.tools_file import (
    CONFIG_MAX_FILE_BYTES_KEY,
    FILE_SEND_SPEC,
    TOOL_NAME,
    FileSendTool,
    resolve_max_file_bytes,
    setup,
)
from karyvia.contracts import (
    AttachmentSource,
    ErrorCode,
    JsonValue,
    KaryviaError,
    RiskLevel,
    ToolCall,
    ToolHandler,
    ToolInvocation,
    ToolSpec,
)
from karyvia.sdk import FileAccess
from karyvia.sdk.testing import (
    FakePluginContext,
    ManualCancel,
    ToolContract,
    make_correlation,
)


class MemoryFiles:
    """工具边界替身；真实 workspace 守卫由 `tests/runtime/test_access.py` 覆盖。"""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    async def read_bytes(self, path: str) -> bytes:
        if ".." in path.replace("\\", "/").split("/"):
            raise KaryviaError(ErrorCode.PERMISSION_PATH_OUTSIDE_WORKSPACE, "路径越界。")
        if path not in self.files:
            raise KaryviaError(ErrorCode.PERSISTENCE_READ_FAILED, "读取失败。")
        return self.files[path]

    async def read_text(self, path: str) -> str:
        return (await self.read_bytes(path)).decode()

    async def write_text(self, path: str, content: str) -> None:
        self.files[path] = content.encode()

    async def write_bytes(self, path: str, data: bytes) -> None:
        self.files[path] = data

    async def list_dir(self, path: str) -> tuple[str, ...]:
        del path
        return tuple(sorted(self.files))


class FileContext(FakePluginContext):
    """只覆盖本工具真实需要的 workspace 文件服务。"""

    def __init__(self, files: dict[str, bytes], *, config: dict[str, JsonValue] | None = None) -> None:
        super().__init__("tools-file", config=config)
        self._files = MemoryFiles(files)

    @property
    def fs(self) -> FileAccess:
        return self._files


def invocation(**arguments: JsonValue) -> ToolInvocation:
    return ToolInvocation(
        call=ToolCall(call_id="call-1", name=TOOL_NAME, arguments=arguments),
        correlation=make_correlation(),
        timeout_ms=5_000,
    )


def make_tool(files: dict[str, bytes], *, limit: int = 1024) -> FileSendTool:
    return FileSendTool(FileContext(files), max_file_bytes=limit)


class TestFileSendContract(ToolContract):
    def make_tool(self) -> tuple[ToolSpec, ToolHandler]:
        return FILE_SEND_SPEC, make_tool({"report.txt": b"hello"})

    def valid_arguments(self) -> dict[str, JsonValue]:
        return {"path": "report.txt"}

    def invalid_arguments(self) -> dict[str, JsonValue]:
        return {"path": 3}


async def test_success_returns_a_workspace_attachment() -> None:
    result = await make_tool({"report.pdf": b"%PDF"}).execute(
        invocation(path="report.pdf"), ManualCancel()
    )

    assert result.ok
    assert len(result.attachments) == 1
    attachment = result.attachments[0]
    assert attachment.source is AttachmentSource.WORKSPACE
    assert attachment.locator == "report.pdf"
    assert attachment.filename == "report.pdf"
    assert attachment.media_type == "application/pdf"
    assert attachment.size_bytes == 4


async def test_missing_file_is_a_failed_result() -> None:
    result = await make_tool({}).execute(
        invocation(path="missing.txt"), ManualCancel()
    )
    assert not result.ok
    assert result.error is not None
    assert result.error.code is ErrorCode.PERSISTENCE_READ_FAILED
    assert result.attachments == ()


async def test_workspace_escape_is_rejected() -> None:
    result = await make_tool({}).execute(
        invocation(path="../outside.txt"), ManualCancel()
    )
    assert not result.ok
    assert result.error is not None
    assert result.error.code is ErrorCode.PERMISSION_PATH_OUTSIDE_WORKSPACE


async def test_file_over_limit_is_not_attached() -> None:
    result = await make_tool({"large.bin": b"12345"}, limit=4).execute(
        invocation(path="large.bin"), ManualCancel()
    )
    assert not result.ok
    assert result.error is not None
    assert result.error.code is ErrorCode.INPUT_TOO_LARGE
    assert result.attachments == ()


def test_settings_reject_non_positive_limit() -> None:
    context = FakePluginContext("tools-file", config={CONFIG_MAX_FILE_BYTES_KEY: 0})
    try:
        resolve_max_file_bytes(context)
    except Exception as error:
        assert getattr(error, "code", None) is ErrorCode.CONFIG_INVALID
    else:
        raise AssertionError("非法大小上限必须在加载期被拒绝")


def test_manifest_and_setup_use_the_normal_plugin_path() -> None:
    registered: list[tuple[ToolSpec, ToolHandler]] = []

    class Api:
        ctx = FakePluginContext("tools-file")

        def register_tool(self, spec: ToolSpec, handler: ToolHandler) -> None:
            registered.append((spec, handler))

    setup(Api())  # type: ignore[arg-type]
    assert TOOLS_FILE in BUILTIN_MANIFESTS
    assert TOOLS_FILE.id == "tools-file"
    assert [decl.name for decl in TOOLS_FILE.capabilities] == [TOOL_NAME]
    assert [spec.name for spec, _ in registered] == [TOOL_NAME]
    assert inspect.signature(FileSendTool.execute) == inspect.signature(ToolHandler.execute)


def test_spec_is_safe_and_read_only() -> None:
    assert FILE_SEND_SPEC.read_only
    assert FILE_SEND_SPEC.risk is RiskLevel.SAFE
