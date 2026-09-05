"""`karyvia` 的子命令实现（技术方案 §4.2）。

职责：re-export 三个子命令入口，使 `main.py` 只需要一条 import 路径。
不负责：解析顶层 argv（`main.py`）、装配实例（`runtime/bootstrap.py`）。

**每个子命令一个模块**：它们的失败模式互不相同（`run` 要管信号与实例锁，`config` 只读，
`session` 只装一条能力），塞进一个文件会让「这条命令到底需要什么」不可读。
`karyvia plugins` / `karyvia capabilities` 是 `D29`。
"""

from __future__ import annotations

#: 空的公开表面是刻意的：`main.py` 直接 import 三个子模块（`from .commands.run import
#: run_command`），这样 `karyvia --version` 不会因为一次包级 re-export 就把装配根那条 import
#: 链拉进来（`NFR-405` 的冷启动预算）。
__all__: list[str] = []
