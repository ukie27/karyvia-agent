"""`shell.exec`：在 workspace 内执行一条 shell 命令。

职责：`shell.exec` 的 `ToolSpec` 与 `ToolHandler` 实现——参数校验、cwd 判定、调用
`process.run_process()`、把产出折成 `ToolResult`。
不负责：进程与取消宽限期（`process.py`）、argv 构造（`command.py`）、环境变量
（`environ.py`）、cwd 边界判定（`paths.py`）、注册（`registration.py`）。

**`side_effect` 的三档判定只在这一处**：

| 收场 | `side_effect` | 依据 |
|---|---|---|
| 执行之前失败（参数非法、cwd 越界、命令为空） | `NONE` | 进程根本没起来，外部世界没变 |
| 进程自己退出（任何退出码）/ 宽限期内被终止 | `OCCURRED` | 命令跑过了，它做了什么由它自己决定 |
| 宽限期用尽被强杀 | `UNKNOWN` | 写了一半文件？改了一半配置？不知道 |

**这与 `tools_fs.FsTool` 那句「折出来的失败一律 `NONE`」正相反，不要照抄**。文件工具的
失败全部发生在真正落盘之前（临时文件 + `os.replace`），因此它一次 `UNKNOWN` 都不产出；
而这里第三行正是 `UNKNOWN` 存在的理由。谎报 `NONE` 会让用户据此重试并造成重复副作用。

**非零退出码不是工具失败**（`ok=True`）。`grep` 没匹配到返回 1、`test` 判假返回 1、
编译器发现错误返回 2——这些都是命令的**正常产出**，模型需要拿到退出码和 stderr 才能
继续工作。把它折成 `ok=False` 会让 Kernel 认为工具坏了，而真正坏掉的路径（启动不了、
超时、被强杀）在这里各有各的错误码。`ok=False` 只留给「这次调用没能给出结论」。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Final

from karyvia.contracts import (
    CancelSignal,
    Concurrency,
    ErrorCode,
    JsonValue,
    KaryviaError,
    RiskLevel,
    SideEffect,
    ToolInvocation,
    ToolResult,
    ToolSpec,
    TrustLevel,
)
from karyvia.contracts.tool import MAX_TOOL_RESULT_LENGTH

from .command import MAX_COMMAND_LENGTH
from .environ import build_environment
from .paths import CwdGuard
from .process import (
    DEFAULT_GRACE_MS,
    ProcessOutcome,
    ProcessResult,
    effective_timeout_ms,
    run_process,
)
from .settings import ShellToolSettings

__all__ = ["EXEC_SPEC", "ShellExecutor", "render_output"]

_EXEC_ARGUMENTS: Final = ("command", "cwd")

#: 截断标记。与 `tools_fs.content` 的那份形状一致（两个工具包给模型看到的
#: 截断提示是同一种），但各写一份——两个内建是各自独立的提供方，见 `paths.py` 的理由。
_TRUNCATION_MARKER: Final = "\n… [truncated: 已显示 {shown}/{total} 字符]"

EXEC_SPEC: Final = ToolSpec(
    name="shell.exec",
    description=(
        "在 workspace 内执行一条 shell 命令并返回退出码、stdout 与 stderr。"
        "POSIX 上交给 sh -c，Windows 上交给 cmd.exe /c。"
        "非零退出码是正常产出而不是错误；输出过长时截断并标注。"
        "命令默认在 workspace 根执行，可用 cwd 指定根内的子目录。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "maxLength": MAX_COMMAND_LENGTH,
                "description": "要执行的命令串，交给平台 shell 解释。",
            },
            "cwd": {
                "type": "string",
                "default": ".",
                "description": "工作目录，相对 workspace 根。省略表示根本身；越界会被拒绝。",
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    },
    # 只读为假、风险最高档：一条命令能删掉整个 workspace，而的确认策略要拦的
    # 正是这一档。「大多数命令其实只是 ls」不构成降级理由——降级意味着**所有**命令都不
    # 再被确认策略看见。
    read_only=False,
    risk=RiskLevel.DESTRUCTIVE,
    # 两条命令并发跑的结果取决于顺序（一条写文件、另一条读它），而 turn 内的并行调度
    # 不保证顺序。与 `fs.write` / `fs.edit` 同一条理由。
    concurrency=Concurrency.EXCLUSIVE,
)


def _malformed(message: str, **detail: object) -> KaryviaError:
    return KaryviaError(ErrorCode.INPUT_MALFORMED, message, detail=detail)


def _require_str(arguments: Mapping[str, JsonValue], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value:
        raise _malformed(
            "缺少必填参数或类型不对（应为非空字符串）。",
            argument=key,
            actual_type=type(value).__name__,
        )
    return value


def _optional_str(arguments: Mapping[str, JsonValue], key: str, default: str) -> str:
    value = arguments.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise _malformed(
            "参数类型不对（应为字符串）。", argument=key, actual_type=type(value).__name__
        )
    return value


def _reject_unknown(arguments: Mapping[str, JsonValue]) -> None:
    """表外参数是错误，不是可以忽略的多余字段。

    Kernel 的 `ToolInvoker` 已经按 schema 校验过一遍（`additionalProperties: false`），
    这里再挡一次是因为 `ToolHandler` 是公开契约：插件作者可以直接调它，`sdk.testing` 的
    `ToolContract` 也直接调它。只在一处校验等于把「谁负责校验」的答案藏进调用链。
    """
    unknown = sorted(set(arguments) - set(_EXEC_ARGUMENTS))
    if unknown:
        raise _malformed("出现了未知参数。", unknown=unknown, allowed=sorted(_EXEC_ARGUMENTS))


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    """收进 `limit` 字符内，返回 `(文本, 是否截断)`。

    **标记算在上限里**：返回值长度恒 ≤ `limit`。先截到上限再拼标记会让结果比上限长，
    而上限的下游是契约的 `MAX_TOOL_RESULT_LENGTH`——那里超一个字符就构造失败。
    """
    total = len(text)
    if total <= limit:
        return text, False
    keep = limit - len(_TRUNCATION_MARKER.format(shown=limit, total=total))
    if keep <= 0:
        return "", True
    return text[:keep] + _TRUNCATION_MARKER.format(shown=keep, total=total), True


def render_output(result: ProcessResult) -> str:
    """把一次执行渲染成模型可读的文本。

    三段固定顺序：退出码 → stdout → stderr。**空的那段整段省略**而不是留一个
    `stderr:\\n(空)`——一次成功的 `ls` 不该在上下文里占三行样板。两段都空时明确说
    「无输出」，那与「工具坏了没给东西」是不同的结论。
    """
    lines: list[str] = []
    if result.grace_expired:
        lines.append("exit: (未知——进程在取消宽限期内没有退出，已强制终止)")
    else:
        lines.append(f"exit: {result.exit_code}")
    if result.stdout:
        lines.append(f"stdout:\n{result.stdout.rstrip()}")
    if result.stderr:
        lines.append(f"stderr:\n{result.stderr.rstrip()}")
    if not result.stdout and not result.stderr:
        lines.append("(无输出)")
    return "\n".join(lines)


class ShellExecutor:
    """`shell.exec` 的 handler。

    **约定不抛**：逸出的异常会被 Kernel 记成 `side_effect=UNKNOWN`，而这里
    大多数失败发生在进程启动之前，那时 `NONE` 才是实话。折叠在 `execute()` 一处完成。
    """

    __slots__ = ("_grace_ms", "_guard", "_settings")

    def __init__(
        self,
        guard: CwdGuard,
        settings: ShellToolSettings,
        *,
        grace_ms: int = DEFAULT_GRACE_MS,
    ) -> None:
        self._guard = guard
        self._settings = settings
        self._grace_ms = grace_ms

    @property
    def guard(self) -> CwdGuard:
        return self._guard

    @property
    def settings(self) -> ShellToolSettings:
        return self._settings

    async def execute(self, invocation: ToolInvocation, cancel: CancelSignal) -> ToolResult:
        """执行一次调用。**约定不抛**，见类 docstring。

        **取消语义**：入口检查一次，执行期间由 `process.run_process()` 轮询；取消或超时
        都走「终止信号 → 宽限期 → 强杀」，宽限期用尽时 `side_effect=UNKNOWN`。
        """
        started = time.perf_counter()
        call_id = invocation.call.call_id
        limit = self._settings.max_output_chars
        try:
            cancel.raise_if_requested()
            return await self._run(invocation, cancel, started)
        except KaryviaError as error:
            # 走到这里说明进程还没起来（参数非法、cwd 越界、入口取消）——`NONE` 是实话。
            return self._failure(call_id, error, limit=limit, started=started)
        except OSError as error:
            folded = KaryviaError(
                ErrorCode.KERNEL_UNEXPECTED,
                "执行命令时发生系统错误。",
                detail={"errno": error.errno, "reason": type(error).__name__},
            )
            return self._failure(call_id, folded, limit=limit, started=started)

    async def _run(
        self, invocation: ToolInvocation, cancel: CancelSignal, started: float
    ) -> ToolResult:
        arguments = invocation.call.arguments
        _reject_unknown(arguments)
        command = _require_str(arguments, "command")
        cwd = self._guard.resolve(_optional_str(arguments, "cwd", "."))
        if not cwd.is_dir():
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                "cwd 不是一个目录。",
                detail={"cwd": self._guard.relative(cwd)},
            )

        result = await run_process(
            command=command,
            cwd=cwd,
            env=build_environment(
                pass_env=self._settings.pass_env, overrides=self._settings.env
            ),
            timeout_ms=effective_timeout_ms(
                invocation_timeout=invocation.timeout_ms,
                config_timeout=self._settings.timeout_ms,
            ),
            grace_ms=self._grace_ms,
            shell=self._settings.shell,
            cancel=cancel,
        )
        return self._fold(invocation, result, started=started)

    def _fold(
        self, invocation: ToolInvocation, result: ProcessResult, *, started: float
    ) -> ToolResult:
        """把一次执行折成 `ToolResult`。`side_effect` 的三档判定在这里，见模块 docstring。"""
        limit = self._settings.max_output_chars
        content, truncated = _truncate(render_output(result), limit)
        data: dict[str, JsonValue] = {
            "exit_code": result.exit_code,
            "outcome": result.outcome,
            "cwd": self._guard.relative(self._guard.root),
            "duration_ms": result.duration_ms,
        }

        if result.grace_expired:
            # 宽限期用尽被强杀：`UNKNOWN` 的正主。错误码按触发原因分——
            # 超时与用户取消的善后动作相同，但诊断要分得开。
            error = KaryviaError(
                ErrorCode.TIMEOUT_TOOL_CANCEL,
                "命令在取消宽限期内没有退出，已强制终止；副作用未知。",
                detail={
                    "grace_ms": self._grace_ms,
                    "timed_out": result.timed_out,
                    "duration_ms": result.duration_ms,
                },
            )
            return ToolResult(
                call_id=invocation.call.call_id,
                ok=False,
                content=content,
                truncated=truncated,
                side_effect=SideEffect.UNKNOWN,
                error=error,
                data=data,
                duration_ms=result.duration_ms,
            )

        if result.outcome == ProcessOutcome.TERMINATED:
            # 被终止但在宽限期内退出：进程跑过、也收过尾，但做没做完不知道。
            # 它**确实**产生了副作用（至少跑了一段），因此 `OCCURRED` 而不是 `UNKNOWN`。
            error = KaryviaError(
                ErrorCode.TIMEOUT_TOOL_CALL if result.timed_out else ErrorCode.CANCELLED_BY_USER,
                "命令被中断，已在宽限期内退出。",
                detail={"duration_ms": result.duration_ms, "timed_out": result.timed_out},
            )
            return ToolResult(
                call_id=invocation.call.call_id,
                ok=False,
                content=content,
                truncated=truncated,
                side_effect=SideEffect.OCCURRED,
                error=error,
                data=data,
                duration_ms=result.duration_ms,
            )

        # 正常退出。**非零退出码是正常产出**（见模块 docstring），因此 `ok=True`。
        return ToolResult(
            call_id=invocation.call.call_id,
            ok=True,
            content=content,
            truncated=truncated,
            side_effect=SideEffect.OCCURRED,
            data=data,
            duration_ms=result.duration_ms,
        )

    def _failure(
        self, call_id: str, error: KaryviaError, *, limit: int, started: float
    ) -> ToolResult:
        """执行之前的失败：进程没起来，`side_effect=NONE` 是实话。

        **`trust=SYSTEM`**：正文是本层自己写的错误文案，一个字节的子进程输出都没有
        （进程压根没起来）。另外三条结果保持默认的 `UNTRUSTED`——它们带的是 stdout /
        stderr，那正是「工具结果里的外部内容」最典型的样子。
        """
        content, truncated = _truncate(error.user_message, min(limit, MAX_TOOL_RESULT_LENGTH))
        return ToolResult(
            call_id=call_id,
            ok=False,
            content=content,
            truncated=truncated,
            side_effect=SideEffect.NONE,
            error=error,
            duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
            trust=TrustLevel.SYSTEM,
        )
