"""启动时打印已启用但未成功运行的插件。

职责：汇总插件发现、加载、激活与能力解析失败，并向启动命令的错误流输出一次。
不负责：决定插件失败是否中断实例、持久化诊断或格式化完整插件清单。
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from typing import TextIO

from nucleamind.contracts import NucleaError, Plugin, parse_capability_target
from nucleamind.runtime.instance import AgentInstance

__all__ = ["write_plugin_failures"]


def write_plugin_failures(instance: AgentInstance, *, stream: TextIO = sys.stderr) -> None:
    """把本次实例中失败的插件集中打印一次。"""
    failed = {
        str(status.plugin_id): status.failure
        for status in instance.diagnostics.plugins()
        if status.failure is not None
    }
    for error in instance.report.failures:
        for plugin_id in _plugin_ids(error):
            failed.setdefault(plugin_id, error)
    if not failed:
        return
    stream.write(f"nm: {len(failed)} 个已启用插件未能加载或启动：\n")
    for plugin_id, error in sorted(failed.items()):
        stream.write(
            f"  {plugin_id}: "
            f"[{error.code.value}] {error.user_message}\n"
        )
    stream.flush()


def _plugin_ids(error: NucleaError) -> Iterable[str]:
    """从能力解析错误中找出应被提醒的外部插件。"""
    if error.capability is not None and isinstance(error.capability.provider, Plugin):
        yield str(error.capability.provider.plugin_id)

    claimants = error.detail.get("claimants")
    if not isinstance(claimants, list):
        return
    for claimant in claimants:
        if not isinstance(claimant, str):
            continue
        try:
            provider, _ = parse_capability_target(claimant)
        except NucleaError:
            continue
        if isinstance(provider, Plugin):
            yield str(provider.plugin_id)
