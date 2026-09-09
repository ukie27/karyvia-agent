"""`fs.grep`：在 workspace 内按正则搜索文本。

职责：`fs.grep` 的 `ToolSpec` 与 `ToolHandler` 实现。
不负责：路径判定（`paths.py`）、解码（`content.py`）、注册（`registration.py`）。

**正则由模型给，因此 ReDoS 有面**。三道约束合起来把它压成有界代价：pattern 长度封顶、
每处理一个文件检查一次取消、匹配数封顶。这不是「挡住了 ReDoS」——一个足够病态的
pattern 配一个足够长的行仍然能卡住一次调用，但那次调用会被 Kernel 的
`tool_timeout_ms` 收走，而不是拖垮整个实例。

**二进制文件跳过而不是报错**：一次 grep 扫过几百个文件，其中有 PNG 是常态。让整次搜索
因此失败，模型只能改成一个个文件读——那比返回一份「跳过了 N 个二进制文件」的结果差得多。
"""

from __future__ import annotations

import asyncio
import re
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
)

from .base import FsTool, optional_bool, optional_str, reject_unknown_arguments, require_str
from .content import decode_text, looks_binary

__all__ = ["GREP_SPEC", "MAX_PATTERN_LENGTH", "GrepTool"]

_GREP_ARGUMENTS: Final = ("pattern", "path", "glob", "case_sensitive")

#: 正则长度上限。够写任何正经查询，又挡住了把一个几 KB 的病态 pattern 塞进来的玩法。
MAX_PATTERN_LENGTH: Final = 512

#: 单行参与匹配的最大字符数。压缩包被误判成文本时，一行可能有几 MB。
_MAX_LINE_LENGTH: Final = 2000

#: 一条匹配的渲染形态：相对路径 + 行号 + 行内容。
_MATCH_LINE: Final = "{path}:{line}: {text}"

GREP_SPEC: Final = ToolSpec(
    name="fs.grep",
    description=(
        "在 workspace 或已启用 @skills/<name>/... 挂载中按 Python 正则搜索文本文件，"
        "返回 路径:行号: 内容。"
        "可用 glob 限定文件名；二进制文件自动跳过；匹配过多时截断并标注。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "maxLength": MAX_PATTERN_LENGTH,
                "description": "Python 正则表达式。",
            },
            "path": {
                "type": "string",
                "default": ".",
                "description": "搜索起点；可用 workspace 或 @skills/<name>/... 路径。",
            },
            "glob": {
                "type": "string",
                "description": "文件名 glob 过滤，如 *.py。省略表示不过滤。",
            },
            "case_sensitive": {
                "type": "boolean",
                "default": True,
                "description": "是否区分大小写。",
            },
        },
        "required": ["pattern"],
        "additionalProperties": False,
    },
    read_only=True,
    risk=RiskLevel.SAFE,
    concurrency=Concurrency.PARALLEL,
)


class GrepTool(FsTool):
    """`fs.grep`：按正则搜索，逐文件检查取消。"""

    __slots__ = ()

    async def run(
        self, invocation: ToolInvocation, cancel: CancelSignal, started: float
    ) -> ToolResult:
        arguments = invocation.call.arguments
        reject_unknown_arguments(arguments, _GREP_ARGUMENTS)
        pattern = _compile(
            require_str(arguments, "pattern"),
            case_sensitive=optional_bool(arguments, "case_sensitive", True),
        )
        name_filter = optional_str(arguments, "glob", "")
        target = self._guard.resolve(optional_str(arguments, "path", "."))

        matches: list[str] = []
        scanned = 0
        skipped = 0
        more = False
        for candidate in sorted(self._candidates(target, name_filter)):
            # 取消检查在**每个文件之前**：一次 grep 可能扫几百个文件，只在入口检查一次
            # 等于取消要等到整次搜索结束（宽限期就白给了）。
            cancel.raise_if_requested()
            found, readable = await asyncio.to_thread(self._search, candidate, pattern)
            if not readable:
                skipped += 1
                continue
            scanned += 1
            room = self._settings.max_matches - len(matches)
            matches.extend(found[:room])
            if len(found) > room:
                more = True
                break

        return self.done(
            invocation,
            "\n".join(matches) if matches else "(无匹配)",
            started=started,
            data={
                "matches": len(matches),
                "files_scanned": scanned,
                "files_skipped": skipped,
                "root": self._guard.relative(target),
            },
            truncated=more,
        )

    def _candidates(self, target: Path, name_filter: str) -> Iterator[Path]:
        """产出待搜索的文件。目标是文件时就搜它自己。

        与 `fs.list` 一样，每个条目重新过一次 `guard.resolve()`：指向根外的符号链接直接
        跳过，一条越界链接不该让整次搜索失败，也不该被搜进结果。
        """
        if target.is_file():
            yield target
            return
        if not target.is_dir():
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                "搜索起点不存在。",
                detail={"path": self._guard.relative(target)},
            )
        stack = [target]
        while stack:
            for entry in stack.pop().iterdir():
                try:
                    resolved = self._guard.resolve(str(entry))
                except KaryviaError:
                    continue
                if resolved.is_dir():
                    stack.append(resolved)
                elif not name_filter or resolved.match(name_filter):
                    yield resolved

    def _search(self, path: Path, pattern: re.Pattern[str]) -> tuple[list[str], bool]:
        """搜一个文件，返回 `(匹配行, 是否可读为文本)`。

        读不动（权限、被删掉、坏块）与二进制在这里是同一种答案——`readable=False`。
        两者的差别对模型没有可操作性，而把 `OSError` 抛上去会让一个不可读的文件毁掉
        整次搜索。
        """
        try:
            with open(path, "rb") as handle:
                data = handle.read(self._settings.max_read_bytes)
        except OSError:
            return [], False
        if looks_binary(data):
            return [], False
        text, _ = decode_text(data)
        display = self._guard.relative(path)
        return [
            _MATCH_LINE.format(path=display, line=number, text=line[:_MAX_LINE_LENGTH])
            for number, line in enumerate(text.split("\n"), start=1)
            if pattern.search(line[:_MAX_LINE_LENGTH])
        ], True


def _compile(pattern: str, *, case_sensitive: bool) -> re.Pattern[str]:
    """编译正则。写坏的正则是**结果**而不是异常——模型据此改写重试即可。"""
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise KaryviaError(
            ErrorCode.INPUT_TOO_LARGE,
            "正则表达式过长。",
            detail={"length": len(pattern), "limit": MAX_PATTERN_LENGTH},
        )
    try:
        return re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
    except re.error as error:
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            "正则表达式非法。",
            # `error.msg` 是 Python 自己的诊断（如 "unbalanced parenthesis"），不含用户
            # 数据；`pattern` 本身是模型刚写的，回给它才谈得上改写重试。
            detail={"reason": error.msg, "pattern": pattern},
        ) from None
