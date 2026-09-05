"""第 5 层：组装根。

职责：把 kernel 机制与 builtins / plugins 能力装配成可运行实例，并提供 `karyvia`
可执行程序；本层是唯一允许同时 import kernel 与 builtins 的地方。
不负责：实现机制或能力本身。

`bootstrap.py` 是唯一组装根，`plugin_bootstrap.py` 收口插件装配策略，`startup.py` 管理
实例构造成功前的资源所有权，`instance.py` 管理运行期生命周期，`access/` 提供受控资源
门面，`cli/` 提供 `karyvia` 命令。可复用机制应下沉到 Kernel，不在本层形成第二套实现。
"""
