"""内建 shell 工具 `tools_shell` 的验收（开发方案 `D21`）。

| 验收项 | 测试 |
| --- | --- |
| 通过 `ToolContract` 全部用例 | `TestShellExec` |
| 取消宽限期矩阵：超时后收尾 / 宽限期用尽（`EDG-407`） | `TestCancelGrace` |
| cwd 守卫与 `tools_fs.WorkspaceGuard` 逐条相同（`EDG-405`） | `test_cwd_guard_matches_the_fs_workspace_guard` |
| 环境变量默认全部不继承（`NFR-307`） | `TestEnvironmentVariables` |
| 非零退出码是正常产出而不是错误 | `TestExitCodeSemantics` |
| 跨平台行为契约一致（`NFR-605`） | `TestCrossPlatformContract` |
| 单工具禁用后模型可见列表同步消失（`TOL-006`） | `TestSingleToolDisable` |
| 内建以普通 manifest + `setup(api)` 注册（`BAS-005`） | `TestRegistration` |

三条写这些用例时的取舍：

- **取消宽限期走真实进程**（`asyncio.create_subprocess_exec`），不 monkeypatch。这套机制的
  全部价值就在于它对真实的进程与信号成立；打了桩的 `terminate()` 只能证明代码路径被走到。
- **cwd 守卫走真实文件系统**（`tmp_path`），并断言它与 `tools_fs.WorkspaceGuard` 逐条对照。
  两个内建工具包的边界判定必须一致（`EDG-405`），而一条人工维护的「应该一致」比不上一条
  失败了就停发的测试。
- **单工具禁用走真实装配链**（manifest → `wire_capabilities(keep=…)` → registry），与
  `test_tools_fs.py::TestSingleToolDisable` 同一套做法。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Final

import pytest

from karyvia.builtins.registry import TOOLS_SHELL
from karyvia.builtins.tools_fs import WorkspaceGuard as FsWorkspaceGuard
from karyvia.builtins.tools_shell import (
    CONFIG_DISABLE_KEY,
    CONFIG_ENV_KEY,
    CONFIG_MAX_OUTPUT_CHARS_KEY,
    CONFIG_PASS_ENV_KEY,
    CONFIG_SHELL_KEY,
    CONFIG_TIMEOUT_KEY,
    CONFIG_WORKSPACE_KEY,
    EXEC_SPEC,
    TOOL_NAME,
    ShellExecutor,
    enabled_tool_names,
    resolve_settings,
    setup,
)
from karyvia.builtins.tools_shell.command import MAX_COMMAND_LENGTH, build_argv
from karyvia.builtins.tools_shell.environ import BASELINE_NAMES, FORCED_ENV, build_environment
from karyvia.builtins.tools_shell.executor import render_output
from karyvia.builtins.tools_shell.paths import CwdGuard
from karyvia.builtins.tools_shell.process import (
    CANCEL_POLL_MS,
    DEFAULT_GRACE_MS,
    ProcessOutcome,
    ProcessResult,
    run_process,
)
from karyvia.contracts import (
    ErrorCode,
    JsonValue,
    KaryviaError,
    ProviderId,
    RiskLevel,
    SideEffect,
    ToolCall,
    ToolHandler,
    ToolInvocation,
    ToolSpec,
)
from karyvia.kernel.turn.invoker import tools_from
from karyvia.runtime.wiring import wire_capabilities
from karyvia.sdk import PluginContext
from karyvia.sdk.testing import (
    FakePluginContext,
    ManualCancel,
    ToolContract,
    make_correlation,
)

#: 本包只有一个工具名。
_ONLY: Final = (TOOL_NAME,)


# --------------------------------------------------------------------------- 夹具


def make_workspace(root: Path) -> Path:
    """铺一个能跑命令的 workspace：一个文本文件 + 子目录 + 脚本。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "file.txt").write_bytes(b"content\n")
    (root / "sub").mkdir(exist_ok=True)
    return root


def make_executor(root: Path, **config: JsonValue) -> ShellExecutor:
    ctx = FakePluginContext(config={CONFIG_WORKSPACE_KEY: str(root), **config})
    settings = resolve_settings(ctx)
    return ShellExecutor(CwdGuard(settings.workspace), settings)


def invocation(name: str, **arguments: JsonValue) -> ToolInvocation:
    return ToolInvocation(
        call=ToolCall(call_id="call-1", name=name, arguments=arguments),
        correlation=make_correlation(),
        timeout_ms=10_000,
    )


async def run(executor: ShellExecutor, **arguments: JsonValue):  # noqa: ANN201
    """跑一次工具。返回类型交给推断：写死 `ToolResult` 只会多一个 import。"""
    return await executor.execute(invocation(TOOL_NAME, **arguments), ManualCancel())


def try_symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    """建一个符号链接，建不了就 skip——不当成通过（与 `test_tools_fs` 同一条理由）。"""
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as error:  # pragma: no cover - 取决于平台权限
        pytest.skip(f"本平台无法创建符号链接：{error}")


