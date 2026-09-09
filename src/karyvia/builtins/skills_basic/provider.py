"""Agent Skills 元数据发现与 Context Provider。

只读取每个直接子目录的 `SKILL.md` frontmatter；正文、references、scripts 与 assets 均由
模型在需要时通过已有工具访问。目录在构造时形成不可变快照，实例运行期间不会热重载。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from karyvia.contracts import (
    CancelSignal,
    ContextFragment,
    Correlation,
    ErrorCode,
    FragmentKind,
    FragmentScope,
    KaryviaError,
    SessionSnapshot,
    TrustLevel,
)
from karyvia.sdk import KaryviaAPI

from .settings import MAX_DESCRIPTION_LENGTH, MAX_NAME_LENGTH, read_roots

__all__ = ["CAPABILITY_NAME", "SkillCatalogProvider", "setup"]

CAPABILITY_NAME: Final = "skills"
_SOURCE: Final = "builtin:skills-basic"
_NAME: Final = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MAX_FRONTMATTER_BYTES: Final = 16 * 1024


@dataclass(frozen=True, slots=True)
class _Skill:
    name: str
    description: str


class SkillCatalogProvider:
    """启动时冻结的 Skill 元数据目录。"""

    __slots__ = ("_skills",)

    def __init__(self, roots: tuple[Path, ...]) -> None:
        self._skills = _discover(roots)

    async def provide(
        self,
        snapshot: SessionSnapshot,
        correlation: Correlation,
        cancel: CancelSignal,
    ) -> tuple[ContextFragment, ...]:
        del snapshot, correlation, cancel
        if not self._skills:
            return ()
        lines = [
            "以下是可按需使用的 Skill。任务匹配 description 时，先用 fs.read 读取其 location；",
            "完整 SKILL.md 是已启用的操作指令。其余 references/assets 按需读取，scripts 用 shell.exec 执行。",
            "<available-skills>",
        ]
        for skill in self._skills:
            lines.extend(
                (
                    "  <skill>",
                    f"    <name>{html.escape(skill.name)}</name>",
                    f"    <description>{html.escape(skill.description)}</description>",
                    f"    <location>@skills/{html.escape(skill.name)}/SKILL.md</location>",
                    "  </skill>",
                )
            )
        lines.append("</available-skills>")
        content = "\n".join(lines)
        return (
            ContextFragment(
                source=_SOURCE,
                kind=FragmentKind.SKILL,
                content=content,
                priority=50,
                estimated_tokens=max(1, len(content) // 4),
                scope=FragmentScope.AGENT,
                trust=TrustLevel.OPERATOR,
            ),
        )


def _discover(roots: tuple[Path, ...]) -> tuple[_Skill, ...]:
    found: dict[str, _Skill] = {}
    origins: dict[str, str] = {}
    for container in roots:
        resolved_container = container.resolve()
        if not resolved_container.is_dir():
            continue
        try:
            entries = sorted(resolved_container.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise _invalid(
                resolved_container, f"无法扫描 Skill 目录：{type(exc).__name__}。"
            ) from exc
        for directory in entries:
            resolved_directory = directory.resolve()
            entry = directory / "SKILL.md"
            if (
                not resolved_directory.is_relative_to(resolved_container)
                or not directory.is_dir()
                or not entry.is_file()
            ):
                continue
            skill = _read_frontmatter(entry, directory.name)
            if skill.name in found:
                raise KaryviaError(
                    ErrorCode.PLUGIN_REGISTRATION_CONFLICT,
                    "多个 Skill 使用了同一个名称。",
                    detail={"skill": skill.name, "sources": [origins[skill.name], str(entry)]},
                )
            found[skill.name] = skill
            origins[skill.name] = str(entry)
    return tuple(found[name] for name in sorted(found))


def _read_frontmatter(path: Path, directory_name: str) -> _Skill:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_MAX_FRONTMATTER_BYTES + 1)
    except OSError as exc:
        raise _invalid(path, f"无法读取 SKILL.md：{type(exc).__name__}。") from exc
    if len(raw) > _MAX_FRONTMATTER_BYTES and b"\n---" not in raw:
        raise _invalid(path, "SKILL.md frontmatter 超过大小上限。")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _invalid(path, "SKILL.md 必须使用 UTF-8 编码。") from exc
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise _invalid(path, "SKILL.md 缺少 YAML frontmatter。")
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        raise _invalid(path, "SKILL.md frontmatter 没有结束标记。") from None
    fields = _frontmatter_fields(lines[1:end])
    name = fields.get("name", "")
    description = fields.get("description", "")
    if name != directory_name or len(name) > MAX_NAME_LENGTH or not _NAME.fullmatch(name):
        raise _invalid(path, "Skill name 非法或与父目录名不一致。")
    if not description or len(description) > MAX_DESCRIPTION_LENGTH:
        raise _invalid(path, "Skill description 缺失或超过长度上限。")
    return _Skill(name, description)


def _frontmatter_fields(lines: list[str]) -> dict[str, str]:
    """读取所需标量；支持常见的 YAML `>` / `|` 多行 description。"""
    fields: dict[str, str] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue
        key, separator, value = line.partition(":")
        normalized_key = key.strip()
        scalar = value.strip()
        if separator and normalized_key in {"name", "description"}:
            if normalized_key == "description" and scalar in {">", ">-", ">+", "|", "|-", "|+"}:
                block: list[str] = []
                index += 1
                while index < len(lines) and (not lines[index].strip() or lines[index][0].isspace()):
                    block.append(lines[index].strip())
                    index += 1
                fields[normalized_key] = (
                    "\n".join(block).strip() if scalar.startswith("|") else " ".join(block).strip()
                )
                continue
            fields[normalized_key] = _scalar(scalar)
        index += 1
    return fields


def _scalar(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _invalid(path: Path, message: str) -> KaryviaError:
    return KaryviaError(
        ErrorCode.CONFIG_INVALID,
        message,
        detail={"file": str(path)},
    )


def setup(api: KaryviaAPI) -> None:
    api.register_context_provider(CAPABILITY_NAME, SkillCatalogProvider(read_roots(api.ctx.config)))
