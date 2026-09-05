"""`karyvia` 可执行程序。

职责：解析 argv，把子命令派发到 runtime 的组装入口。
不负责：会话内的斜杠命令（见 builtins/commands_core），也不实现业务逻辑。
"""
