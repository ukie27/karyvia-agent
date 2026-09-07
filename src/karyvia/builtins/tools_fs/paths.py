"""Workspace 路径守卫：把模型给的路径串判成一个边界内的绝对路径。

职责：`WorkspaceGuard`——解析、双重校验（逻辑 + realpath）、渲染回相对显示路径，以及
越界与形状非法两类拒绝。
不负责：读写文件（`readers.py` / `writers.py`）、决定 workspace 根在哪（配置交下来，
见 `settings.py`）。

**判据是双重校验，缺一不可**：

1. **逻辑校验**：`os.path.normpath` 之后（不跟随符号链接）必须落在根内——挡住 `..`。
2. **realpath 校验**：`Path.resolve()` 之后必须**再**落在根内——挡住符号链接与 Windows
   的重解析点。

只做第二步会让一个指向根外、但尚不存在的路径在创建后才暴露；只做第一步则任何一个
symlink 都能出去。两次比较都先过 `os.path.normcase`，因此 Windows 的大小写差异不构成
绕过面，而 Linux 上 `normcase` 是恒等函数——同一段代码在两个平台给出同一套判定
。

**这是应用级守卫，不是 OS 沙箱**：校验与随后的 `open()` 之间存在 TOCTOU 窗口——目标可以
在这期间被换成一个指向根外的符号链接。挡住它需要 `openat` + `O_NOFOLLOW` 一类的原语，
而那在 Windows 上没有对等物。这里如实写明，不假装挡得住；更严格的隔离由可选独立宿主
或部署环境承担。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from karyvia.contracts import ErrorCode, KaryviaError

__all__ = ["RESERVED_DEVICE_NAMES", "WorkspaceGuard"]

#: Windows 的保留设备名。它们能通过 containment 校验（`workspace/CON` 看起来完全正常）
#: 却根本不是文件——打开它会连上控制台或空设备。两个平台一律拒绝而不是只在 Windows 上
#: 拒绝： 要的是同参数同语义，为它开一个平台分支等于让同一份工具调用在 Linux
#: 上成功、在 Windows 上失败。
RESERVED_DEVICE_NAMES: Final = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{digit}" for digit in range(1, 10)}
    | {f"lpt{digit}" for digit in range(1, 10)}
)


def _key(path: Path | str) -> str:
    """比较用的归一形式。Windows 上折叠大小写，POSIX 上是恒等。"""
    return os.path.normcase(os.fspath(path))


def _within(candidate: Path, root: Path) -> bool:
    """`candidate` 是否就是 `root` 或它的后代。纯字符串判定，不碰磁盘。

    用 `os.path.commonpath` 之类的辅助只会多一层可能抛异常的间接；这里比较归一后的串，
    并显式要求分隔符边界——否则 `/ws-evil` 会被判成 `/ws` 的后代。
    """
    candidate_key = _key(candidate)
    root_key = _key(root)
    if candidate_key == root_key:
        return True
    prefix = root_key if root_key.endswith(os.sep) else root_key + os.sep
    return candidate_key.startswith(prefix)


class WorkspaceGuard:
    """一个 workspace 根，以及「这个路径串算不算数」的全部判定。

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
        """把一个模型给的路径串判成边界内的绝对路径。

        相对路径按根解析；绝对路径同样接受，但要过同一道门——`FileAccess` 那条「绝对
        路径一律拒绝」是针对**没有根校验**的门面说的，这里两步校验都在，拒绝它只会换来
        模型拼一串 `../`。**不做 `expanduser()`**：`~` 是模型给的普通字符，展开它等于
        凭空多出一条通往用户主目录的路径，而一个真叫 `~` 的目录反倒读不到了。

        **异常约定**：空串、NUL 字节、保留设备名抛 `INPUT_MALFORMED`；越界抛
        `PERMISSION_PATH_OUTSIDE_WORKSPACE`，`detail` 里只放原始串**不放**解析结果——
        把宿主机的绝对路径写进模型可见的错误里是另一种泄漏。
        """
        if not raw or not raw.strip():
            raise KaryviaError(ErrorCode.INPUT_MALFORMED, "路径不能为空。", detail={"path": raw})
        if "\x00" in raw:
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED, "路径不得包含 NUL 字节。", detail={"path": "<binary>"}
            )
        self._reject_reserved_names(raw)

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

        工具产出里一律用它而不是绝对路径：两个平台给出同一个串，顺带也不把
        宿主机目录结构送进上下文。根自身渲染成 `"."`。
        """
        try:
            relative = path.relative_to(self._root)
        except ValueError:
            # 只可能是调用方把一个没过 `resolve()` 的路径递进来了——那是本包内部的
            # 编程错误，不是用户输入问题。
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "渲染了一个不在 workspace 内的路径。",
                detail={"root": str(self._root)},
            ) from None
        text = relative.as_posix()
        return text if text and text != "." else "."

    def _reject_reserved_names(self, raw: str) -> None:
        for part in Path(raw).parts:
            stem = part.split(".")[0].strip().lower()
            if stem in RESERVED_DEVICE_NAMES:
                raise KaryviaError(
                    ErrorCode.INPUT_MALFORMED,
                    "路径包含保留设备名，两个平台一律拒绝。",
                    detail={"path": raw, "segment": part},
                )

    def _outside(self, raw: str) -> KaryviaError:
        return KaryviaError(
            ErrorCode.PERMISSION_PATH_OUTSIDE_WORKSPACE,
            "路径越出了 workspace 边界。",
            detail={"path": raw},
        )
