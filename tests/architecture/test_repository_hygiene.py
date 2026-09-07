"""仓库文字卫生守卫。

职责：阻止开发阶段编号和已删除的阶段文档重新进入活跃源码与文档。
不负责：扫描只读参考仓库、虚拟环境、缓存、二进制文件或许可证归属信息。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ._common import REPO_ROOT

_SKIPPED_PARTS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest-tmp",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "references",
    }
)
_REQUIREMENT_PREFIXES = (
    "BAS",
    "CFG",
    "CMD",
    "CMP",
    "CTX",
    "DST",
    "EDG",
    "KER",
    "MEM",
    "MOD",
    "MSG",
    "NFR",
    "OBS",
    "PLG",
    "SDK",
    "SES",
    "TOL",
)
_STAGE_ID = re.compile(r"(?<![A-Za-z0-9])" + "D" + r"\d{2}(?!\d)")
_REQUIREMENT_ID = re.compile(
    r"\b(?:" + "|".join(_REQUIREMENT_PREFIXES) + r")-\d{3}\b"
)
_RETIRED_DOCS = (
    "requirements" + "-analysis.md",
    "technical" + "-design.md",
    "history" + ".md",
)


def _text_files(root: Path) -> list[Path]:
    paths: list[Path] = []
    for directory, names, files in os.walk(root):
        names[:] = sorted(name for name in names if name not in _SKIPPED_PARTS)
        paths.extend(
            Path(directory) / name
            for name in sorted(files)
            if name != "LICENSE"
        )
    return paths


def _violations(root: Path) -> list[str]:
    found: list[str] = []
    for path in _text_files(root):
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(content.splitlines(), start=1):
            if _STAGE_ID.search(line) or _REQUIREMENT_ID.search(line):
                found.append(f"{path.relative_to(root)}:{line_number}: {line.strip()}")
            if any(name in line for name in _RETIRED_DOCS):
                found.append(f"{path.relative_to(root)}:{line_number}: {line.strip()}")
    return found


def test_active_repository_has_no_development_trace_ids() -> None:
    assert not _violations(REPO_ROOT)


def test_injected_development_trace_is_rejected(tmp_path: Path) -> None:
    marker = "D" + "08"
    (tmp_path / "note.md").write_text(f"temporary milestone {marker}\n", encoding="utf-8")
    assert _violations(tmp_path)
