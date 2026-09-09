"""Skill 元数据发现、虚拟挂载与既有工具复用。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from karyvia.builtins.skills_basic import SkillCatalogProvider
from karyvia.builtins.tools_fs import ReadTool, WorkspaceGuard
from karyvia.builtins.tools_fs.settings import CONFIG_SKILL_ROOTS_KEY, CONFIG_WORKSPACE_KEY
from karyvia.builtins.tools_shell import ShellExecutor
from karyvia.builtins.tools_shell.paths import CwdGuard
from karyvia.builtins.tools_shell.settings import (
    CONFIG_SKILL_ROOTS_KEY as SHELL_SKILL_ROOTS,
)
from karyvia.builtins.tools_shell.settings import (
    CONFIG_WORKSPACE_KEY as SHELL_WORKSPACE,
)
from karyvia.contracts import (
    ErrorCode,
    KaryviaError,
    SessionKey,
    SessionSnapshot,
    ToolCall,
    ToolInvocation,
    TrustLevel,
)
from karyvia.sdk.testing import FakePluginContext, ManualCancel, make_correlation


def _skill(container: Path, name: str = "pdf-tools") -> Path:
    root = container / name
    (root / "scripts").mkdir(parents=True)
    (root / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Process PDF documents.\n---\n\n# Instructions\n",
        encoding="utf-8",
    )
    (root / "scripts" / "where.py").write_text(
        "from pathlib import Path\nprint(Path.cwd().name)\n", encoding="utf-8"
    )
    return root


async def test_catalog_exposes_virtual_location(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    _skill(skills)
    provider = SkillCatalogProvider((skills,))
    fragments = await provider.provide(
        SessionSnapshot(SessionKey("cli", "local", "local")),
        make_correlation(),
        ManualCancel(),
    )
    assert len(fragments) == 1
    assert "@skills/pdf-tools/SKILL.md" in fragments[0].content
    assert fragments[0].trust is TrustLevel.OPERATOR


async def test_catalog_supports_folded_yaml_description(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    root = _skill(skills)
    (root / "SKILL.md").write_text(
        "---\nname: pdf-tools\ndescription: >-\n  Process PDF\n  documents.\n---\n",
        encoding="utf-8",
    )
    fragments = await SkillCatalogProvider((skills,)).provide(
        SessionSnapshot(SessionKey("cli", "local", "local")),
        make_correlation(),
        ManualCancel(),
    )
    assert "Process PDF documents." in fragments[0].content


async def test_fs_read_uses_existing_tool_and_trusts_only_skill_entry(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skills = tmp_path / "skills"
    _skill(skills)
    ctx = FakePluginContext(
        config={CONFIG_WORKSPACE_KEY: str(workspace), CONFIG_SKILL_ROOTS_KEY: [str(skills)]}
    )
    from karyvia.builtins.tools_fs import resolve_settings

    settings = resolve_settings(ctx)
    tool = ReadTool(WorkspaceGuard(workspace, settings.skill_roots), settings)
    result = await tool.execute(
        ToolInvocation(
            ToolCall("call-1", "fs.read", {"path": "@skills/pdf-tools/SKILL.md"}),
            make_correlation(),
            5_000,
        ),
        ManualCancel(),
    )
    assert result.ok
    assert result.trust is TrustLevel.SYSTEM

    reference = skills / "pdf-tools" / "reference.md"
    reference.write_text("external data", encoding="utf-8")
    other = await tool.execute(
        ToolInvocation(
            ToolCall("call-2", "fs.read", {"path": "@skills/pdf-tools/reference.md"}),
            make_correlation(),
            5_000,
        ),
        ManualCancel(),
    )
    assert other.trust is TrustLevel.UNTRUSTED


def test_skill_mount_rejects_escape_and_writes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skills = tmp_path / "skills"
    root = _skill(skills)
    guard = WorkspaceGuard(workspace, (skills,))
    with pytest.raises(KaryviaError) as caught:
        guard.resolve("@skills/pdf-tools/../outside")
    assert caught.value.code is ErrorCode.PERMISSION_PATH_OUTSIDE_WORKSPACE
    with pytest.raises(KaryviaError) as caught:
        guard.ensure_writable(root / "SKILL.md")
    assert caught.value.code is ErrorCode.PERMISSION_DENIED


async def test_shell_exec_accepts_skill_mount_as_cwd(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skills = tmp_path / "skills"
    _skill(skills)
    ctx = FakePluginContext(
        config={SHELL_WORKSPACE: str(workspace), SHELL_SKILL_ROOTS: [str(skills)]}
    )
    from karyvia.builtins.tools_shell import resolve_settings

    settings = resolve_settings(ctx)
    executor = ShellExecutor(CwdGuard(workspace, settings.skill_roots), settings)
    result = await executor.execute(
        ToolInvocation(
            ToolCall(
                "call-3",
                "shell.exec",
                {"command": f'"{sys.executable}" scripts/where.py', "cwd": "@skills/pdf-tools"},
            ),
            make_correlation(),
            5_000,
        ),
        ManualCancel(),
    )
    assert result.ok
    assert "pdf-tools" in result.content
