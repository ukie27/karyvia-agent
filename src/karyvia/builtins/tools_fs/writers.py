"""写工具：`fs.write` 与 `fs.edit`。

职责：两个工具的 `ToolSpec` 与 `ToolHandler` 实现，以及本包**唯一**的落盘路径。
不负责：路径判定（`paths.py`）、解码与截断（`content.py`）、注册（`registration.py`）。

**本包只有这个模块会写盘**，拆出来正是为了让这件事在文件边界上就看得见：`readers.py`
与 `search.py` 里出现一个 `os.replace` 会当场显眼。

**原子写 + 副作用如实标注**。写入走「同目录临时文件 → `fsync` → `os.replace`」，
因此：

- `os.replace` **之前**的任何失败，目标文件一个字节都没变 → `side_effect=NONE`。
  这就是 `base.FsTool` 把折出来的失败一律标成 `NONE` 的依据。
- `os.replace` **之后**没有可失败的步骤 → 成功即 `OCCURRED`。

没有第三种情况，所以本包不会产出 `SideEffect.UNKNOWN`——那个取值留给真的说不清的场合
。

**换行不翻译**：内容以 UTF-8 编码后按二进制写下去，`\\n` 就是 `\\n`。文本模式在 Windows
上会把它变成 `\\r\\n`，那样同一次工具调用在两个平台产出的文件字节就不同了。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Final

from karyvia.contracts import (
    CancelSignal,
    Concurrency,
    ErrorCode,
    KaryviaError,
    RiskLevel,
    SideEffect,
    ToolInvocation,
    ToolResult,
    ToolSpec,
    TrustLevel,
)

from .base import FsTool, optional_bool, reject_unknown_arguments, require_str
from .content import decode_text, looks_binary

__all__ = ["EDIT_SPEC", "WRITE_SPEC", "EditTool", "WriteTool"]

_WRITE_ARGUMENTS: Final = ("path", "content")
_EDIT_ARGUMENTS: Final = ("path", "old_text", "new_text", "replace_all")

#: 临时文件后缀。落在同一目录以保证 `os.replace` 不跨设备（与 `session_jsonl` 同一做法）。
_TEMP_SUFFIX: Final = ".karyvia-tmp"

WRITE_SPEC: Final = ToolSpec(
    name="fs.write",
    description=(
        "把内容整份写入 workspace 内的一个文件，已存在则覆盖，父目录自动创建。"
        "写入是原子的：要么整份生效，要么原文件不变。需要局部修改请用 fs.edit。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "workspace 内的文件路径。"},
            "content": {"type": "string", "description": "文件的完整新内容。"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    },
    read_only=False,
    # 覆盖既有文件是不可撤销的内容丢失，而 `DESTRUCTIVE` 正是确认策略要拦的那一档
    # 。「它只写一个文件」不构成把它降成 MUTATING 的理由。
    risk=RiskLevel.DESTRUCTIVE,
    # 两次写入同一个文件的结果取决于顺序，而 turn 内的并行调度不保证顺序。串行执行是唯一能让「模型看到的结果」可解释的选择。
    concurrency=Concurrency.EXCLUSIVE,
)

EDIT_SPEC: Final = ToolSpec(
    name="fs.edit",
    description=(
        "把 workspace 内某个文件里的一段精确文本替换成另一段。"
        "old_text 必须唯一命中，否则不做任何修改并报错；replace_all=true 时替换全部命中。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "workspace 内的文件路径。"},
            "old_text": {"type": "string", "description": "要被替换的原文，需精确匹配。"},
            "new_text": {"type": "string", "description": "替换后的新文本，可以为空串。"},
            "replace_all": {
                "type": "boolean",
                "default": False,
                "description": "命中多处时是否全部替换。默认 false，多处命中即报错。",
            },
        },
        "required": ["path", "old_text", "new_text"],
        "additionalProperties": False,
    },
    read_only=False,
    risk=RiskLevel.DESTRUCTIVE,
    concurrency=Concurrency.EXCLUSIVE,
)


class _WritingTool(FsTool):
    """两个写工具的公共部分：`OSError` 折成写失败，成功即 `OCCURRED`。"""

    __slots__ = ()

    os_error_code = ErrorCode.PERSISTENCE_WRITE_FAILED
    side_effect = SideEffect.OCCURRED

    def _atomic_write(self, target: Path, text: str) -> int:
        """把 `text` 原子地写进 `target`，返回写入字节数。

        临时文件与目标同目录、同前缀，因此 `os.replace` 不跨设备。中途失败一律清掉临时
        文件——留下一个 `.karyvia-tmp` 残骸会让下一次 `fs.list` 把它当成用户的文件列出来。
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = text.encode("utf-8")
        temp = target.with_name(target.name + _TEMP_SUFFIX)
        try:
            with open(temp, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
        except BaseException:
            # `BaseException` 而不是 `Exception`：取消也要清残骸。清理本身失败则忽略——
            # 那时真正该报告的是原来那个错误。
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return len(payload)

    def _read_existing(self, target: Path) -> str:
        """读出既有内容，供 `fs.edit` 做替换。"""
        if target.is_dir():
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                "这是一个目录，不能当文件编辑。",
                detail={"path": self._guard.relative(target)},
            )
        if not target.exists():
            raise KaryviaError(
                ErrorCode.PERSISTENCE_READ_FAILED,
                "文件不存在。",
                detail={"path": self._guard.relative(target)},
            )
        with open(target, "rb") as handle:
            data = handle.read(self._settings.max_read_bytes + 1)
        if len(data) > self._settings.max_read_bytes:
            raise KaryviaError(
                ErrorCode.INPUT_TOO_LARGE,
                "文件超过可编辑的大小上限——整份读进内存再写回会有丢内容的风险。",
                detail={
                    "path": self._guard.relative(target),
                    "limit": self._settings.max_read_bytes,
                },
            )
        if looks_binary(data):
            raise KaryviaError(
                ErrorCode.INPUT_UNSUPPORTED_MEDIA,
                "这是一个二进制文件，本工具只编辑文本。",
                detail={"path": self._guard.relative(target)},
            )
        text, lossy = decode_text(data)
        if lossy:
            # 有损解码后写回去，等于把那些 `�` 变成文件的真实内容——一次编辑顺手毁掉
            # 原本还能被别的工具正确读出的字节。读可以将就，写不行。
            raise KaryviaError(
                ErrorCode.INPUT_UNSUPPORTED_MEDIA,
                "文件不是合法 UTF-8，编辑它会丢失原有字节。",
                detail={"path": self._guard.relative(target)},
            )
        return text


