"""Agent 请求发送 workspace 文件的工具。

职责：校验参数、确认文件可读且大小有界，并以 `ToolResult.attachments` 表达投递意图。
不负责：调用 Channel、主动选择会话、上传文件或保存二进制；这些分别属于 Kernel 出站链、
Channel 与现有 workspace 文件服务。

插件是完全可信的同进程代码。这里仍使用 `ctx.fs`，是为了复用项目统一的 workspace 相对路径
语义与可测试错误，而不是执行插件权限检查。
"""

from __future__ import annotations

import mimetypes
import time
from collections.abc import Collection, Mapping
from typing import Final

from karyvia.contracts import (
    AttachmentRef,
    AttachmentSource,
    CancelSignal,
    ErrorCode,
    JsonValue,
    KaryviaError,
    RiskLevel,
    SideEffect,
    ToolInvocation,
    ToolResult,
    ToolSpec,
    TrustLevel,
    validate_identifier,
)
from karyvia.sdk import PluginContext

__all__ = ["FILE_SEND_SPEC", "TOOL_NAME", "FileSendTool"]

TOOL_NAME: Final = "file.send"
_ARGUMENTS: Final[frozenset[str]] = frozenset({"path", "filename", "media_type"})
_DEFAULT_MEDIA_TYPE: Final = "application/octet-stream"

FILE_SEND_SPEC: Final = ToolSpec(
    name=TOOL_NAME,
    description=(
        "把 workspace 中已经存在的文件附加到本轮最终回复。只负责选择文件，具体上传由当前"
        " Channel 完成。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "minLength": 1,
                "description": "相对于 workspace 的文件路径。",
            },
            "filename": {
                "type": "string",
                "minLength": 1,
                "description": "用户收到时显示的文件名；缺省使用 path 的最后一段。",
            },
            "media_type": {
                "type": "string",
                "minLength": 3,
                "description": "MIME 类型；缺省按扩展名推断。",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    read_only=True,
    risk=RiskLevel.SAFE,
)


def _malformed(message: str, **detail: object) -> KaryviaError:
    return KaryviaError(ErrorCode.INPUT_MALFORMED, message, detail=detail)


def _invalid_filename() -> KaryviaError:
    return _malformed("filename 只能是文件名，不能包含路径分隔符。")


def _check_size(size_bytes: int, limit: int) -> None:
    if size_bytes > limit:
        raise KaryviaError(
            ErrorCode.INPUT_TOO_LARGE,
            "文件超过允许发送的大小。",
            detail={"size_bytes": size_bytes, "limit": limit},
        )


def _reject_unknown(arguments: Mapping[str, JsonValue], allowed: Collection[str]) -> None:
    unknown = sorted(set(arguments) - set(allowed))
    if unknown:
        raise _malformed("出现了未知参数。", unknown=unknown, allowed=sorted(allowed))


def _required(arguments: Mapping[str, JsonValue], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value:
        raise _malformed("缺少必填参数或类型不对。", argument=key)
    return value


def _optional(arguments: Mapping[str, JsonValue], key: str) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise _malformed("可选参数必须是非空字符串。", argument=key)
    return value


def _filename(path: str, override: str | None) -> str:
    name = override or path.replace("\\", "/").rsplit("/", 1)[-1]
    validate_identifier("file.send.filename", name, max_length=255)
    if "/" in name or "\\" in name:
        raise _invalid_filename()
    return name


def _media_type(path: str, override: str | None) -> str:
    guessed, _ = mimetypes.guess_type(path, strict=False)
    return override or guessed or _DEFAULT_MEDIA_TYPE


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


class FileSendTool:
    """`file.send` 实现。保存 Context 是为了在调用期取得 workspace 文件服务。"""

    __slots__ = ("_ctx", "_max_file_bytes")

    def __init__(self, ctx: PluginContext, *, max_file_bytes: int) -> None:
        self._ctx = ctx
        self._max_file_bytes = max_file_bytes

    async def execute(self, invocation: ToolInvocation, cancel: CancelSignal) -> ToolResult:
        """确认文件可发送并返回附件；失败折成 `ToolResult`，不接触任何 Channel。"""
        started = time.perf_counter()
        call_id = invocation.call.call_id
        try:
            cancel.raise_if_requested()
            arguments = invocation.call.arguments
            _reject_unknown(arguments, _ARGUMENTS)
            path = _required(arguments, "path")
            filename = _filename(path, _optional(arguments, "filename"))
            media_type = _media_type(path, _optional(arguments, "media_type"))
            data = await self._ctx.fs.read_bytes(path)
            cancel.raise_if_requested()
            _check_size(len(data), self._max_file_bytes)
            attachment = AttachmentRef(
                source=AttachmentSource.WORKSPACE,
                locator=path,
                media_type=media_type,
                size_bytes=len(data),
                filename=filename,
            )
            return ToolResult(
                call_id=call_id,
                ok=True,
                content=f"已把 {filename} 加入本轮回复附件。",
                truncated=False,
                side_effect=SideEffect.NONE,
                data={"path": path, "size_bytes": len(data), "media_type": media_type},
                attachments=(attachment,),
                duration_ms=_elapsed_ms(started),
            )
        except KaryviaError as error:
            return ToolResult(
                call_id=call_id,
                ok=False,
                content=error.user_message,
                truncated=False,
                side_effect=SideEffect.NONE,
                error=error,
                duration_ms=_elapsed_ms(started),
                trust=TrustLevel.SYSTEM,
            )
        except Exception as exc:  # noqa: BLE001 - ToolHandler 约定不抛
            error = KaryviaError(
                ErrorCode.PERSISTENCE_READ_FAILED,
                "无法读取要发送的文件。",
                detail={"cause": type(exc).__name__},
            )
            return ToolResult(
                call_id=call_id,
                ok=False,
                content=error.user_message,
                truncated=False,
                side_effect=SideEffect.NONE,
                error=error,
                duration_ms=_elapsed_ms(started),
                trust=TrustLevel.SYSTEM,
            )
