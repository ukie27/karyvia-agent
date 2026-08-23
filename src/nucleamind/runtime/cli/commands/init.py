"""`nm init`：在实例目录里生成最小可用配置与它的 JSON Schema（`EDG-506`、`BAS-006`）。

职责：解析 `nm init` 的参数，调一次 `ensure_initial_config()`，打印指引并给出退出码。
不负责：写盘与渲染指引（`runtime/first_run.py`）、加载配置（`kernel/config/`）、
装配实例（`bootstrap.py`）。

**不取实例锁**：生成配置是一次性的本地动作，与「另一个实例正在跑」无关；而 `O_EXCL`
已经保证了并发下不会互相覆盖（见 `first_run.py` 的模块 docstring）。

**已存在时以非零码退出**：`nm init` 的语义是「把这个实例初始化好」，而一个已经有配置的
实例不需要被初始化。退让并说明比什么都不说更有用——用户下一步该做的是编辑那个文件，
路径就印在上面。
"""

from __future__ import annotations

import sys

from nucleamind.contracts import ErrorCode, NucleaError
from nucleamind.kernel.config import InstanceLayout

from ...first_run import ensure_initial_config, guidance_lines
from ...plugin_home import GlobalPluginHome
from ..main import Options

__all__ = ["init_command"]

_USAGE = """用法：nm init

在实例目录里生成最小可用的 config.json 与 config.schema.json。
已存在 config.json 时不做任何修改（配置文件永不被覆盖）。

选项：
  --instance <名字>      选实例（默认 default）
  --instance-dir <目录>  直接指定实例目录
"""


def init_command(options: Options) -> int:
    """`nm init` 的入口。返回值即退出码：0 生成成功，3 已存在。"""
    action = options.rest[0] if options.rest else ""
    if action in ("-h", "--help"):
        sys.stdout.write(_USAGE)
        return 0
    if action:
        raise NucleaError(
            ErrorCode.INPUT_MALFORMED,
            f"nm init 不接受参数 {action!r}。",
            detail={"suggestion": "用 --instance / --instance-dir 选实例。"},
        )

    layout = InstanceLayout.resolve(
        instance_dir=options.instance_dir, instance=options.instance
    )
    result = ensure_initial_config(layout)
    home = GlobalPluginHome.resolve()
    with home.registration():
        home.register_instance(layout.root)
    for line in guidance_lines(result):
        sys.stdout.write(line + "\n")
    return 0 if result.created else 3
