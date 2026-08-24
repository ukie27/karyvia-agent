"""内建文件工具 `tools_fs` 的验收（开发方案 `D20`）。

| 验收项 | 测试 |
| --- | --- |
| 通过 `ToolContract` 全部用例（每个工具一次） | `TestFsRead` … `TestFsGrep` |
| Workspace 逃逸矩阵：符号链接 / `..` / 大小写 / 重解析点（`EDG-405`） | `TestWorkspaceEscape` |
| 单工具禁用后模型可见列表同步消失（`TOL-006`） | `TestSingleToolDisable` |
| 空 / 超大 / 二进制 / 损坏编码（`EDG-205`） | `TestContentEdgeCases` |
| 结果超限截断并置 `truncated=True`（`TOL-003`） | `TestTruncation` |
| 跨平台行为契约一致（`NFR-605`） | `TestCrossPlatformContract` |
| 内建以普通 manifest + `setup(api)` 注册（`BAS-005`） | `TestRegistration` |

三条写这些用例时的取舍：

- **逃逸矩阵走真实文件系统**（`tmp_path`），不 monkeypatch `Path.resolve`。这套守卫的全部
  价值就在于它对真实的符号链接与重解析点成立；打了桩的 `resolve` 只能证明代码路径被走到。
- **符号链接建不了就 skip，不当成通过**。Windows 上创建符号链接要开发者模式或管理员权限，
  把 `OSError` 吞掉记成 pass，等于在最需要这条守卫的平台上悄悄不测它。
- **单工具禁用走真实装配链**（manifest → `wire_capabilities(keep=…)` → registry →
  `tools_from()`）。只断言 `setup()` 少调了一次 `register_tool` 证明不了「模型可见列表」
  这件事——那份列表是从 registry 生成的。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

import pytest

from nucleamind.builtins.registry import BUILTIN_MANIFESTS, TOOLS_FS
from nucleamind.builtins.tools_fs import (
    CONFIG_DISABLE_KEY,
    CONFIG_MAX_ENTRIES_KEY,
    CONFIG_MAX_MATCHES_KEY,
    CONFIG_MAX_READ_BYTES_KEY,
    CONFIG_MAX_RESULT_CHARS_KEY,
    CONFIG_WORKSPACE_KEY,
    EDIT_SPEC,
    GREP_SPEC,
    LIST_SPEC,
    READ_SPEC,
    REPLACEMENT_CHAR,
    TOOL_FACTORIES,
    TOOL_NAMES,
    WRITE_SPEC,
    EditTool,
    FsTool,
    GrepTool,
    ListTool,
    ReadTool,
    WorkspaceGuard,
    WriteTool,
    enabled_tool_names,
    resolve_settings,
    setup,
    truncate,
)
from nucleamind.contracts import (
    Builtin,
    CapabilityKind,
    Concurrency,
    ErrorCode,
    JsonValue,
    NucleaError,
    ProviderId,
    RiskLevel,
    SideEffect,
    ToolCall,
    ToolHandler,
    ToolInvocation,
    ToolSpec,
)
from nucleamind.contracts.tool import MAX_TOOL_RESULT_LENGTH
from nucleamind.kernel.turn.invoker import tools_from
from nucleamind.runtime.wiring import wire_capabilities
from nucleamind.sdk import PluginContext
from nucleamind.sdk.testing import (
    FakePluginContext,
    ManualCancel,
    ToolContract,
    make_correlation,
)

#: 五个工具在 `TOOL_NAMES` 里的顺序，测试里多处用到。
_ALL: Final = TOOL_NAMES


# --------------------------------------------------------------------------- 夹具


def make_workspace(root: Path) -> Path:
    """铺一个有内容的 workspace：两个文本文件 + 一个子目录。"""
    root.mkdir(parents=True, exist_ok=True)
    # 一律 `write_bytes`：`write_text` 在 Windows 上会把 `\n` 翻成 `\r\n`，夹具本身就会
    # 变成平台相关的，而这套用例要断言的恰好是字节级的跨平台一致（`NFR-605`）。
    (root / "notes.txt").write_bytes(b"alpha\nbeta\ngamma\n")
    (root / "sub").mkdir(exist_ok=True)
    (root / "sub" / "code.py").write_bytes(b"def go():\n    return 1\n")
    return root


def make_tool(kind: type[FsTool], root: Path, **config: JsonValue) -> FsTool:
    ctx = FakePluginContext(config={CONFIG_WORKSPACE_KEY: str(root), **config})
    settings = resolve_settings(ctx)
    return kind(WorkspaceGuard(settings.workspace), settings)


def invocation(name: str, **arguments: JsonValue) -> ToolInvocation:
    return ToolInvocation(
        call=ToolCall(call_id="call-1", name=name, arguments=arguments),
        correlation=make_correlation(),
        timeout_ms=5_000,
    )


async def run(tool: FsTool, name: str, **arguments: JsonValue):  # noqa: ANN201 - 见下
    """跑一次工具。返回类型交给推断：写死 `ToolResult` 只会多一个 import。"""
    return await tool.execute(invocation(name, **arguments), ManualCancel())


def try_symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    """建一个符号链接，建不了就 skip——不当成通过（见模块 docstring）。"""
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as error:  # pragma: no cover - 取决于平台权限
        pytest.skip(f"本平台无法创建符号链接：{error}")


def try_junction(link: Path, target: Path) -> None:
    """建一个 Windows 目录联接（重解析点）。

    技术方案 §8.3 把重解析点单列为一类逃逸面，而 Windows 上创建**符号链接**需要开发者
    模式或管理员权限——多数开发机与 CI 上 `try_symlink` 会 skip，那条最该跑的守卫就永远
    没跑过。目录联接不需要提权，走的又是同一条 `resolve()` 判定，因此它是这台机器上
    唯一能真正验到重解析点的途径。
    """
    import subprocess

    completed = subprocess.run(  # noqa: S603 - 参数是测试自己拼的绝对路径
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:  # pragma: no cover - 取决于平台
        pytest.skip(f"无法创建目录联接：{completed.stderr!r}")


# --------------------------------------------------------------------------- 契约


class _FsToolContract(ToolContract):
    """五个工具共用的契约夹具。子类给出 `kind` / `spec` / 一组合法参数。"""

    kind: type[FsTool]
    spec: ToolSpec

    @pytest.fixture(autouse=True)
    def _workspace(self, tmp_path: Path) -> None:
        # `ToolContract` 的用例不接 fixture 参数（它不 import pytest），因此 workspace
        # 只能经实例属性交进去。autouse 保证每个用例都拿到一个干净的目录。
        self.root = make_workspace(tmp_path)

    def make_tool(self) -> tuple[ToolSpec, ToolHandler]:
        return self.spec, make_tool(self.kind, self.root)

    def invalid_arguments(self) -> dict[str, JsonValue] | None:
        return {"path": 12345}


class TestFsRead(_FsToolContract):
    kind = ReadTool
    spec = READ_SPEC

    def valid_arguments(self) -> dict[str, JsonValue]:
        return {"path": "notes.txt"}

    async def test_it_reads_the_whole_file(self, tmp_path: Path) -> None:
        result = await run(make_tool(ReadTool, make_workspace(tmp_path)), "fs.read", path="notes.txt")
        assert result.ok
        assert result.content == "alpha\nbeta\ngamma\n"
        assert result.data == {
            "path": "notes.txt",
            "bytes": 17,
            "start_line": 1,
            "end_line": 4,
            "total_lines": 4,
            "lossy": False,
        }

    async def test_a_line_window_reports_where_it_stopped(self, tmp_path: Path) -> None:
        tool = make_tool(ReadTool, make_workspace(tmp_path))
        result = await run(tool, "fs.read", path="notes.txt", start_line=2, max_lines=1)
        assert result.content == "beta"
        assert result.truncated is True, "只读了一段就得说自己没读全"

    async def test_a_window_past_the_end_is_empty_not_an_error(self, tmp_path: Path) -> None:
        """「这个位置之后没有内容」与「文件读不出来」不是一回事。"""
        tool = make_tool(ReadTool, make_workspace(tmp_path))
        result = await run(tool, "fs.read", path="notes.txt", start_line=99)
        assert result.ok
        assert result.content == ""

    async def test_reading_a_directory_points_at_the_right_tool(self, tmp_path: Path) -> None:
        tool = make_tool(ReadTool, make_workspace(tmp_path))
        result = await run(tool, "fs.read", path="sub")
        assert result.ok is False
        assert result.error is not None
        assert result.error.code is ErrorCode.INPUT_MALFORMED


class TestFsList(_FsToolContract):
    kind = ListTool
    spec = LIST_SPEC

    def valid_arguments(self) -> dict[str, JsonValue]:
        return {"path": "."}

    async def test_it_lists_sorted_entries_with_a_directory_marker(self, tmp_path: Path) -> None:
        result = await run(make_tool(ListTool, make_workspace(tmp_path)), "fs.list")
        assert result.ok
        assert result.content.splitlines() == ["notes.txt\t17 bytes", "sub/"]

    async def test_recursive_descends_into_subdirectories(self, tmp_path: Path) -> None:
        tool = make_tool(ListTool, make_workspace(tmp_path))
        result = await run(tool, "fs.list", recursive=True)
        assert "sub/code.py\t23 bytes" in result.content

    async def test_an_empty_directory_says_so(self, tmp_path: Path) -> None:
        """空目录不是错误，也不是空字符串——模型得看得出来查询成功了。"""
        (tmp_path / "empty").mkdir()
        result = await run(make_tool(ListTool, tmp_path), "fs.list", path="empty")
        assert result.ok
        assert result.content == "(空目录)"
        assert result.data is not None and result.data["entries"] == 0

    async def test_too_many_entries_are_truncated(self, tmp_path: Path) -> None:
        for index in range(10):
            (tmp_path / f"f{index}.txt").write_text("x", encoding="utf-8")
        tool = make_tool(ListTool, tmp_path, **{CONFIG_MAX_ENTRIES_KEY: 3})
        result = await run(tool, "fs.list")
        assert result.truncated is True
        assert len(result.content.splitlines()) == 3


class TestFsGrep(_FsToolContract):
    kind = GrepTool
    spec = GREP_SPEC

    def valid_arguments(self) -> dict[str, JsonValue]:
        return {"pattern": "alpha"}

    def invalid_arguments(self) -> dict[str, JsonValue] | None:
        return {"pattern": "("}

    async def test_it_reports_path_line_and_text(self, tmp_path: Path) -> None:
        result = await run(make_tool(GrepTool, make_workspace(tmp_path)), "fs.grep", pattern="beta")
        assert result.ok
        assert result.content == "notes.txt:2: beta"

    async def test_a_glob_narrows_the_file_set(self, tmp_path: Path) -> None:
        tool = make_tool(GrepTool, make_workspace(tmp_path))
        result = await run(tool, "fs.grep", pattern="return", glob="*.py")
        assert result.content == "sub/code.py:2:     return 1"

    async def test_no_match_is_a_successful_empty_answer(self, tmp_path: Path) -> None:
        tool = make_tool(GrepTool, make_workspace(tmp_path))
        result = await run(tool, "fs.grep", pattern="不存在的东西")
        assert result.ok
        assert result.content == "(无匹配)"

    async def test_case_insensitive_search(self, tmp_path: Path) -> None:
        tool = make_tool(GrepTool, make_workspace(tmp_path))
        assert (await run(tool, "fs.grep", pattern="ALPHA")).content == "(无匹配)"
        hit = await run(tool, "fs.grep", pattern="ALPHA", case_sensitive=False)
        assert hit.content == "notes.txt:1: alpha"

    async def test_a_broken_pattern_is_a_result_not_an_exception(self, tmp_path: Path) -> None:
        result = await run(make_tool(GrepTool, tmp_path), "fs.grep", pattern="[unclosed")
        assert result.ok is False
        assert result.error is not None
        assert result.error.code is ErrorCode.INPUT_MALFORMED

    async def test_binary_files_are_skipped_not_fatal(self, tmp_path: Path) -> None:
        (tmp_path / "text.txt").write_text("needle\n", encoding="utf-8")
        (tmp_path / "blob.bin").write_bytes(b"needle\x00\x01\x02")
        result = await run(make_tool(GrepTool, tmp_path), "fs.grep", pattern="needle")
        assert result.ok
        assert result.content == "text.txt:1: needle"
        assert result.data is not None and result.data["files_skipped"] == 1


class TestFsWrite(_FsToolContract):
    kind = WriteTool
    spec = WRITE_SPEC

    def valid_arguments(self) -> dict[str, JsonValue]:
        return {"path": "fresh.txt", "content": "hi"}

    def invalid_arguments(self) -> dict[str, JsonValue] | None:
        return {"path": "fresh.txt"}  # 缺 content

    async def test_it_creates_the_file_and_its_parents(self, tmp_path: Path) -> None:
        result = await run(make_tool(WriteTool, tmp_path), "fs.write", path="a/b/c.txt", content="x")
        assert result.ok
        assert result.side_effect is SideEffect.OCCURRED
        assert (tmp_path / "a" / "b" / "c.txt").read_text(encoding="utf-8") == "x"

    async def test_overwriting_is_reported_as_such(self, tmp_path: Path) -> None:
        tool = make_tool(WriteTool, make_workspace(tmp_path))
        result = await run(tool, "fs.write", path="notes.txt", content="new")
        assert result.data is not None and result.data["overwritten"] is True

    async def test_a_rejected_write_leaves_no_trace(self, tmp_path: Path) -> None:
        """越界写必须在落盘之前被挡住，`side_effect` 才谈得上如实（`EDG-401`）。"""
        result = await run(make_tool(WriteTool, tmp_path), "fs.write", path="../x.txt", content="x")
        assert result.ok is False
        assert result.side_effect is SideEffect.NONE
        assert not (tmp_path.parent / "x.txt").exists()

    async def test_no_temporary_file_survives_a_successful_write(self, tmp_path: Path) -> None:
        """留下一个 `.nm-tmp` 残骸会让下一次 `fs.list` 把它当成用户的文件列出来。"""
        root = tmp_path / "fresh"
        root.mkdir()
        await run(make_tool(WriteTool, root), "fs.write", path="c.txt", content="x")
        assert [item.name for item in root.iterdir()] == ["c.txt"]


class TestFsEdit(_FsToolContract):
    kind = EditTool
    spec = EDIT_SPEC

    def valid_arguments(self) -> dict[str, JsonValue]:
        return {"path": "notes.txt", "old_text": "beta", "new_text": "BETA"}

    def invalid_arguments(self) -> dict[str, JsonValue] | None:
        return {"path": "notes.txt", "old_text": "beta"}  # 缺 new_text

    async def test_it_replaces_exactly_one_occurrence(self, tmp_path: Path) -> None:
        root = make_workspace(tmp_path)
        result = await run(
            make_tool(EditTool, root), "fs.edit", path="notes.txt", old_text="beta", new_text="B"
        )
        assert result.ok
        assert (root / "notes.txt").read_text(encoding="utf-8") == "alpha\nB\ngamma\n"

    async def test_an_ambiguous_match_changes_nothing(self, tmp_path: Path) -> None:
        (tmp_path / "dup.txt").write_text("x\nx\n", encoding="utf-8")
        result = await run(
            make_tool(EditTool, tmp_path), "fs.edit", path="dup.txt", old_text="x", new_text="y"
        )
        assert result.ok is False
        assert result.side_effect is SideEffect.NONE
        assert (tmp_path / "dup.txt").read_text(encoding="utf-8") == "x\nx\n"

    async def test_replace_all_opts_into_the_ambiguous_case(self, tmp_path: Path) -> None:
        (tmp_path / "dup.txt").write_text("x\nx\n", encoding="utf-8")
        result = await run(
            make_tool(EditTool, tmp_path),
            "fs.edit",
            path="dup.txt",
            old_text="x",
            new_text="y",
            replace_all=True,
        )
        assert result.ok
        assert result.data is not None and result.data["replacements"] == 2
        assert (tmp_path / "dup.txt").read_text(encoding="utf-8") == "y\ny\n"

    async def test_a_missing_match_changes_nothing(self, tmp_path: Path) -> None:
        root = make_workspace(tmp_path)
        result = await run(
            make_tool(EditTool, root), "fs.edit", path="notes.txt", old_text="nope", new_text="y"
        )
        assert result.ok is False
        assert (root / "notes.txt").read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"

    async def test_editing_a_missing_file_is_a_read_failure(self, tmp_path: Path) -> None:
        result = await run(
            make_tool(EditTool, tmp_path), "fs.edit", path="ghost.txt", old_text="a", new_text="b"
        )
        assert result.ok is False
        assert result.error is not None
        assert result.error.code is ErrorCode.PERSISTENCE_READ_FAILED


# ------------------------------------------------------------------------ EDG-405


class TestWorkspaceEscape:
    """逃逸矩阵。这是 `NFR-302` 的唯一防线，因此每一类都单独立一条。"""

    def guard(self, root: Path) -> WorkspaceGuard:
        return WorkspaceGuard(root)

    @pytest.mark.parametrize(
        "raw",
        ["..", "../outside.txt", "sub/../../outside.txt", "./sub/./../../x"],
        ids=["bare", "parent", "through-sub", "mixed"],
    )
    def test_dot_dot_cannot_climb_out(self, tmp_path: Path, raw: str) -> None:
        with pytest.raises(NucleaError) as caught:
            self.guard(make_workspace(tmp_path / "ws")).resolve(raw)
        assert caught.value.code is ErrorCode.PERMISSION_PATH_OUTSIDE_WORKSPACE

    def test_dot_dot_that_stays_inside_is_allowed(self, tmp_path: Path) -> None:
        """守卫要挡的是出界，不是 `..` 这个写法本身。"""
        root = make_workspace(tmp_path / "ws")
        assert self.guard(root).resolve("sub/../notes.txt") == (root / "notes.txt").resolve()

    def test_a_symlink_to_a_file_outside_is_rejected(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        root.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("s3cret", encoding="utf-8")
        try_symlink(root / "link.txt", secret)
        with pytest.raises(NucleaError) as caught:
            self.guard(root).resolve("link.txt")
        assert caught.value.code is ErrorCode.PERMISSION_PATH_OUTSIDE_WORKSPACE

    def test_a_symlinked_directory_outside_is_rejected(self, tmp_path: Path) -> None:
        """重解析点走的是同一条判定：`resolve()` 之后再校验一次。"""
        root = tmp_path / "ws"
        root.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "file.txt").write_text("x", encoding="utf-8")
        try_symlink(root / "door", elsewhere, directory=True)
        with pytest.raises(NucleaError) as caught:
            self.guard(root).resolve("door/file.txt")
        assert caught.value.code is ErrorCode.PERMISSION_PATH_OUTSIDE_WORKSPACE

    def test_a_symlink_that_stays_inside_is_allowed(self, tmp_path: Path) -> None:
        root = make_workspace(tmp_path / "ws")
        try_symlink(root / "alias.txt", root / "notes.txt")
        assert self.guard(root).resolve("alias.txt") == (root / "notes.txt").resolve()

    @pytest.mark.skipif(os.name != "nt", reason="目录联接是 Windows 特有的重解析点")
    def test_a_windows_junction_out_of_the_root_is_rejected(self, tmp_path: Path) -> None:
        """重解析点是 §8.3 单列的一类逃逸面，且它不需要提权就能建（见 `try_junction`）。"""
        root = tmp_path / "ws"
        root.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "file.txt").write_bytes(b"x")
        try_junction(root / "door", elsewhere)
        with pytest.raises(NucleaError) as caught:
            self.guard(root).resolve("door/file.txt")
        assert caught.value.code is ErrorCode.PERMISSION_PATH_OUTSIDE_WORKSPACE

    @pytest.mark.skipif(os.name != "nt", reason="目录联接是 Windows 特有的重解析点")
    async def test_listing_skips_a_junction_pointing_out_of_the_root(self, tmp_path: Path) -> None:
        root = make_workspace(tmp_path / "ws")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "secret.txt").write_bytes(b"x")
        try_junction(root / "door", elsewhere)
        result = await run(make_tool(ListTool, root), "fs.list", recursive=True)
        assert result.ok
        assert "door" not in result.content
        assert "secret.txt" not in result.content

    @pytest.mark.skipif(os.name != "nt", reason="大小写折叠只在 Windows 上是绕过面")
    def test_windows_case_differences_do_not_bypass_the_boundary(self, tmp_path: Path) -> None:
        """`WS/notes.txt` 与 `ws/notes.txt` 在 Windows 上是同一个文件，判定必须一致。"""
        root = make_workspace(tmp_path / "ws")
        guard = self.guard(root)
        assert guard.resolve("NOTES.TXT") == guard.resolve("notes.txt")

    @pytest.mark.parametrize("name", ["CON", "nul", "com1", "LPT9.txt"])
    def test_reserved_device_names_are_rejected_on_every_platform(
        self, tmp_path: Path, name: str
    ) -> None:
        """两个平台一律拒绝——为它开一个平台分支就破坏了 `NFR-605`。"""
        with pytest.raises(NucleaError) as caught:
            self.guard(tmp_path).resolve(name)
        assert caught.value.code is ErrorCode.INPUT_MALFORMED

    def test_an_absolute_path_inside_the_root_is_accepted(self, tmp_path: Path) -> None:
        """绝对路径过的是同一道门，因此没有理由额外拒绝它。"""
        root = make_workspace(tmp_path / "ws")
        assert self.guard(root).resolve(str(root / "notes.txt")) == (root / "notes.txt").resolve()

    def test_an_absolute_path_outside_the_root_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(NucleaError) as caught:
            self.guard(tmp_path / "ws").resolve(str(tmp_path / "other.txt"))
        assert caught.value.code is ErrorCode.PERMISSION_PATH_OUTSIDE_WORKSPACE

    def test_a_sibling_directory_sharing_a_prefix_is_not_inside(self, tmp_path: Path) -> None:
        """`/x/ws-evil` 不是 `/x/ws` 的后代——前缀比较必须落在分隔符边界上。"""
        (tmp_path / "ws").mkdir()
        (tmp_path / "ws-evil").mkdir()
        with pytest.raises(NucleaError):
            self.guard(tmp_path / "ws").resolve(str(tmp_path / "ws-evil" / "x.txt"))

    @pytest.mark.parametrize("raw", ["", "   ", "a\x00b"], ids=["empty", "blank", "nul"])
    def test_malformed_path_strings_are_rejected(self, tmp_path: Path, raw: str) -> None:
        with pytest.raises(NucleaError) as caught:
            self.guard(tmp_path).resolve(raw)
        assert caught.value.code is ErrorCode.INPUT_MALFORMED

    def test_the_rejection_detail_does_not_leak_the_host_path(self, tmp_path: Path) -> None:
        """错误是模型可见的：里面不该出现宿主机的绝对路径。"""
        root = tmp_path / "ws"
        root.mkdir()
        with pytest.raises(NucleaError) as caught:
            self.guard(root).resolve("../secret.txt")
        assert caught.value.detail == {"path": "../secret.txt"}

    async def test_listing_skips_an_out_of_bounds_symlink(self, tmp_path: Path) -> None:
        """一条越界链接不该让整次列目录失败，也不该出现在清单里。"""
        root = make_workspace(tmp_path / "ws")
        outside = tmp_path / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        try_symlink(root / "escape.txt", outside)
        result = await run(make_tool(ListTool, root), "fs.list")
        assert result.ok
        assert "escape.txt" not in result.content


# ------------------------------------------------------------------------ EDG-205


class TestContentEdgeCases:
    """空、超大、二进制、损坏编码各有可预期结果。"""

    async def test_an_empty_file_reads_as_empty_not_missing(self, tmp_path: Path) -> None:
        (tmp_path / "empty.txt").write_bytes(b"")
        result = await run(make_tool(ReadTool, tmp_path), "fs.read", path="empty.txt")
        assert result.ok
        assert result.content == ""
        assert result.data is not None and result.data["bytes"] == 0

    async def test_a_binary_file_is_refused_rather_than_mangled(self, tmp_path: Path) -> None:
        (tmp_path / "blob.bin").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
        result = await run(make_tool(ReadTool, tmp_path), "fs.read", path="blob.bin")
        assert result.ok is False
        assert result.error is not None
        assert result.error.code is ErrorCode.INPUT_UNSUPPORTED_MEDIA

    async def test_broken_encoding_is_readable_and_says_so(self, tmp_path: Path) -> None:
        (tmp_path / "mixed.txt").write_bytes("好".encode("gbk") + b"\ntail\n")
        result = await run(make_tool(ReadTool, tmp_path), "fs.read", path="mixed.txt")
        assert result.ok, "一行坏字节不该让整份文件读不出来"
        assert REPLACEMENT_CHAR in result.content
        assert result.data is not None and result.data["lossy"] is True

    async def test_editing_a_broken_encoding_file_is_refused(self, tmp_path: Path) -> None:
        """读可以将就，写不行——写回去会把 `�` 变成文件的真实内容。"""
        (tmp_path / "mixed.txt").write_bytes("好".encode("gbk") + b"\ntail\n")
        result = await run(
            make_tool(EditTool, tmp_path), "fs.edit", path="mixed.txt", old_text="tail", new_text="t"
        )
        assert result.ok is False
        assert result.error is not None
        assert result.error.code is ErrorCode.INPUT_UNSUPPORTED_MEDIA

    async def test_an_oversized_file_is_truncated_not_refused(self, tmp_path: Path) -> None:
        (tmp_path / "big.txt").write_text("x" * 5000, encoding="utf-8")
        tool = make_tool(ReadTool, tmp_path, **{CONFIG_MAX_READ_BYTES_KEY: 100})
        result = await run(tool, "fs.read", path="big.txt")
        assert result.ok
        assert result.truncated is True
        assert result.data is not None and result.data["bytes"] == 100

    async def test_an_oversized_file_cannot_be_edited(self, tmp_path: Path) -> None:
        """编辑要整份读进内存再写回：超限时拒绝，而不是悄悄截掉后半截。"""
        (tmp_path / "big.txt").write_text("needle" + "x" * 5000, encoding="utf-8")
        tool = make_tool(EditTool, tmp_path, **{CONFIG_MAX_READ_BYTES_KEY: 100})
        result = await run(tool, "fs.edit", path="big.txt", old_text="needle", new_text="n")
        assert result.ok is False
        assert result.error is not None
        assert result.error.code is ErrorCode.INPUT_TOO_LARGE


# ------------------------------------------------------------------------ TOL-003


class TestTruncation:
    """超限结果必须截断并置 `truncated=True`，且**含标记在内**不超上限。"""

    @pytest.mark.parametrize("limit", [40, 100, 1000, MAX_TOOL_RESULT_LENGTH])
    def test_the_marker_is_counted_inside_the_limit(self, limit: int) -> None:
        text, cut = truncate("x" * (limit * 2), limit)
        assert cut is True
        assert len(text) <= limit, "标记必须算在上限里，否则构造 ToolResult 会失败"

    def test_short_text_is_untouched(self) -> None:
        assert truncate("short", 100) == ("short", False)

    def test_the_marker_reports_the_real_shown_length(self) -> None:
        text, _ = truncate("x" * 500, 100)
        shown = len(text) - len(text[text.index("\n…") :])
        assert f"已显示 {shown}/500 字符" in text

    async def test_an_oversized_result_is_cut_and_flagged(self, tmp_path: Path) -> None:
        (tmp_path / "big.txt").write_text("y" * 5000, encoding="utf-8")
        tool = make_tool(ReadTool, tmp_path, **{CONFIG_MAX_RESULT_CHARS_KEY: 200})
        result = await run(tool, "fs.read", path="big.txt")
        assert result.truncated is True
        assert len(result.content) <= 200

    async def test_grep_stops_at_the_match_ceiling(self, tmp_path: Path) -> None:
        (tmp_path / "many.txt").write_text("hit\n" * 50, encoding="utf-8")
        tool = make_tool(GrepTool, tmp_path, **{CONFIG_MAX_MATCHES_KEY: 5})
        result = await run(tool, "fs.grep", pattern="hit")
        assert result.truncated is True
        assert len(result.content.splitlines()) == 5

    def test_a_result_limit_above_the_contract_ceiling_is_refused(self) -> None:
        ctx = FakePluginContext(config={CONFIG_MAX_RESULT_CHARS_KEY: MAX_TOOL_RESULT_LENGTH + 1})
        with pytest.raises(NucleaError) as caught:
            resolve_settings(ctx)
        assert caught.value.code is ErrorCode.CONFIG_INVALID


# ------------------------------------------------------------------------ NFR-605


class TestCrossPlatformContract:
    """同参数产生同退出语义与同截断规则，与本机是哪个平台无关。"""

    async def test_crlf_is_normalised_on_read(self, tmp_path: Path) -> None:
        (tmp_path / "crlf.txt").write_bytes(b"a\r\nb\r\n")
        result = await run(make_tool(ReadTool, tmp_path), "fs.read", path="crlf.txt")
        assert result.content == "a\nb\n"

    async def test_writes_are_byte_for_byte_identical_on_both_platforms(
        self, tmp_path: Path
    ) -> None:
        """文本模式会在 Windows 上把 `\\n` 变成 `\\r\\n`，那样两个平台的产物就不同了。"""
        await run(make_tool(WriteTool, tmp_path), "fs.write", path="out.txt", content="a\nb\n")
        assert (tmp_path / "out.txt").read_bytes() == b"a\nb\n"

    async def test_listing_order_is_sorted_not_filesystem_order(self, tmp_path: Path) -> None:
        for name in ("z.txt", "a.txt", "m.txt"):
            (tmp_path / name).write_text("x", encoding="utf-8")
        result = await run(make_tool(ListTool, tmp_path), "fs.list")
        assert [line.split("\t")[0] for line in result.content.splitlines()] == [
            "a.txt",
            "m.txt",
            "z.txt",
        ]

    async def test_paths_in_results_are_posix_relative(self, tmp_path: Path) -> None:
        result = await run(
            make_tool(ListTool, make_workspace(tmp_path)), "fs.list", recursive=True
        )
        assert "sub/code.py" in result.content
        assert "\\" not in result.content, "结果里不该出现平台相关的分隔符"

    async def test_reading_a_directory_is_the_same_error_everywhere(self, tmp_path: Path) -> None:
        """裸 `open()` 一个目录在 Linux 与 Windows 上抛的是不同的 `OSError`。"""
        (tmp_path / "d").mkdir()
        result = await run(make_tool(ReadTool, tmp_path), "fs.read", path="d")
        assert result.error is not None
        assert result.error.code is ErrorCode.INPUT_MALFORMED


# ------------------------------------------------------------------------ TOL-006


def context_with(**config: JsonValue) -> PluginContext:
    return FakePluginContext(config=config)


class TestSingleToolDisable:
    """单工具禁用后，模型可见列表中同步消失。"""

    def test_by_default_every_tool_is_enabled(self) -> None:
        assert enabled_tool_names({}) == _ALL

    def test_disabling_removes_exactly_that_name(self) -> None:
        enabled = enabled_tool_names({CONFIG_DISABLE_KEY: ["fs.write", "fs.edit"]})
        assert enabled == ("fs.read", "fs.list", "fs.grep")

    def test_an_unknown_name_is_refused_rather_than_ignored(self) -> None:
        """一句本意是「关掉写工具」的配置被静默忽略，代价是模型仍然能写盘。"""
        with pytest.raises(NucleaError) as caught:
            enabled_tool_names({CONFIG_DISABLE_KEY: ["fs.wirte"]})
        assert caught.value.code is ErrorCode.CONFIG_INVALID
        assert caught.value.detail["unknown"] == ["fs.wirte"]

    def test_setup_registers_only_the_enabled_tools(self, tmp_path: Path) -> None:
        registered: list[str] = []

        class RecordingApi:
            ctx = context_with(
                **{CONFIG_WORKSPACE_KEY: str(tmp_path), CONFIG_DISABLE_KEY: ["fs.write"]}
            )

            def register_tool(self, spec: ToolSpec, handler: ToolHandler) -> None:
                del handler
                registered.append(spec.name)

        setup(RecordingApi())  # type: ignore[arg-type]
        assert registered == ["fs.read", "fs.edit", "fs.list", "fs.grep"]

    async def test_the_disabled_tool_is_absent_from_the_registry(self, tmp_path: Path) -> None:
        """走真实装配链：manifest → wiring(keep=…) → registry → `tools_from()`。

        这才是「模型可见列表」的来源。只断言 `setup()` 少调了一次 `register_tool`
        证明不了它。
        """
        config: dict[str, JsonValue] = {
            CONFIG_WORKSPACE_KEY: str(tmp_path),
            CONFIG_DISABLE_KEY: ["fs.write", "fs.edit"],
        }
        allowed = set(enabled_tool_names(config))

        def context_for(provider: ProviderId) -> PluginContext:
            del provider
            return FakePluginContext(config=config)

        wiring = await wire_capabilities(
            manifests=[TOOLS_FS],
            context_for=context_for,
            keep=lambda manifest, decl: decl.name in allowed,
        )

        assert wiring.report.ok
        names = sorted(tool.spec.name for tool in tools_from(wiring.registry))
        assert names == ["fs.grep", "fs.list", "fs.read"]

    async def test_forgetting_the_filter_fails_loudly(self, tmp_path: Path) -> None:
        """声明与注册必须同源。不过滤声明就是「声明了却没注册」，`D16` 会拒绝加载。"""
        config: dict[str, JsonValue] = {
            CONFIG_WORKSPACE_KEY: str(tmp_path),
            CONFIG_DISABLE_KEY: ["fs.write"],
        }

        def context_for(provider: ProviderId) -> PluginContext:
            del provider
            return FakePluginContext(config=config)

        wiring = await wire_capabilities(manifests=[TOOLS_FS], context_for=context_for)
        failures = [outcome for outcome in wiring.outcomes if outcome.error is not None]
        assert failures, "少注册一项能力必须被 CapabilityHost.finish() 挡下"
        assert failures[0].error is not None
        assert failures[0].error.code is ErrorCode.PLUGIN_LOAD_FAILED


# --------------------------------------------------------------------------- 注册


class TestRegistration:
    """内建的落地形态：一份普通 manifest + 一个 `setup(api)`，没有第二条路（`BAS-005`）。"""

    def test_the_manifest_is_listed_as_a_builtin(self) -> None:
        assert TOOLS_FS in BUILTIN_MANIFESTS
        assert TOOLS_FS.id == "tools-fs"

    def test_every_declaration_matches_the_frozen_tool_list(self) -> None:
        """§8.2 的清单本身是接口：manifest、`TOOL_NAMES` 与装配表必须三处同集。"""
        declared = [decl.name for decl in TOOLS_FS.capabilities]
        assert declared == list(_ALL)
        assert set(TOOL_FACTORIES) == set(_ALL)
        assert all(decl.kind is CapabilityKind.TOOL for decl in TOOLS_FS.capabilities)
        # `priority` 不写：内建基准是 0，写了（哪怕写的是默认值 100）就会被原样采纳。
        assert all("priority" not in decl.model_fields_set for decl in TOOLS_FS.capabilities)

    def test_the_spec_name_matches_the_table_key(self) -> None:
        assert all(spec.name == name for name, (spec, _) in TOOL_FACTORIES.items())

    def test_the_config_schema_lists_exactly_the_keys_the_code_reads(self) -> None:
        properties = TOOLS_FS.config_schema["properties"]
        assert isinstance(properties, dict)
        assert set(properties) == {
            CONFIG_WORKSPACE_KEY,
            CONFIG_DISABLE_KEY,
            CONFIG_MAX_READ_BYTES_KEY,
            CONFIG_MAX_RESULT_CHARS_KEY,
            CONFIG_MAX_ENTRIES_KEY,
            CONFIG_MAX_MATCHES_KEY,
        }
        assert TOOLS_FS.config_schema["additionalProperties"] is False

    def test_read_only_tools_are_safe(self) -> None:
        for spec in (READ_SPEC, LIST_SPEC, GREP_SPEC):
            assert spec.read_only is True
            assert spec.risk is RiskLevel.SAFE

    def test_write_tools_are_exclusive_and_destructive(self) -> None:
        """覆盖既有内容不可撤销；两次写同一个文件的结果又取决于顺序。"""
        for spec in (WRITE_SPEC, EDIT_SPEC):
            assert spec.read_only is False
            assert spec.risk is RiskLevel.DESTRUCTIVE
            assert spec.concurrency is Concurrency.EXCLUSIVE

    async def test_wiring_registers_all_five_at_the_builtin_priority(self, tmp_path: Path) -> None:
        def context_for(provider: ProviderId) -> PluginContext:
            del provider
            return FakePluginContext(config={CONFIG_WORKSPACE_KEY: str(tmp_path)})

        wiring = await wire_capabilities(manifests=[TOOLS_FS], context_for=context_for)

        assert wiring.report.ok
        registrations = wiring.registry.of_kind(CapabilityKind.TOOL)
        assert len(registrations) == len(_ALL)
        assert all(item.priority == 0 for item in registrations), "内建必须排在插件之前"
        assert all(item.ref.provider == Builtin() for item in registrations)

    def test_a_bad_configuration_fails_at_setup_rather_than_at_the_first_turn(self) -> None:
        class RecordingApi:
            ctx = context_with(**{CONFIG_WORKSPACE_KEY: 123})  # type: ignore[dict-item]

            def register_tool(self, spec: ToolSpec, handler: ToolHandler) -> None:
                raise AssertionError("配置非法时不该注册任何东西")

        with pytest.raises(NucleaError) as caught:
            setup(RecordingApi())  # type: ignore[arg-type]
        assert caught.value.code is ErrorCode.CONFIG_INVALID

    def test_the_workspace_falls_back_to_the_private_state_dir(self, tmp_path: Path) -> None:
        """没配 workspace 时不该悄悄用进程 cwd（`D23` 必须真的把它填上）。"""
        settings = resolve_settings(FakePluginContext(state_dir=tmp_path))
        assert settings.workspace == tmp_path

    def test_setup_touches_no_disk(self, tmp_path: Path) -> None:
        """`nm capabilities` 这类只读命令不该因为一个从未用过的 workspace 留下痕迹。"""
        target = tmp_path / "never-created"

        class RecordingApi:
            ctx = context_with(**{CONFIG_WORKSPACE_KEY: str(target)})

            def register_tool(self, spec: ToolSpec, handler: ToolHandler) -> None:
                del spec, handler

        setup(RecordingApi())  # type: ignore[arg-type]
        assert not target.exists()


def test_json_value_typing_is_satisfied_by_the_documented_config(tmp_path: Path) -> None:
    """文档化的六个键都是 `JsonValue`——配置块会原样穿过 JSON。"""
    config: dict[str, JsonValue] = {
        CONFIG_WORKSPACE_KEY: str(tmp_path),
        CONFIG_DISABLE_KEY: ["fs.grep"],
        CONFIG_MAX_READ_BYTES_KEY: 2048,
        CONFIG_MAX_RESULT_CHARS_KEY: 4096,
        CONFIG_MAX_ENTRIES_KEY: 10,
        CONFIG_MAX_MATCHES_KEY: 20,
    }
    settings = resolve_settings(FakePluginContext(config=config))
    assert settings.enabled == ("fs.read", "fs.write", "fs.edit", "fs.list")
    assert settings.max_matches == 20
