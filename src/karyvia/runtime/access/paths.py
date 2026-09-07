"""插件路径守卫：把插件给的路径串判成 workspace 内的绝对路径。

职责：`PathGuard`——解析、双重校验（逻辑 + realpath）并渲染回相对显示路径。
不负责：读写文件（`files.py`）或决定根在哪（装配根交下来）。

**这是第三份 workspace 双重校验**。前两份是 `builtins/tools_fs/paths.py::WorkspaceGuard`
与 `builtins/tools_shell/paths.py::CwdGuard`——`R2` 禁止 `runtime/` 之外的层互相够到，而这
三者分属三个可以各自被禁用或被第三方覆盖的边界。判定逐条相同，由
`tests/runtime/test_access.py::test_path_guard_matches_the_fs_workspace_guard` 钉住，
改一边要改三边（与 `estimate_tokens`、`DEFAULT_GRACE_MS` 同一种做法）。

**与那两份的一处刻意差异：绝对路径一律拒绝**。`FileAccess` 的契约（`sdk/api.py`）就是这么
写的，理由也不同——`tools_fs` 面对的是模型给的路径串，拒绝绝对路径只会换来一串 `../`；
而这里面对的是插件作者写的代码，一条绝对路径几乎总是「我以为自己在宿主机上随便读写」。

**这是应用级守卫，不是 OS 沙箱**：校验与随后的 `open()` 之间存在 TOCTOU 窗口，目标可以在
这期间被换成一个指向根外的符号链接。挡住它需要 `openat` + `O_NOFOLLOW` 一类原语，那在
Windows 上没有对等物。这里如实写明，不假装挡得住（§13.7）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from karyvia.contracts import ErrorCode, KaryviaError

__all__ = ["RESERVED_DEVICE_NAMES", "PathGuard"]

#: Windows 的保留设备名。能通过 containment 校验却根本不是文件——两个平台一律拒绝，
#: 为它开平台分支等于让同一段插件代码在 Linux 上成功、在 Windows 上失败。
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

    显式要求落在分隔符边界上——否则 `/x/ws-evil` 会被判成 `/x/ws` 的后代。
    """
    candidate_key = _key(candidate)
    root_key = _key(root)
    if candidate_key == root_key:
        return True
    prefix = root_key if root_key.endswith(os.sep) else root_key + os.sep
    return candidate_key.startswith(prefix)


class PathGuard:
    """一个根，以及「这个路径串算不算数」的全部判定。"""

    __slots__ = ("_root",)

    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve()

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, raw: str) -> Path:
        """把一个路径串判成 workspace 内的绝对路径。

        **异常约定**：空串、NUL 字节、保留设备名、绝对路径抛 `INPUT_MALFORMED`；
        越出根抛 `PERMISSION_PATH_OUTSIDE_WORKSPACE`，`detail` 里**只放原始
        串不放解析结果**——把宿主机的绝对路径写进错误是另一种泄漏（同一条判据）。
        """
        if not raw or not raw.strip():
            raise KaryviaError(ErrorCode.INPUT_MALFORMED, "路径不能为空。", detail={"path": raw})
        if "\x00" in raw:
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED, "路径不得包含 NUL 字节。", detail={"path": "<binary>"}
            )
        candidate = Path(raw)
        if candidate.is_absolute():
            raise KaryviaError(
                ErrorCode.INPUT_MALFORMED,
                "ctx.fs 只接受相对路径，根由实例 workspace 决定。",
                detail={"path": raw},
            )
        self._reject_reserved_names(raw)

        logical = Path(os.path.normpath(self._root / candidate))
        if not _within(logical, self._root):
            raise self._outside(raw)
        real = logical.resolve()
        if not _within(real, self._root):
            raise self._outside(raw)
        return real

    def relative(self, path: Path) -> str:
        """渲染成根内的 posix 相对路径。根自身渲染成 `"."`。"""
        try:
            relative = path.relative_to(self._root)
        except ValueError:
            raise KaryviaError(
                ErrorCode.KERNEL_INVARIANT_VIOLATED,
                "渲染了一个不在根内的路径。",
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
