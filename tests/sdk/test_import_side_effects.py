"""「导入 manifest 模块无副作用且廉价」的可执行形态（技术方案 §7.2 硬约束）。

阶段 A 校验要保持在毫秒级（`NFR-401`、`NFR-403`），前提是读一份 manifest 不会顺带
连网、写盘或拉起半个运行时。用**子进程 + audit hook** 而不是 mock：审计钩子看到的是
解释器实际发生的事件，mock 只能证明「我们没调用自己挑出来的那几个函数」。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

#: 导入耗时上限（毫秒）。宽到不会因为机器慢而抖动，紧到能拦住「顺手 import 了整个
#: kernel」这一类真正的回归。
IMPORT_BUDGET_MS: Final = 2_000

#: 探针在子进程里跑：装上 audit hook，再导入被测模块，最后打印一份 JSON 报告。
#: `-B` 关闭 .pyc 写入，否则首次导入产生的字节码缓存会被记成「写文件」。
#: 模块名用占位符替换而不是 `str.format`——探针里全是花括号，逐个转义只会更容易写错。
_PROBE: Final = """
import importlib
import json
import sys
import time

writes, network = [], []


def _hook(event, args):
    if event == "open":
        mode = args[1] if len(args) > 1 else None
        if isinstance(mode, str) and any(flag in mode for flag in "wax+"):
            writes.append("open:" + str(args[0]))
    elif event in ("os.mkdir", "os.rename", "os.remove", "os.rmdir", "shutil.copyfile"):
        writes.append(event)
    elif event in ("socket.connect", "socket.getaddrinfo", "urllib.Request"):
        network.append(event)


sys.addaudithook(_hook)

start = time.perf_counter()
importlib.import_module("__MODULE__")
elapsed_ms = (time.perf_counter() - start) * 1000

print(json.dumps({
    "writes": writes,
    "network": network,
    "elapsed_ms": elapsed_ms,
    "modules": sorted(m for m in sys.modules if m.startswith("karyvia")),
}))
"""


def _probe(module: str, *, cwd: Path | None = None) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-B", "-c", _PROBE.replace("__MODULE__", module)],
        capture_output=True,
        text=True,
        check=True,
        cwd=cwd,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def manifest_probe() -> dict[str, object]:
    """一次子进程，多条断言——每条断言都起一个解释器太慢。"""
    return _probe("karyvia.sdk.manifest")


def test_importing_manifest_writes_no_files(manifest_probe: dict[str, object]) -> None:
    assert manifest_probe["writes"] == []


def test_importing_manifest_touches_no_network(manifest_probe: dict[str, object]) -> None:
    assert manifest_probe["network"] == []


def test_importing_manifest_is_cheap(manifest_probe: dict[str, object]) -> None:
    elapsed = manifest_probe["elapsed_ms"]
    assert isinstance(elapsed, float)
    assert elapsed < IMPORT_BUDGET_MS, f"导入 manifest 耗时 {elapsed:.0f}ms"


def test_importing_manifest_pulls_in_only_contracts_and_sdk(
    manifest_probe: dict[str, object],
) -> None:
    """manifest 是数据：读它不该拉起 kernel、runtime、legacy 或任何能力实现。"""
    modules = manifest_probe["modules"]
    assert isinstance(modules, list)
    forbidden = [
        name
        for name in modules
        if name.startswith(("karyvia.kernel", "karyvia.builtins"))
    ]
    assert not forbidden, f"导入 manifest 顺带拉起了：{forbidden}"


def test_the_probe_actually_detects_a_write(tmp_path: Path) -> None:
    """守卫自证：注入一个在导入时写文件的模块，探针必须报出来。

    没有这条，上面四个「什么都没发生」的断言可能只是因为钩子根本没装上。
    """
    (tmp_path / "side_effecty.py").write_text(
        "from pathlib import Path\n"
        "Path(__file__).with_name('touched.txt').write_text('x', encoding='utf-8')\n",
        encoding="utf-8",
    )
    report = _probe("side_effecty", cwd=tmp_path)
    assert report["writes"], "探针没有捕获到写文件"