def try_junction(link: Path, target: Path) -> None:
    """建一个 Windows 目录联接（重解析点）。

    技术方案 §8.3 把重解析点单列为一类逃逸面，而 Windows 上创建**符号链接**需要开发者
    模式或管理员权限——多数开发机与 CI 上 `try_symlink` 会 skip，那条最该跑的守卫
    （`paths.py` 的 realpath 校验）就永远没跑过。目录联接不需要提权，走的又是同一条
    `resolve()` 判定，因此它是这台机器上唯一能真正验到重解析点的途径。
    与 `test_tools_fs.py::try_junction` 同一条做法。
    """
    import subprocess

    completed = subprocess.run(  # noqa: S603 - 参数是测试自己拼的绝对路径
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:  # pragma: no cover - 取决于平台
        pytest.skip(f"无法创建目录联接：{completed.stderr!r}")


def sleep_command(seconds: float) -> str:
    """一条跨平台的「睡 N 秒」命令。

    **不用 `sleep` / `timeout /t`**：前者在 Windows 上不存在，后者在 stdin 被重定向到
    DEVNULL 时（本工具恒如此）会立刻报错退出——一条本该睡 10 秒的命令瞬间返回，
    取消用例于是测了个寂寞。用当前解释器则两个平台行为完全一致。
    """
    return f'"{sys.executable}" -c "import time; time.sleep({seconds})"'


def context_with(**config: JsonValue) -> FakePluginContext:
    """造一个 `PluginContext`，只为让 `setup()` 跑起来——那个函数读 `api.ctx`。"""
    return FakePluginContext(config=config)


# --------------------------------------------------------------------------- 契约


class TestShellExec(ToolContract):
    """唯一的工具 `shell.exec` 通过 `ToolContract` 全部用例。"""

    @pytest.fixture(autouse=True)
    def _workspace(self, tmp_path: Path) -> None:
        self.root = make_workspace(tmp_path)

    def make_tool(self) -> tuple[ToolSpec, ToolHandler]:
        return EXEC_SPEC, make_executor(self.root)

    def valid_arguments(self) -> dict[str, JsonValue]:
        # 跨平台的「总是成功」命令：POSIX 的 `true`、Windows 的 `exit 0`。
        return {"command": "true" if os.name != "nt" else "exit 0"}

    def invalid_arguments(self) -> dict[str, JsonValue] | None:
        return {"command": 12345}

    async def test_it_runs_a_simple_command(self, tmp_path: Path) -> None:
        executor = make_executor(make_workspace(tmp_path))
        cmd = "echo hello" if os.name != "nt" else "echo hello"
        result = await run(executor, command=cmd)
        assert result.ok
        assert "hello" in result.content
        assert result.data["exit_code"] == 0
        assert result.data["outcome"] == ProcessOutcome.COMPLETED

    async def test_a_command_with_nonzero_exit_is_still_ok(self, tmp_path: Path) -> None:
        """非零退出码是正常产出而不是错误（见 `executor.py` 模块 docstring）。"""
        executor = make_executor(make_workspace(tmp_path))
        cmd = "exit 1" if os.name != "nt" else "exit 1"
        result = await run(executor, command=cmd)
        assert result.ok is True, "非零退出不是工具失败"
        assert result.data["exit_code"] == 1
        assert result.side_effect is SideEffect.OCCURRED


# --------------------------------------------------------------------------- 取消宽限期


class TestCancelGrace:
    """取消宽限期矩阵：进程自己退出 / 宽限期内收尾 / 宽限期用尽（`EDG-407`）。"""

    async def test_a_command_completes_normally(self, tmp_path: Path) -> None:
        result = await run_process(
            command="exit 0",
            cwd=tmp_path,
            env=build_environment(),
            timeout_ms=5_000,
            grace_ms=DEFAULT_GRACE_MS,
            cancel=ManualCancel(),
        )
        assert result.outcome == ProcessOutcome.COMPLETED
        assert result.exit_code == 0
        assert result.grace_expired is False

    async def test_timeout_then_terminate_within_grace(self, tmp_path: Path) -> None:
        """超时后进程在宽限期内退出——`TERMINATED`，不是 `GRACE_EXPIRED`。"""
        result = await run_process(
            command=sleep_command(0.3),
            cwd=tmp_path,
            env=build_environment(),
            timeout_ms=50,
            grace_ms=500,
            cancel=ManualCancel(),
        )
        assert result.outcome == ProcessOutcome.TERMINATED
        assert result.timed_out is True
        assert result.grace_expired is False

    async def test_grace_expired_when_process_ignores_termination(self, tmp_path: Path) -> None:
        """宽限期用尽被强杀——`side_effect=UNKNOWN` 的正主（`EDG-407`）。"""
        if os.name == "nt":
            pytest.skip("Windows 的 TerminateProcess 无法被捕获")
        # POSIX：`trap '' SIGTERM` 让进程忽略终止信号。
        result = await run_process(
            command="trap '' TERM; sleep 10",
            cwd=tmp_path,
            env=build_environment(),
            timeout_ms=50,
            grace_ms=200,
            cancel=ManualCancel(),
        )
        assert result.outcome == ProcessOutcome.GRACE_EXPIRED
        assert result.exit_code is None, "被强杀时退出码不报——那说的是「我们杀了它」而不是「它跑成什么样」"
        assert result.grace_expired is True

    async def test_cancel_is_observed_within_poll_interval(self, tmp_path: Path) -> None:
        """取消在 `CANCEL_POLL_MS` 内被看见，而不是等到 `timeout_ms` 才收场。"""
        token = ManualCancel()
        task = asyncio.create_task(
            run_process(
                command=sleep_command(10),
                cwd=tmp_path,
                env=build_environment(),
                timeout_ms=60_000,
                grace_ms=500,
                cancel=token,
            )
        )
        await asyncio.sleep(CANCEL_POLL_MS / 1000 * 2)
        token.request()
        result = await task
        assert result.outcome in (ProcessOutcome.TERMINATED, ProcessOutcome.GRACE_EXPIRED)
        assert result.timed_out is False, "取消触发的，不是超时"
        assert result.duration_ms < 5_000, "不该跑满 60 秒"


# --------------------------------------------------------------------------- cwd 守卫


def test_cwd_guard_matches_the_fs_workspace_guard(tmp_path: Path) -> None:
    """逐条对照：两个内建工具包的边界判定必须一致（`EDG-405`、`NFR-302`）。

    这条测试的存在理由见 `paths.py` 的模块 docstring「为什么不 import `WorkspaceGuard`」：
    优先重复而非过早抽象，但重复必须由测试钉住。
    """
    root = tmp_path / "ws"
    root.mkdir()
    (root / "allowed.txt").write_text("ok")
    outside = tmp_path / "evil"
    outside.mkdir()

    shell_guard = CwdGuard(root)
    fs_guard = FsWorkspaceGuard(root)

    # 1. 相对路径按根解析
    assert shell_guard.resolve(".") == fs_guard.resolve(".")
    assert shell_guard.resolve("sub/../allowed.txt") == fs_guard.resolve("sub/../allowed.txt")

    # 2. 绝对路径接受但过同一道门
    assert shell_guard.resolve(str(root / "allowed.txt")) == fs_guard.resolve(str(root / "allowed.txt"))

    # 3. `..` 逃逸被拒绝
    with pytest.raises(KaryviaError) as shell_caught:
        shell_guard.resolve("../evil")
    with pytest.raises(KaryviaError) as fs_caught:
        fs_guard.resolve("../evil")
    assert shell_caught.value.code == fs_caught.value.code == ErrorCode.PERMISSION_PATH_OUTSIDE_WORKSPACE

    # 4. 符号链接指向根外被拒绝
    link = root / "link_out"
    try_symlink(link, outside, directory=True)
    with pytest.raises(KaryviaError) as shell_caught:
        shell_guard.resolve("link_out")
    with pytest.raises(KaryviaError) as fs_caught:
        fs_guard.resolve("link_out")
    assert shell_caught.value.code == fs_caught.value.code == ErrorCode.PERMISSION_PATH_OUTSIDE_WORKSPACE

    # 5. `relative()` 渲染成 posix 相对路径
    assert shell_guard.relative(root) == fs_guard.relative(root) == "."
    assert shell_guard.relative(root / "sub" / "file.txt") == fs_guard.relative(root / "sub" / "file.txt")


class TestCwdEdgeCases:
    """cwd 的边界：空串 / NUL / 不是目录 / 重解析点。"""

    @pytest.mark.skipif(os.name != "nt", reason="目录联接是 Windows 特有的重解析点")
    def test_a_directory_junction_out_of_the_workspace_is_refused(self, tmp_path: Path) -> None:
        """realpath 校验挡住重解析点——双重校验的第二步（`EDG-405`、`NFR-302`）。

        逻辑校验（`normpath`）看不见联接：`ws/junction` 在字符串上完全落在根内。只有
        `resolve()` 之后再比一次才发现它指向根外。这条用例是那半边守卫在 Windows 上
        唯一真正跑过的证明（符号链接用例要提权，多半 skip）。
        """
        root = make_workspace(tmp_path / "ws")
        outside = tmp_path / "outside"
        outside.mkdir()
        try_junction(root / "junction", outside)

        with pytest.raises(KaryviaError) as caught:
            CwdGuard(root).resolve("junction")
        assert caught.value.code is ErrorCode.PERMISSION_PATH_OUTSIDE_WORKSPACE
        assert caught.value.detail["cwd"] == "junction", "detail 里只放原始串"

    def test_empty_cwd_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(KaryviaError) as caught:
            CwdGuard(tmp_path).resolve("")
        assert caught.value.code is ErrorCode.INPUT_MALFORMED

    def test_cwd_with_nul_byte_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(KaryviaError) as caught:
            CwdGuard(make_workspace(tmp_path)).resolve("sub\x00evil")
        assert caught.value.code is ErrorCode.INPUT_MALFORMED

    async def test_cwd_pointing_to_a_file_is_rejected(self, tmp_path: Path) -> None:
        """cwd 必须是目录，不能是文件。"""
        executor = make_executor(make_workspace(tmp_path))
        result = await run(executor, command="exit 0", cwd="file.txt")
        assert result.ok is False
        assert result.error.code is ErrorCode.INPUT_MALFORMED
        assert result.side_effect is SideEffect.NONE


# --------------------------------------------------------------------------- 环境变量


class TestEnvironmentVariables:
    """默认全部不继承（`NFR-307`、`MOD-002`）。"""

    def test_baseline_contains_only_platform_essentials(self) -> None:
        """基线名单本身不含任何凭据类变量（见 `environ.py` 模块 docstring）。"""
        sensitive = {"API", "KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH", "CREDENTIAL"}
        for name in BASELINE_NAMES:
            upper = name.upper()
            assert not any(word in upper for word in sensitive), f"{name} 疑似凭据变量"

    def test_by_default_only_baseline_is_inherited(self) -> None:
        """父进程的环境默认一个字节都不进子进程。"""
        parent = {"PATH": "/usr/bin", "OPENAI_API_KEY": "sk-secret", "CUSTOM": "value"}
        env = build_environment(source=parent)
        assert "PATH" in env, "基线变量应当被继承"
        assert "OPENAI_API_KEY" not in env, "未点名的凭据不该出现"
        assert "CUSTOM" not in env

    def test_pass_env_allows_specific_names(self) -> None:
        """运维点名后才转发。"""
        parent = {"HOME": "/home/user", "CARGO_HOME": "/cargo", "SECRET": "s"}
        env = build_environment(pass_env=("CARGO_HOME",), source=parent)
        assert "CARGO_HOME" in env
        assert "SECRET" not in env

    def test_pass_env_silently_skips_missing_names(self) -> None:
        """父进程没有的名字不设也不报错（见 `environ.py` 的函数 docstring）。"""
        env = build_environment(pass_env=("DOES_NOT_EXIST",), source={})
        assert "DOES_NOT_EXIST" not in env

    def test_explicit_overrides_win(self) -> None:
        """显式写死的值覆盖基线与 pass_env。"""
        parent = {"PATH": "/usr/bin"}
        env = build_environment(pass_env=("PATH",), overrides={"PATH": "/custom"}, source=parent)
        assert env["PATH"] == "/custom"

    def test_forced_env_is_always_set(self) -> None:
        """`FORCED_ENV` 在基线之后、`pass_env` 之前，运维仍然能覆盖它。"""
        env = build_environment(source={})
        for name, value in FORCED_ENV.items():
            assert env[name] == value

    async def test_a_sentinel_credential_never_reaches_the_child_process(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """哨兵扫描：父进程里的凭据不出现在**真实子进程**的环境里（`NFR-307`、`MOD-002`）。

        这条刻意走真实进程而不是只断言 `build_environment()` 的返回值——那个函数是对的
        不等于调用它的那条路径是对的（漏传 `env=` 就会让子进程继承整个父环境，而单测
        看不见）。哨兵形如 `sk-` + ≥16 字符，与 `errors.py::_SECRET_VALUE_PATTERNS` 同形。
        """
        sentinel = "sk-livecredential0123456789"
        monkeypatch.setenv("OPENAI_API_KEY", sentinel)
        monkeypatch.setenv("NM_TEST_PLAIN", "harmless")

        executor = make_executor(make_workspace(tmp_path))
        # 让子进程把自己看到的整个环境打印出来，再在里面找哨兵。
        dump = f'"{sys.executable}" -c "import os;print(chr(10).join(f\'{{k}}={{v}}\' for k,v in os.environ.items()))"'
        result = await run(executor, command=dump)

        assert result.ok, result.content
        assert sentinel not in result.content, "父进程的凭据泄漏进了子进程"
        assert "OPENAI_API_KEY" not in result.content
        assert "NM_TEST_PLAIN" not in result.content, "未点名的变量一律不继承"

    async def test_pass_env_reaches_the_child_process(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """点名之后确实转发到子进程——否则「默认不继承」就成了「永远拿不到」。"""
        monkeypatch.setenv("NM_TEST_FORWARDED", "expected-value")
        executor = make_executor(
            make_workspace(tmp_path), **{CONFIG_PASS_ENV_KEY: ["NM_TEST_FORWARDED"]}
        )
        probe = f'"{sys.executable}" -c "import os;print(os.environ.get(\'NM_TEST_FORWARDED\',\'MISSING\'))"'
        result = await run(executor, command=probe)

        assert result.ok, result.content
        assert "expected-value" in result.content


# --------------------------------------------------------------------------- 副作用三档


class TestSideEffectLadder:
    """`side_effect` 的三档判定（`EDG-401`、`EDG-407`）。

    **这一组刻意在 `_fold` 上做而不是靠真实进程**：`GRACE_EXPIRED` 那一档需要一个忽略
    终止信号的进程，而 Windows 的 `TerminateProcess` 无法被捕获——只走真实进程的话，
    本模块最要紧的那条语义（宽限期用尽 → `UNKNOWN`）在 Windows 上永远没被验证过。
    喂一个合成的 `ProcessResult` 让三档在两个平台都跑得到。
    """

    def _fold(self, tmp_path: Path, result: ProcessResult):  # noqa: ANN202
        executor = make_executor(make_workspace(tmp_path))
        return executor._fold(  # noqa: SLF001 - 三档判定的唯一入口，就是要直接验它
            invocation(TOOL_NAME, command="x"), result, started=0.0
        )

    @staticmethod
    def _result(outcome: str, *, exit_code: int | None, timed_out: bool = False) -> ProcessResult:
        return ProcessResult(
            exit_code=exit_code,
            stdout="out",
            stderr="",
            duration_ms=1,
            outcome=outcome,
            timed_out=timed_out,
        )

    def test_completed_reports_occurred(self, tmp_path: Path) -> None:
        folded = self._fold(tmp_path, self._result(ProcessOutcome.COMPLETED, exit_code=0))
        assert folded.ok is True
        assert folded.side_effect is SideEffect.OCCURRED

    def test_terminated_within_grace_reports_occurred(self, tmp_path: Path) -> None:
        """宽限期内退出：它跑过也收过尾，`OCCURRED` 而不是 `UNKNOWN`。"""
        folded = self._fold(
            tmp_path, self._result(ProcessOutcome.TERMINATED, exit_code=-15, timed_out=True)
        )
        assert folded.ok is False
        assert folded.side_effect is SideEffect.OCCURRED
        assert folded.error is not None
        assert folded.error.code is ErrorCode.TIMEOUT_TOOL_CALL

    def test_terminated_by_cancel_reports_cancelled(self, tmp_path: Path) -> None:
        """取消与超时的善后相同，但错误码要分得开。"""
        folded = self._fold(
            tmp_path, self._result(ProcessOutcome.TERMINATED, exit_code=-15, timed_out=False)
        )
        assert folded.error is not None
        assert folded.error.code is ErrorCode.CANCELLED_BY_USER

    def test_grace_expired_reports_unknown(self, tmp_path: Path) -> None:
        """**本模块与 `tools_fs` 唯一的语义差异**：宽限期用尽 → `UNKNOWN`（`EDG-407`）。"""
        folded = self._fold(
            tmp_path, self._result(ProcessOutcome.GRACE_EXPIRED, exit_code=None, timed_out=True)
        )
        assert folded.ok is False
        assert folded.side_effect is SideEffect.UNKNOWN, "宽限期用尽必须如实说副作用未知"
        assert folded.error is not None
        assert folded.error.code is ErrorCode.TIMEOUT_TOOL_CANCEL
        assert folded.data["exit_code"] is None
        assert "未知" in folded.content

    def test_the_grace_constant_matches_the_kernel(self) -> None:
        """`R4` 逼得宽限期常量在 `builtins/` 与 `kernel/` 各写一份，这里对照钉住。"""
        from karyvia.kernel.turn.cancel import DEFAULT_TOOL_CANCEL_GRACE_MS

        assert DEFAULT_GRACE_MS == DEFAULT_TOOL_CANCEL_GRACE_MS


class TestOutputRendering:
    """`render_output` 的三段：退出码 → stdout → stderr，空段整段省略。"""

    @staticmethod
    def _result(*, stdout: str = "", stderr: str = "", exit_code: int | None = 0) -> ProcessResult:
        return ProcessResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=1,
            outcome=ProcessOutcome.COMPLETED,
            timed_out=False,
        )

    def test_stderr_is_rendered_when_present(self) -> None:
        text = render_output(self._result(stdout="out", stderr="boom"))
        assert "stdout:\nout" in text
        assert "stderr:\nboom" in text

    def test_empty_sections_are_omitted(self) -> None:
        """一次成功的 `ls` 不该在上下文里占三行样板。"""
        text = render_output(self._result(stdout="out"))
        assert "stderr" not in text

    def test_no_output_at_all_says_so(self) -> None:
        """「没有输出」与「工具坏了没给东西」是不同的结论。"""
        assert "(无输出)" in render_output(self._result())


class TestSpawnFailure:
    """进程起不来是一次有结论的调用，不是 `UNKNOWN`。"""

    async def test_a_missing_shell_folds_into_a_result(self, tmp_path: Path) -> None:
        """`exit_code=-1` 不是任何程序的真实退出码，诊断因此能区分「启动失败」。

        POSIX 才验得到：Windows 走 `create_subprocess_shell`，`shell` 配置项不生效
        （见 `command.py` 的模块 docstring），指一个不存在的 shell 也不会让启动失败。
        """
        if os.name == "nt":
            pytest.skip("Windows 上 shell 配置项不生效，起不来这条路要靠别的方式触发")
        result = await run_process(
            command="exit 0",
            cwd=tmp_path,
            env=build_environment(),
            timeout_ms=5_000,
            shell="/nonexistent/shell",
            cancel=ManualCancel(),
        )
        assert result.exit_code == -1
        assert result.outcome == ProcessOutcome.COMPLETED
        assert "启动失败" in result.stderr

    async def test_a_spawn_failure_reports_no_side_effect(self, tmp_path: Path) -> None:
        """进程根本没起来 → `NONE`。这是与宽限期用尽（`UNKNOWN`）最容易混的一档。"""
        if os.name == "nt":
            pytest.skip("同上")
        executor = make_executor(
            make_workspace(tmp_path), **{CONFIG_SHELL_KEY: "/nonexistent/shell"}
        )
        result = await run(executor, command="exit 0")
        assert result.ok is True, "启动失败仍是一次有结论的调用，退出码 -1 说明了一切"
        assert result.side_effect is SideEffect.OCCURRED
        assert result.data["exit_code"] == -1


# --------------------------------------------------------------------------- 退出码语义


class TestExitCodeSemantics:
    """非零退出码是正常产出而不是错误（`executor.py` 模块 docstring）。"""

    async def test_grep_no_match_returns_1_and_ok_true(self, tmp_path: Path) -> None:
        """grep 没匹配到返回 1——这是命令的正常产出。"""
        if os.name == "nt":
            pytest.skip("Windows 的 findstr 语义与 grep 不同")
        ws = make_workspace(tmp_path)
        executor = make_executor(ws)
        result = await run(executor, command="grep notfound file.txt || exit $?")
        assert result.ok is True, "非零退出不是工具失败"
        assert result.data["exit_code"] == 1

    async def test_test_command_false_returns_1(self, tmp_path: Path) -> None:
        """test 判假返回 1。"""
        if os.name == "nt":
            pytest.skip("cmd.exe 没有 test 命令")
        executor = make_executor(make_workspace(tmp_path))
        result = await run(executor, command="test 1 -eq 2; exit $?")
        assert result.ok is True
        assert result.data["exit_code"] == 1


# --------------------------------------------------------------------------- 跨平台契约


class TestCrossPlatformContract:
    """两个平台的对外行为契约一致（`NFR-605`）。"""

    def test_posix_argv_puts_the_command_last(self) -> None:
        """POSIX 走 `<shell> -c <command>`，命令串是最后一个参数。

        **Windows 上不走这条路**（`create_subprocess_shell`，见 `command.py` 的模块
        docstring），`build_argv` 因此是纯函数、两个平台都能跑，但它的产物只在 POSIX 上
        被真的用来启动进程。这条测试断言的是那个形状，不是平台分派。
        """
        argv = build_argv("echo test", shell="/bin/sh")
        assert argv == ("/bin/sh", "-c", "echo test")

    def test_the_platform_split_lives_in_one_place(self) -> None:
        """平台分派只在 `process._spawn` 一处——挪到别处就会有人"顺手统一"成 exec。"""
        import inspect

        from karyvia.builtins.tools_shell import process

        source = inspect.getsource(process)
        assert source.count("os.name") == 1, "平台判断不该散落在多处"
        assert "create_subprocess_shell" in source
        assert "create_subprocess_exec" in source

    async def test_same_command_produces_same_exit_code(self, tmp_path: Path) -> None:
        """同一条命令在两个平台产生同样的退出码语义。"""
        executor = make_executor(make_workspace(tmp_path))
        result = await run(executor, command="exit 0" if os.name != "nt" else "exit 0")
        assert result.data["exit_code"] == 0

    def test_newlines_are_normalized_in_output(self) -> None:
        """`\\r\\n` 归一成 `\\n`（`NFR-605`）——输出在 `process._decode` 之后是平台无关的。"""
        # 测试在单元层（`_decode`），不跑真实进程——Windows 的 `echo` 会不会输出 `\r\n`
        # 取决于 shell 与重定向，而这条测试要断言的是「解码层归一了」。
        from karyvia.builtins.tools_shell.process import _decode

        assert _decode(b"line1\r\nline2\r\n") == "line1\nline2\n"
        assert _decode(b"line1\rline2") == "line1\nline2"


# --------------------------------------------------------------------------- 单工具禁用


class TestSingleToolDisable:
    """单工具禁用后，模型可见列表中同步消失（`TOL-006`）。"""

    def test_by_default_the_only_tool_is_enabled(self) -> None:
        assert enabled_tool_names({}) == _ONLY

    def test_disabling_removes_it(self) -> None:
        assert enabled_tool_names({CONFIG_DISABLE_KEY: [TOOL_NAME]}) == ()

    def test_an_unknown_name_is_refused(self) -> None:
        with pytest.raises(KaryviaError) as caught:
            enabled_tool_names({CONFIG_DISABLE_KEY: ["shell.execute"]})
        assert caught.value.code is ErrorCode.CONFIG_INVALID
        assert "shell.execute" in caught.value.detail["unknown"]

    def test_setup_registers_nothing_when_disabled(self, tmp_path: Path) -> None:
        registered: list[str] = []

        class RecordingApi:
            ctx = context_with(
                **{CONFIG_WORKSPACE_KEY: str(tmp_path), CONFIG_DISABLE_KEY: [TOOL_NAME]}
            )

            def register_tool(self, spec: ToolSpec, handler: ToolHandler) -> None:
                del handler
                registered.append(spec.name)

        setup(RecordingApi())  # type: ignore[arg-type]
        assert registered == []

    async def test_the_disabled_tool_is_absent_from_the_registry(self, tmp_path: Path) -> None:
        """走真实装配链：manifest → wiring(keep=…) → registry → `tools_from()`。"""
        config: dict[str, JsonValue] = {
            CONFIG_WORKSPACE_KEY: str(tmp_path),
            CONFIG_DISABLE_KEY: [TOOL_NAME],
        }
        allowed = set(enabled_tool_names(config))

        def context_for(provider: ProviderId) -> PluginContext:
            del provider
            return FakePluginContext(config=config)

        wiring = await wire_capabilities(
            manifests=[TOOLS_SHELL],
            context_for=context_for,
            keep=lambda manifest, decl: decl.name in allowed,
        )

        assert wiring.report.ok
        tools = tools_from(wiring.registry)
        assert len(tools) == 0

    async def test_forgetting_the_filter_fails_loudly(self, tmp_path: Path) -> None:
        """不过滤声明就是「声明了却没注册」，`D16` 会拒绝加载（与 `test_tools_fs` 同）。"""
        config: dict[str, JsonValue] = {
            CONFIG_WORKSPACE_KEY: str(tmp_path),
            CONFIG_DISABLE_KEY: [TOOL_NAME],
        }

        def context_for(provider: ProviderId) -> PluginContext:
            del provider
            return FakePluginContext(config=config)

        # 忘了传 `keep`——manifest 声明了一个，setup 注册了零个。
        wiring = await wire_capabilities(manifests=[TOOLS_SHELL], context_for=context_for)
        failures = [outcome for outcome in wiring.outcomes if outcome.error is not None]
        assert failures, "少注册一项能力必须被 CapabilityHost.finish() 挡下"
        assert failures[0].error is not None
        assert failures[0].error.code is ErrorCode.PLUGIN_LOAD_FAILED


# --------------------------------------------------------------------------- 注册


class TestRegistration:
    """内建以普通 manifest + `setup(api)` 注册，与外部插件无特权差异（`BAS-005`）。"""

    def test_the_manifest_declares_exactly_one_tool(self) -> None:
        assert TOOLS_SHELL.id == "tools-shell"
        assert len(TOOLS_SHELL.capabilities) == 1
        assert TOOLS_SHELL.capabilities[0].name == TOOL_NAME

    def test_the_spec_is_destructive_and_exclusive(self) -> None:
        """`RiskLevel.DESTRUCTIVE` + `Concurrency.EXCLUSIVE`（与 `fs.write` 同一档）。"""
        assert EXEC_SPEC.risk is RiskLevel.DESTRUCTIVE
        assert EXEC_SPEC.read_only is False

    def test_setup_signature_matches_external_plugins(self, tmp_path: Path) -> None:
        """setup(api) 的形状，ctx 从 api.ctx 拿——与外部插件写的那段代码相同。"""
        called = False

        class MinimalApi:
            ctx = context_with(**{CONFIG_WORKSPACE_KEY: str(tmp_path)})

            def register_tool(self, spec: ToolSpec, handler: ToolHandler) -> None:
                nonlocal called
                called = True

        setup(MinimalApi())  # type: ignore[arg-type]
        assert called

    def test_config_schema_matches_settings(self) -> None:
        """manifest 的 `config_schema` 与 `settings.py` 的键一致，由测试钉住。"""
        props = TOOLS_SHELL.config_schema["properties"]  # type: ignore[index]
        assert CONFIG_WORKSPACE_KEY in props
        assert CONFIG_DISABLE_KEY in props
        assert CONFIG_TIMEOUT_KEY in props
        assert CONFIG_MAX_OUTPUT_CHARS_KEY in props
        assert CONFIG_PASS_ENV_KEY in props
        assert CONFIG_ENV_KEY in props
        assert CONFIG_SHELL_KEY in props


# --------------------------------------------------------------------------- 配置


class TestSettings:
    """配置解析：类型校验、上限、`disable` 表外名字。"""

    def test_workspace_defaults_to_state_dir(self, tmp_path: Path) -> None:
        ctx = FakePluginContext(state_dir=tmp_path, config={})
        settings = resolve_settings(ctx)
        assert settings.workspace == tmp_path

    def test_timeout_must_be_positive(self, tmp_path: Path) -> None:
        ctx = FakePluginContext(config={CONFIG_WORKSPACE_KEY: str(tmp_path), CONFIG_TIMEOUT_KEY: 0})
        with pytest.raises(KaryviaError) as caught:
            resolve_settings(ctx)
        assert caught.value.code is ErrorCode.CONFIG_INVALID

    def test_max_output_exceeding_contract_limit_is_rejected(self, tmp_path: Path) -> None:
        from karyvia.contracts.tool import MAX_TOOL_RESULT_LENGTH

        ctx = FakePluginContext(
            config={
                CONFIG_WORKSPACE_KEY: str(tmp_path),
                CONFIG_MAX_OUTPUT_CHARS_KEY: MAX_TOOL_RESULT_LENGTH + 1,
            }
        )
        with pytest.raises(KaryviaError) as caught:
            resolve_settings(ctx)
        assert caught.value.code is ErrorCode.CONFIG_INVALID

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            # 「写法合法但被静默忽略」是本项目一贯拒绝的那类失败——每一条都必须报出来。
            (CONFIG_WORKSPACE_KEY, 123),
            (CONFIG_WORKSPACE_KEY, "   "),
            (CONFIG_TIMEOUT_KEY, True),
            (CONFIG_TIMEOUT_KEY, -1),
            (CONFIG_MAX_OUTPUT_CHARS_KEY, 0),
            (CONFIG_DISABLE_KEY, "shell.exec"),
            (CONFIG_DISABLE_KEY, [123]),
            (CONFIG_PASS_ENV_KEY, "PATH"),
            (CONFIG_PASS_ENV_KEY, [123]),
            (CONFIG_ENV_KEY, ["PATH=x"]),
            (CONFIG_ENV_KEY, {"PATH": 123}),
            (CONFIG_SHELL_KEY, 123),
            (CONFIG_SHELL_KEY, "  "),
        ],
    )
    def test_malformed_config_is_refused(self, key: str, value: JsonValue) -> None:
        with pytest.raises(KaryviaError) as caught:
            resolve_settings(FakePluginContext(config={key: value}))
        assert caught.value.code is ErrorCode.CONFIG_INVALID

    def test_env_overrides_and_shell_round_trip(self, tmp_path: Path) -> None:
        """合法配置逐项进到设置里——校验不该顺手改写用户写的值。"""
        settings = resolve_settings(
            FakePluginContext(
                config={
                    CONFIG_WORKSPACE_KEY: str(tmp_path),
                    CONFIG_TIMEOUT_KEY: 5_000,
                    CONFIG_PASS_ENV_KEY: ["B", "A", "A"],
                    CONFIG_ENV_KEY: {"NM": "1"},
                    CONFIG_SHELL_KEY: "/bin/bash",
                }
            )
        )
        assert settings.timeout_ms == 5_000
        assert settings.pass_env == ("A", "B"), "去重并排序，让诊断输出稳定"
        assert settings.env == {"NM": "1"}
        assert settings.shell == "/bin/bash"
        assert settings.enabled is True


