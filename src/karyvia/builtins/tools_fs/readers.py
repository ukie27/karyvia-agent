"""只读工具：`fs.read` 与 `fs.list`。

职责：两个工具的 `ToolSpec` 与 `ToolHandler` 实现。
不负责：路径判定（`paths.py`）、解码与截断（`content.py`）、注册（`registration.py`）。

两个都是 `read_only=True` / `risk=SAFE` / `side_effect=NONE`——契约的
`ToolSpec.__post_init__` 会核对前两者一致，`ToolContract` 会核对第三者。

**输出里的路径一律是 workspace 相对路径**（`guard.relative()`）：两个平台给出同一个串
，顺带也不把宿主机的目录结构送进模型上下文。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Final

from karyvia.contracts import (
    CancelSignal,
    Concurrency,
    ErrorCode,
    KaryviaError,
    RiskLevel,
    ToolInvocation,
    ToolResult,
    ToolSpec,
    TrustLevel,
)

from .base import (
    FsTool,
    optional_bool,
    optional_int,
    optional_str,
    reject_unknown_arguments,
    require_str,
)
from .content import decode_text, looks_binary

__all__ = ["LIST_SPEC", "READ_SPEC", "ListTool", "ReadTool"]

_READ_ARGUMENTS: Final = ("path", "start_line", "max_lines")
_LIST_ARGUMENTS: Final = ("path", "recursive")

#: 目录条目的渲染形态。目录带尾随 `/`，文件带字节数——模型据此决定下一步读哪个。
_DIR_ENTRY: Final = "{path}/"
_FILE_ENTRY: Final = "{path}\t{size} bytes"

READ_SPEC: Final = ToolSpec(
    name="fs.read",
    description=(
        "读取 workspace 或已启用 @skills/<name>/... 挂载中的文本文件。"
        "可用 start_line / max_lines 读取片段。"
        "二进制文件会被拒绝而不是返回乱码；超长内容会被截断并标注。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "workspace 内路径，或已启用的 @skills/<name>/... 路径。",
            },
            "start_line": {
                "type": "integer",
                "minimum": 1,
                "default": 1,
                "description": "起始行号，从 1 开始。",
            },
            "max_lines": {
                "type": "integer",
                "minimum": 1,
                "description": "最多读取多少行。省略表示读到文件末尾（仍受长度上限约束）。",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    read_only=True,
    risk=RiskLevel.SAFE,
    concurrency=Concurrency.PARALLEL,
)

LIST_SPEC: Final = ToolSpec(
    name="fs.list",
    description=(
        "列出 workspace 或已启用 @skills/<name>/... 挂载中的目录。"
        "目录以 / 结尾，文件附字节数；"
        "结果按路径排序，条目过多时截断并标注。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "default": ".",
                "description": "workspace 内目录或 @skills/<name>/...；省略表示 workspace 根。",
            },
            "recursive": {
                "type": "boolean",
                "default": False,
                "description": "是否递归列出子目录。",
            },
        },
        "additionalProperties": False,
    },
    read_only=True,
    risk=RiskLevel.SAFE,
    concurrency=Concurrency.PARALLEL,
)


class ReadTool(FsTool):
    """`fs.read`：读一个文本文件，可选行区间。"""

    __slots__ = ()

    async def run(
        self, invocation: ToolInvocation, cancel: CancelSignal, started: float
    ) -> ToolResult:
        arguments = invocation.call.arguments
        reject_unknown_arguments(arguments, _READ_ARGUMENTS)
        start_line = optional_int(arguments, "start_line", 1, minimum=1)
        max_lines = optional_int(arguments, "max_lines", 0, minimum=1)
        target = self._guard.resolve(require_str(arguments, "path"))

        cancel.raise_if_requested()
        data, oversized = await asyncio.to_thread(self._read_bytes, target)
        if looks_binary(data):
            raise KaryviaError(
                ErrorCode.INPUT_UNSUPPORTED_MEDIA,
                "这是一个二进制文件，本工具只读文本。",
                detail={"path": self._guard.relative(target)},
            )
        text, lossy = decode_text(data)
        lines = text.split("\n")
        selected, end_line = _slice_lines(lines, start_line, max_lines)
        return self.done(
            invocation,
            "\n".join(selected),
            started=started,
            data={
                "path": self._guard.relative(target),
                "bytes": len(data),
                "start_line": start_line,
                "end_line": end_line,
                "total_lines": len(lines),
                # 有损解码是结果的一部分而不是日志里的一句话：模型看到 `�` 时得知道
                # 那是文件坏了，不是它读错了地方。
                "lossy": lossy,
            },
            truncated=oversized or end_line < len(lines) or start_line > 1,
            trust=(
                TrustLevel.SYSTEM
                if self._guard.is_skill_entry(target)
                else TrustLevel.UNTRUSTED
            ),
        )

    def _read_bytes(self, target: Path) -> tuple[bytes, bool]:
        """读取至多 `max_read_bytes` 字节，返回 `(字节, 是否被大小上限截断)`。

        目录被当成文件读会得到一个平台相关的 `OSError`（Linux 是 `IsADirectoryError`，
        Windows 是 `PermissionError`），因此先显式判一次—— 要的正是同参数
        同语义。
        """
        if target.is_dir():
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                "这是一个目录，请用 fs.list。",
                detail={"path": self._guard.relative(target)},
            )
        limit = self._settings.max_read_bytes
        with open(target, "rb") as handle:
            data = handle.read(limit + 1)
        return (data[:limit], True) if len(data) > limit else (data, False)


class ListTool(FsTool):
    """`fs.list`：列一个目录，可选递归。"""

    __slots__ = ()

    async def run(
        self, invocation: ToolInvocation, cancel: CancelSignal, started: float
    ) -> ToolResult:
        arguments = invocation.call.arguments
        reject_unknown_arguments(arguments, _LIST_ARGUMENTS)
        recursive = optional_bool(arguments, "recursive", False)
        target = self._guard.resolve(optional_str(arguments, "path", "."))

        cancel.raise_if_requested()
        entries, more = await asyncio.to_thread(self._collect, target, recursive)
        return self.done(
            invocation,
            "\n".join(entries) if entries else "(空目录)",
            started=started,
            data={
                "path": self._guard.relative(target),
                "entries": len(entries),
                "recursive": recursive,
            },
            truncated=more,
        )

    def _collect(self, target: Path, recursive: bool) -> tuple[list[str], bool]:
        """收集条目，返回 `(渲染好的行, 是否还有更多)`。

        排序在收集之后统一做：`os.scandir` 的顺序是文件系统给的，两个平台不一样，而
         要求同一个目录在哪里列出来都是同一份输出。
        """
        if not target.is_dir():
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                "这不是一个目录。",
                detail={"path": self._guard.relative(target)},
            )
        limit = self._settings.max_entries
        collected = sorted(self._walk(target, recursive), key=lambda item: item[0])
        rendered = [
            _DIR_ENTRY.format(path=name)
            if is_dir
            else _FILE_ENTRY.format(path=name, size=size)
            for name, is_dir, size in collected[:limit]
        ]
        return rendered, len(collected) > limit

    def _walk(self, target: Path, recursive: bool) -> Iterator[tuple[str, bool, int]]:
        """产出 `(相对路径, 是否目录, 字节数)`。

        **不跟随符号链接出界**：每个条目都重新过一次 `guard.resolve()`，指向根外的链接
        直接跳过而不是报错——一个目录里混进一条越界链接不该让整次列目录失败，但它也不该
        出现在给模型的清单里。
        """
        stack = [target]
        while stack:
            current = stack.pop()
            for entry in current.iterdir():
                try:
                    resolved = self._guard.resolve(str(entry))
                except KaryviaError:
                    continue
                is_dir = resolved.is_dir()
                if is_dir and recursive:
                    stack.append(resolved)
                size = 0 if is_dir else resolved.stat().st_size
                yield self._guard.relative(resolved), is_dir, size


def _slice_lines(lines: list[str], start_line: int, max_lines: int) -> tuple[list[str], int]:
    """按 1 起的行号取一段，返回 `(片段, 结束行号)`。

    起始行超出文件长度返回空片段而不是报错：那是「这个位置之后没有内容」，与「文件读不
    出来」是两回事，混成一个错误会让模型以为路径给错了。
    """
    begin = start_line - 1
    end = len(lines) if max_lines <= 0 else min(len(lines), begin + max_lines)
    if begin >= len(lines):
        return [], len(lines)
    return lines[begin:end], end
