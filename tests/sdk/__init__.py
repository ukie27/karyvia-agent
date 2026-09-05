"""`tests/sdk/`：SDK 公开表面、manifest 与测试夹具（`D05`）。

职责：锁住 `sdk.__all__` 快照与 `KaryviaAPI` 的 9 个注册方法，验证 manifest 校验矩阵、
导入无副作用，并用 Fake 跑通 5 个契约测试基类。
不负责：Kernel 侧的注册与解析行为——那是 `tests/kernel/`（`D06`）。
"""