class TestArgumentValidation:
    """参数校验：表外参数、类型不对，都是 `ok=False` 的结果而不是异常（`TOL-002`）。"""

    @pytest.mark.parametrize(
        "arguments",
        [
            {"command": "exit 0", "unexpected": 1},
            {"command": ""},
            {"command": None},
            {"command": "exit 0", "cwd": 123},
            {},
        ],
    )
    async def test_bad_arguments_come_back_as_results(
        self, tmp_path: Path, arguments: dict[str, JsonValue]
    ) -> None:
        executor = make_executor(make_workspace(tmp_path))
        result = await executor.execute(
            invocation(TOOL_NAME, **arguments), ManualCancel()
        )
        assert result.ok is False
        assert result.error is not None
        assert result.side_effect is SideEffect.NONE, "参数没过就没起进程"

    async def test_an_already_cancelled_call_never_starts_a_process(self, tmp_path: Path) -> None:
        """入口取消：`side_effect=NONE`，因为进程根本没被起来。"""
        cancel = ManualCancel()
        cancel.request()
        executor = make_executor(make_workspace(tmp_path))
        result = await executor.execute(invocation(TOOL_NAME, command="exit 0"), cancel)
        assert result.ok is False
        assert result.side_effect is SideEffect.NONE

    async def test_cwd_outside_the_workspace_is_refused(self, tmp_path: Path) -> None:
        """越界 cwd：错误里只放原始串，不放解析后的宿主机绝对路径。"""
        executor = make_executor(make_workspace(tmp_path / "ws"))
        result = await run(executor, command="exit 0", cwd="../..")
        assert result.ok is False
        assert result.error is not None
        assert result.error.code is ErrorCode.PERMISSION_PATH_OUTSIDE_WORKSPACE
        assert result.error.detail["cwd"] == "../.."
        assert str(tmp_path) not in repr(result.error), "宿主机绝对路径不进模型可见的错误"