class WriteTool(_WritingTool):
    """`fs.write`：整份覆盖写入。"""

    __slots__ = ()

    async def run(
        self, invocation: ToolInvocation, cancel: CancelSignal, started: float
    ) -> ToolResult:
        arguments = invocation.call.arguments
        reject_unknown_arguments(arguments, _WRITE_ARGUMENTS)
        content = arguments.get("content")
        if not isinstance(content, str):
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                "缺少必填参数或类型不对（应为字符串）。",
                detail={"argument": "content", "actual_type": type(content).__name__},
            )
        target = self._guard.resolve(require_str(arguments, "path"))
        self._guard.ensure_writable(target)

        cancel.raise_if_requested()
        existed = target.exists()
        written = await asyncio.to_thread(self._atomic_write, target, content)
        display = self._guard.relative(target)
        return self.done(
            invocation,
            f"已{'覆盖' if existed else '创建'} {display}（{written} 字节）。",
            started=started,
            data={"path": display, "bytes": written, "overwritten": existed},
            # 回执是本工具自己的话（唯一的外部成分是那个相对路径，它来自模型自己的入参），
            # 因此不包不可信数据块——四个读类工具走默认的 `UNTRUSTED`。
            trust=TrustLevel.SYSTEM,
        )


class EditTool(_WritingTool):
    """`fs.edit`：精确文本替换。命中数不合预期时**不做任何修改**。"""

    __slots__ = ()

    async def run(
        self, invocation: ToolInvocation, cancel: CancelSignal, started: float
    ) -> ToolResult:
        arguments = invocation.call.arguments
        reject_unknown_arguments(arguments, _EDIT_ARGUMENTS)
        old_text = require_str(arguments, "old_text")
        new_text = arguments.get("new_text")
        if not isinstance(new_text, str):
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                "缺少必填参数或类型不对（应为字符串，可以是空串）。",
                detail={"argument": "new_text", "actual_type": type(new_text).__name__},
            )
        replace_all = optional_bool(arguments, "replace_all", False)
        target = self._guard.resolve(require_str(arguments, "path"))
        self._guard.ensure_writable(target)

        cancel.raise_if_requested()
        current = await asyncio.to_thread(self._read_existing, target)
        display = self._guard.relative(target)
        hits = current.count(old_text)
        if hits == 0:
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                "没有找到要替换的文本——请先用 fs.read 确认原文。",
                detail={"path": display},
            )
        if hits > 1 and not replace_all:
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                "要替换的文本命中多处，请补足上下文使其唯一，或显式传 replace_all。",
                detail={"path": display, "occurrences": hits},
            )

        updated = current.replace(old_text, new_text, -1 if replace_all else 1)
        written = await asyncio.to_thread(self._atomic_write, target, updated)
        return self.done(
            invocation,
            f"已修改 {display}（替换 {hits if replace_all else 1} 处，{written} 字节）。",
            started=started,
            data={
                "path": display,
                "bytes": written,
                "replacements": hits if replace_all else 1,
            },
            trust=TrustLevel.SYSTEM,
        )
