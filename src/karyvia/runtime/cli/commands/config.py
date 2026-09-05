"""`karyvia config show`：打印生效配置与每个值的来源（`CFG-005`）。

职责：加载配置并把它连同来源索引打印出来。
不负责：生成初始配置（`karyvia init`，见 `runtime/first_run.py`）、写回任何文件——本命令**只读**。

**不取实例锁**：`load_config()` 是纯读操作，而取锁会让「看一眼配置」与「正在跑的实例」
互斥（`kernel/config/loader.py` 的模块 docstring 写死了这条分工）。

明文凭据结构性地不在输出里：配置树自始至终持有 `${VAR}` 字面量（`D11`），
`/config` 命令与本命令读的是同一棵树。
"""

from __future__ import annotations

import json
import sys

from karyvia.contracts import ErrorCode, KaryviaError
from karyvia.kernel.config import load_config

from ..main import Options

__all__ = ["config_command"]

_USAGE = """用法：karyvia config show [--origins] [--json]

选项：
  --origins   逐字段打印来源（default / config.json / env / cli）
  --json      输出 JSON 而不是文本
"""


def config_command(options: Options) -> int:
    action = options.rest[0] if options.rest else ""
    if action in ("", "-h", "--help"):
        sys.stdout.write(_USAGE)
        return 0
    if action != "show":
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            f"未知的 config 子命令 {action!r}。",
            detail={"known": ["show"]},
        )

    flags = set(options.rest[1:])
    unknown = flags - {"--origins", "--json"}
    if unknown:
        raise KaryviaError(
            ErrorCode.INPUT_MALFORMED,
            "未知选项。",
            detail={"unknown": sorted(unknown), "known": ["--origins", "--json"]},
        )

    loaded = load_config(
        instance=options.instance,
        instance_dir=options.instance_dir,
        overrides=options.overrides,
        # 只读诊断不该顺手建目录（`load_config` 的 `ensure_dirs` 就是为此存在的）。
        ensure_dirs=False,
    )
    document = loaded.to_json()
    if "--json" in flags:
        sys.stdout.write(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return 0

    sys.stdout.write(f"实例目录：{loaded.layout.root}\n")
    sys.stdout.write(f"workspace：{loaded.workspace_root}\n\n")
    sys.stdout.write(json.dumps(document["config"], ensure_ascii=False, indent=2, sort_keys=True))
    sys.stdout.write("\n")
    if "--origins" in flags:
        sys.stdout.write("\n来源（未列出的取自内置默认值）：\n")
        for pointer, origin in sorted(loaded.merge.origins.items()):
            sys.stdout.write(f"  {pointer}: {origin}\n")
    return 0