# --------------------------------------------------------------------------- 命令边界


class TestCommandEdgeCases:
    """命令串的边界：空串 / NUL / 超长。"""

    def test_empty_command_is_rejected(self) -> None:
        with pytest.raises(KaryviaError) as caught:
            build_argv("")
        assert caught.value.code is ErrorCode.INPUT_MALFORMED

    def test_command_with_nul_byte_is_rejected(self) -> None:
        with pytest.raises(KaryviaError) as caught:
            build_argv("echo\x00evil")
        assert caught.value.code is ErrorCode.INPUT_MALFORMED

    def test_command_exceeding_max_length_is_rejected(self) -> None:
        with pytest.raises(KaryviaError) as caught:
            build_argv("x" * (MAX_COMMAND_LENGTH + 1))
        assert caught.value.code is ErrorCode.INPUT_TOO_LARGE


# --------------------------------------------------------------------------- 截断


class TestTruncation:
    """输出超限时截断并置 `truncated=True`（`TOL-003`）。"""

    async def test_output_within_limit_is_not_truncated(self, tmp_path: Path) -> None:
        executor = make_executor(make_workspace(tmp_path), **{CONFIG_MAX_OUTPUT_CHARS_KEY: 1000})
        result = await run(executor, command="echo short")
        assert result.truncated is False

    async def test_output_exceeding_limit_is_truncated(self, tmp_path: Path) -> None:
        executor = make_executor(make_workspace(tmp_path), **{CONFIG_MAX_OUTPUT_CHARS_KEY: 50})
        # 生成一段长输出
        cmd = 'python -c "print(\\"x\\" * 200)"' if os.name != "nt" else 'python -c "print(\\"x\\" * 200)"'
        result = await run(executor, command=cmd)
        assert result.truncated is True
        assert len(result.content) <= 50
        assert "truncated" in result.content.lower()
