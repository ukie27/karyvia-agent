"""`nm capabilities`：打印覆盖解析报告（`D29`，技术方案 §10.4；`NFR-502`、`PLG-006`）。

职责：跑一次只读装配并把 `ResolutionReport` 的四段印出来——生效、被覆盖、已禁用、冲突，
每一条都带提供方标识。
不负责：装配可用实例（`bootstrap.py`）、判定覆盖（`kernel/registry/resolution.py`）、
改配置（`nm plugins`）。

**覆盖不静默**（技术方案 §8.3 第 4 条）：一个插件替掉了内建的会话存储，用户必须能一眼
看到 `(builtin:jsonl, plugin:session-pg)` 这对关系。因此 `shadowed` 段即使为空也照印
——「零条」是一条有价值的结论，只在非空时才提它会让用户不确定到底查没查。

**冲突印出来而不是抛出去**：`raise_if_failed()` 是启动路径的语义。对这条命令来说，
冲突恰恰是要看的东西，把它折成一条退出码 2 的诊断只会少印另外三段。
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Mapping, Sequence

from nucleamind.contracts import ErrorCode, JsonValue, NucleaError
from nucleamind.kernel.plugins import LoadOutcome
from nucleamind.kernel.registry import ResolutionReport

from ...inspect import inspect_capabilities
from ..main import Options

__all__ = ["capabilities_command"]

_USAGE = """用法：nm capabilities [--json]

选项：
  --json   输出 ResolutionReport 的 JSON 形态
"""


def capabilities_command(options: Options) -> int:
    args = options.rest
    if args and args[0] in ("-h", "--help"):
        sys.stdout.write(_USAGE)
        return 0
    unknown = set(args) - {"--json"}
    if unknown:
        raise NucleaError(
            ErrorCode.INPUT_MALFORMED, "未知选项。", detail={"unknown": sorted(unknown)}
        )
    inspection = asyncio.run(
        inspect_capabilities(
            instance_dir=options.instance_dir,
            instance=options.instance,
            overrides=options.overrides,
        )
    )
    report = inspection.report
    if report is None:  # pragma: no cover - `inspect_capabilities()` 恒填这一项。
        raise NucleaError(ErrorCode.KERNEL_INVARIANT_VIOLATED, "没有拿到覆盖解析报告。")
    if "--json" in args:
        sys.stdout.write(
            json.dumps(report.to_json(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        return 0
    sys.stdout.write(f"实例目录：{inspection.loaded.layout.root}\n\n")
    sys.stdout.write(_render(report))
    sys.stdout.write(_load_failures(inspection.outcomes))
    return 0


def _load_failures(outcomes: Sequence[LoadOutcome]) -> str:
    """`setup()` 没跑通的提供方。

    与「冲突」分开印：这些提供方的能力**从来没进过** registry（因此不会出现在上面四段
    里的任何一段），而冲突说的是进了又被判出局。最常见的一条是模型凭据还没导出。
    """
    failed = [outcome for outcome in outcomes if outcome.error is not None]
    if not failed:
        return ""
    lines = [f"\n加载失败的提供方（{len(failed)}）——它们的能力一项都没注册：\n"]
    for outcome in failed:
        error = outcome.error
        assert error is not None  # noqa: S101 - 上面刚筛过，这里只为窄化类型。
        lines.append(f"  {outcome.provider}  [{error.code.value}] {error.user_message}\n")
    return "".join(lines)


def _render(report: ResolutionReport) -> str:
    """四段文本。数据取自 `to_json()`——那是 `NFR-502` 承诺可序列化的那一份，
    渲染读它就不会与 `--json` 的输出各说各话。"""
    document = report.to_json()
    return "".join(
        [
            _active(_rows(document.get("active"))),
            _shadowed(_rows(document.get("shadowed"))),
            _disabled(_rows(document.get("disabled"))),
            _failures(_rows(document.get("failures"))),
        ]
    )


def _rows(value: JsonValue | None) -> tuple[Mapping[str, JsonValue], ...]:
    items = value if isinstance(value, (list, tuple)) else ()
    return tuple(item for item in items if isinstance(item, Mapping))


def _ref(ref: Mapping[str, JsonValue] | None) -> str:
    """一条能力引用：`kind:name ← provider`。

    provider 已经由 `_ref_json()` 渲染成 `builtin` / `plugin:<id>`（与覆盖目标串同一套
    编码），这里不再拼第二份——`NFR-502` 要的「包含 provider 标识」就是它。
    """
    if ref is None:
        return "（未知能力）"
    return f"{ref.get('kind')}:{ref.get('name')} ← {ref.get('provider')}"


def _mapping(row: Mapping[str, JsonValue], key: str) -> Mapping[str, JsonValue] | None:
    value = row.get(key)
    return value if isinstance(value, Mapping) else None


def _active(rows: Sequence[Mapping[str, JsonValue]]) -> str:
    if not rows:
        return "生效能力：无\n"
    lines = [f"生效能力（{len(rows)}）：\n"]
    lines.extend(f"  {_ref(row)}\n" for row in rows)
    return "".join(lines)


def _shadowed(rows: Sequence[Mapping[str, JsonValue]]) -> str:
    """被覆盖的能力与覆盖它的那一个，成对打印。"""
    if not rows:
        return "\n被覆盖：无\n"
    lines = [f"\n被覆盖（{len(rows)}）：\n"]
    for row in rows:
        lines.append(f"  {_ref(_mapping(row, 'capability'))}\n")
        lines.append(f"      被覆盖 → {_ref(_mapping(row, 'overridden_by'))}\n")
    return "".join(lines)


def _disabled(rows: Sequence[Mapping[str, JsonValue]]) -> str:
    if not rows:
        return "\n已禁用：无\n"
    lines = [f"\n已禁用（{len(rows)}）：\n"]
    for row in rows:
        lines.append(f"  {_ref(_mapping(row, 'capability'))}    原因：{row.get('reason')}\n")
    return "".join(lines)


def _failures(rows: Sequence[Mapping[str, JsonValue]]) -> str:
    """冲突。**非空即意味着这份配置起不来**，因此这一段带上错误码与 detail。"""
    if not rows:
        return "\n冲突：无\n"
    lines = [f"\n冲突（{len(rows)}）——实例在这份配置下无法启动：\n"]
    for row in rows:
        lines.append(f"  [{row.get('code')}] {row.get('message')}\n")
        detail = _mapping(row, "detail") or {}
        lines.extend(f"      {key}: {detail[key]}\n" for key in sorted(detail))
    return "".join(lines)
