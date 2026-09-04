"""cwd 边界守卫：把 `cwd` 参数判成 workspace 内的一个目录（`EDG-405`、`NFR-302`）。

职责：`CwdGuard`——解析、双重校验（逻辑 + realpath）、渲染回相对显示路径。
不负责：执行命令（`process.py`）、
决定 workspace 根在哪（配置交下来，见 `settings.py`）。

**判据与 `tools_fs.WorkspaceGuard` 逐条相同**（`EDG-405`、`NFR-302`）：

1. **逻辑校验**：`os.path.normpath` 之后（不跟随符号链接）必须落在根内——挡住 `..`。
2. **realpath 校验**：`Path.resolve()` 之后必须**再**落在根内——挡住符号链接与 Windows
   的重解析点。

两次比较都先过 `os.path.normcase`，因此 Windows 的大小写差异不构成绕过面，而 Linux 上
`normcase` 是恒等函数——同一段代码在两个平台给出同一套判定（`NFR-605`）。

**为什么不 import `tools_fs.WorkspaceGuard` 而是各写一份**（`AGENTS.md` 原则 5「优先重复
而非过早抽象」）：`R4` 确实允许 `builtins/` 之间互相 import，但 `tools-fs` 与 `tools-shell`
是两份独立的 manifest、两个可以各自被禁用或被第三方覆盖的提供方。让其中一个 import 另一个
的内部模块，等于在能力边界之外偷偷建立一条依赖——`tools-fs` 被替换成第三方实现时，
`tools-shell` 仍然绑在内建那份代码上。两份实现由
`tests/builtins/test_tools_shell.py::test_cwd_guard_matches_the_fs_workspace_guard` 逐条对照
钉住；该常量属于跨层协议事实，因此由测试保持两侧一致。

**这是应用级守卫，不是 OS 沙箱**：校验与随后 `create_subprocess_exec` 使用该 cwd 之间存在
TOCTOU 窗口——目标可以在这期间被换成一个指向根外的符号链接。更要紧的是，**守住 cwd 并不
等于守住命令能碰到的文件**：一条 `cat /etc/shadow` 用的是绝对路径，与 cwd 无关。cwd 边界
限制的是「命令默认在哪里落地」。更严格的控制是不启用该工具，或使用独立宿主 / 部署沙箱；
这里如实写明，不假装挡得住。
"""

from __future__ import annotations

import os
from pathlib import Path

from nucleamind.contracts import ErrorCode, NucleaError

__all__ = ["CwdGuard"]


def _key(path: Path | str) -> str:
    """比较用的归一形式。Windows 上折叠大小写，POSIX 上是恒等。"""
    return os.path.normcase(os.fspath(path))


def _within(candidate: Path, root: Path) -> bool:
    """`candidate` 是否就是 `root` 或它的后代。纯字符串判定，不碰磁盘。

    显式要求落在分隔符边界上——否则 `/x/ws-evil` 会被判成 `/x/ws` 的后代。
    """
    candidate_key = _key(candidate)
    root_key = _key(root)
    if candidate_key == root_key:
        return True
    prefix = root_key if root_key.endswith(os.sep) else root_key + os.sep
    return candidate_key.startswith(prefix)


class CwdGuard:
    """一个 workspace 根，以及「这个 cwd 算不算数」的全部判定。

    根在构造时 `resolve()` 一次并记住：之后每次校验都只是字符串比较，不会因为根自身
    是个符号链接而每次都重新解析出不同结果。
    """

    __slots__ = ("_root",)

    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve()

    @property
    def root(self) -> Path:
        """已解析的 workspace 根（绝对路径）。"""
        return self._root

    def resolve(self, raw: str) -> Path:
        """把一个模型给的 cwd 串判成边界内的绝对路径。

        相对路径按根解析；绝对路径同样接受，但要过同一道门——两步校验都在，拒绝它只会
        换来模型拼一串 `../`。**不做 `expanduser()`**：`~` 是模型给的普通字符，展开它
        等于凭空多出一条通往用户主目录的路径（与 `tools_fs` 同一条判定）。

        **异常约定**：空串、NUL 字节抛 `INPUT_MALFORMED`；越界抛
        `PERMISSION_PATH_OUTSIDE_WORKSPACE`，`detail` 里只放原始串**不放**解析结果——
        把宿主机的绝对路径写进模型可见的错误里是另一种泄漏。
        """
        if not raw or not raw.strip():
            raise NucleaError(ErrorCode.INPUT_MALFORMED, "cwd 不能为空。", detail={"cwd": raw})
        if "\x00" in raw:
            raise NucleaError(
                ErrorCode.INPUT_MALFORMED, "cwd 不得包含 NUL 字节。", detail={"cwd": "<binary>"}
            )

        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self._root / candidate

        logical = Path(os.path.normpath(candidate))
        if not _within(logical, self._root):
            raise self._outside(raw)
        real = logical.resolve()
        if not _within(real, self._root):
            raise self._outside(raw)
        return real

    def relative(self, path: Path) -> str:
        """渲染成根内的 posix 相对路径，供模型消费。

        工具产出里一律用它而不是绝对路径：两个平台给出同一个串（`NFR-605`），顺带也不把
        宿主机目录结构送进上下文。根自身渲染成 `"."`。
        """
        try:
            relative = path.relative_to(self._root)
        except ValueError:
            # 只可能是调用方把一个没过 `resolve()` 的路径递进来了——那是本包内部的
            # 编程错误，不是用户输入问题。
            raise NucleaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "渲染了一个不在 workspace 内的路径。",
                detail={"root": str(self._root)},
            ) from None
        text = relative.as_posix()
        return text if text and text != "." else "."

    def _outside(self, raw: str) -> NucleaError:
        return NucleaError(
            ErrorCode.PERMISSION_PATH_OUTSIDE_WORKSPACE,
            "cwd 越出了 workspace 边界。",
            detail={"cwd": raw},
        )
