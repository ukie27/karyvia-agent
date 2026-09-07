"""记录 `karyvia` 的启动开销指标。

常驻脚本。测量三件事：

    import_ms      `import karyvia` 的耗时（必须保持零副作用、零子模块导入）
    version_ms     `karyvia --version` 全流程耗时（进程启动 + argv 解析）
    startup_ms     无插件冷启动到**可接受输入**：装配根 import + bootstrap() + start()
    imported       `import karyvia` 之后进入 sys.modules 的本项目模块清单

第四项是最有价值的：`karyvia/__init__.py` 承诺「零依赖、零副作用」，一旦有人
在包根加了便利导入，清单会立刻变长，而耗时可能还看不出来。

`startup_ms` **只告警不失败**（原文：「超出阈值 20% 触发告警而非直接失败」）。
CI 机器的抖动足以让一个 300 ms 的门禁天天误报，而误报的门禁最后一定会被关掉。它拆成
import 与 bootstrap 两段报出来，「该优化哪一段」因此查得到。

用法：
    python scripts/check_startup_cost.py               # 人读
    python scripts/check_startup_cost.py --json        # 机读，供 CI 记录
    python scripts/check_startup_cost.py --check       # 越过阈值即非零退出（不含 startup_ms）
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# 阈值宽松，只用于拦「数量级劣化」，不做微基准。冷/热缓存与 CI 机器差异都在这之内。
IMPORT_BUDGET_MS = 150.0
VERSION_BUDGET_MS = 2000.0

#: 的目标：无插件冷启动到可接受输入 ≤ 300 ms（不含解释器启动）。
#: **它是告警线不是门禁线**，见模块 docstring。
STARTUP_BUDGET_MS = 300.0

#: 告警容差。超过 `预算 × (1 + 容差)` 才出声，免得贴着线抖动时天天告警。
STARTUP_WARN_RATIO = 0.20

# `import karyvia` 之后允许出现在 sys.modules 里的本项目模块。
# 包根之外任何东西被拉进来，都说明有人在 __init__.py 加了便利导入。
ALLOWED_EAGER_MODULES = frozenset({"karyvia"})

_MEASURE_IMPORT = textwrap.dedent(
    """
    import json, sys, time

    start = time.perf_counter()
    import karyvia  # noqa: F401
    elapsed_ms = (time.perf_counter() - start) * 1000

    imported = sorted(
        name for name in sys.modules
        if name == "karyvia" or name.startswith("karyvia.")
    )
    json.dump({"import_ms": elapsed_ms, "imported": imported}, sys.stdout)
    """
)

#: 冷启动测量。刻意在**干净子进程**里跑：同进程测量会拿到已经暖好的 import 缓存，
#: 而「冷启动」问的正是那笔钱。
#:
#: 三个刻意的选择：① 实例目录是临时的，配置由 `karyvia init` 那条同一路径生成——测的必须是
#: 用户第二次 `karyvia run` 真正走的路，不是一份手搓的最小配置；② 凭据用一个假值导出，
#: `model-openai` 只在 setup 时读它，**不发任何请求**（没有网络参与这次测量）；
#: ③ 计时分两段，import 与 bootstrap 各自可归因。
_MEASURE_STARTUP = textwrap.dedent(
    """
    import asyncio, json, os, shutil, sys, tempfile, time
    from pathlib import Path

    root = Path(tempfile.mkdtemp(prefix="karyvia-startup-"))
    os.environ["OPENAI_API_KEY"] = "startup-cost-probe-not-a-real-key"

    start = time.perf_counter()
    from karyvia.kernel.config import InstanceLayout
    from karyvia.runtime.bootstrap import bootstrap
    from karyvia.runtime.first_run import ensure_initial_config
    import_ms = (time.perf_counter() - start) * 1000

    layout = InstanceLayout.resolve(instance_dir=root)
    ensure_initial_config(layout)

    async def boot():
        began = time.perf_counter()
        instance = await bootstrap(instance_dir=root)
        await instance.start()
        elapsed = (time.perf_counter() - began) * 1000
        await instance.stop()
        return elapsed

    try:
        bootstrap_ms = asyncio.run(boot())
    finally:
        shutil.rmtree(root, ignore_errors=True)

    json.dump(
        {"startup_import_ms": import_ms, "startup_bootstrap_ms": bootstrap_ms}, sys.stdout
    )
    """
)


def _run_python(code: str) -> str:
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        raise SystemExit(
            f"[startup-cost] 子进程失败（{completed.returncode}）：\n{completed.stderr}"
        )
    return completed.stdout


def measure_import() -> dict[str, object]:
    """在干净子进程中测量 `import karyvia`（同进程测量会被已导入模块污染）。"""
    return json.loads(_run_python(_MEASURE_IMPORT))


def measure_version() -> float:
    """测量 `karyvia --version` 全流程；走 `python -m` 以免依赖 console script 已安装。"""
    start = time.perf_counter()
    completed = subprocess.run(
        [sys.executable, "-m", "karyvia.runtime.cli.main", "--version"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    elapsed_ms = (time.perf_counter() - start) * 1000
    if completed.returncode != 0:
        raise SystemExit(
            f"[startup-cost] `karyvia --version` 失败（{completed.returncode}）：\n{completed.stderr}"
        )
    return elapsed_ms


def measure_startup() -> dict[str, float]:
    """在干净子进程里测量冷启动到可接受输入。"""
    raw = json.loads(_run_python(_MEASURE_STARTUP))
    return {
        "startup_import_ms": round(float(raw["startup_import_ms"]), 2),
        "startup_bootstrap_ms": round(float(raw["startup_bootstrap_ms"]), 2),
    }


def collect() -> dict[str, object]:
    result = measure_import()
    startup = measure_startup()
    return {
        "import_ms": round(float(result["import_ms"]), 2),  # type: ignore[arg-type]
        "version_ms": round(measure_version(), 2),
        "startup_ms": round(startup["startup_import_ms"] + startup["startup_bootstrap_ms"], 2),
        "startup_import_ms": startup["startup_import_ms"],
        "startup_bootstrap_ms": startup["startup_bootstrap_ms"],
        "imported": result["imported"],
        "budgets": {
            "import_ms": IMPORT_BUDGET_MS,
            "version_ms": VERSION_BUDGET_MS,
            "startup_ms": STARTUP_BUDGET_MS,
        },
    }


def warnings_for(data: dict[str, object]) -> list[str]:
    """返回**告警**（不影响退出码）。 的 20% 容差在这里，不在 `check` 里。"""
    startup_ms = float(data["startup_ms"])  # type: ignore[arg-type]
    threshold = STARTUP_BUDGET_MS * (1 + STARTUP_WARN_RATIO)
    if startup_ms <= threshold:
        return []
    return [
        f"冷启动到可接受输入 {startup_ms}ms > {threshold:.0f}ms"
        f"（目标 {STARTUP_BUDGET_MS}ms + {int(STARTUP_WARN_RATIO * 100)}% 容差）；"
        f"其中 import {data['startup_import_ms']}ms、bootstrap "
        f"{data['startup_bootstrap_ms']}ms"
    ]


def check(data: dict[str, object]) -> list[str]:
    """返回**门禁**级越界说明；全部在预算内时返回空列表。

    `startup_ms` 刻意不在这里：它是告警项（`warnings_for`），见模块 docstring。
    """
    problems: list[str] = []

    import_ms = float(data["import_ms"])  # type: ignore[arg-type]
    if import_ms > IMPORT_BUDGET_MS:
        problems.append(f"import karyvia 耗时 {import_ms}ms > {IMPORT_BUDGET_MS}ms")

    version_ms = float(data["version_ms"])  # type: ignore[arg-type]
    if version_ms > VERSION_BUDGET_MS:
        problems.append(f"karyvia --version 耗时 {version_ms}ms > {VERSION_BUDGET_MS}ms")

    imported = set(data["imported"])  # type: ignore[arg-type]
    unexpected = sorted(imported - ALLOWED_EAGER_MODULES)
    if unexpected:
        problems.append(
            "`import karyvia` 拉入了额外子模块（包根必须零副作用）："
            + ", ".join(unexpected)
        )

    return problems


def _print_table(data: dict[str, object]) -> None:
    imported = data["imported"]
    assert isinstance(imported, list)
    print("karyvia 启动开销")
    print("=" * 46)
    print(f"  import karyvia   {data['import_ms']:>8} ms   (预算 {IMPORT_BUDGET_MS} ms)")
    print(f"  karyvia --version        {data['version_ms']:>8} ms   (预算 {VERSION_BUDGET_MS} ms)")
    print(f"  冷启动到可接受输入  {data['startup_ms']:>8} ms   (目标 {STARTUP_BUDGET_MS} ms，仅告警)")
    print(f"    ├ import          {data['startup_import_ms']:>8} ms")
    print(f"    └ bootstrap+start {data['startup_bootstrap_ms']:>8} ms")
    print(f"  急切导入的模块      {len(imported)} 个：{', '.join(imported)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="记录 karyvia 启动开销")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--out", type=Path, help="把 JSON 写入文件（隐含 --json）")
    parser.add_argument("--check", action="store_true", help="越过阈值以非零码退出")
    args = parser.parse_args(argv)

    data = collect()

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"[startup-cost] 写出 {args.out}")
    elif args.json:
        json.dump(data, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        _print_table(data)

    if args.check:
        for line in warnings_for(data):
            print(f"[startup-cost] 告警：{line}", file=sys.stderr)
        problems = check(data)
        if problems:
            print("[startup-cost] 启动开销回归：", file=sys.stderr)
            for line in problems:
                print(f"  {line}", file=sys.stderr)
            return 1
        print("[startup-cost] OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
