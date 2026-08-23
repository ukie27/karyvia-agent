"""全局插件安装与实例级启用管理。

职责：在 NucleaMind home 中安装、更新和卸载插件；列出当前实例状态；把启用 / 禁用写进
实例 ``config.json``；在显式确认后删除实例插件状态。
不负责：发现与阶段 A 判定（`runtime/inspect.py` → `inventory.py` / `plugin_plan.py`）、
改配置的文件操作（`runtime/config_edit.py`）。

安装、更新、卸载是全局操作，不接受实例参数，并要求全部实例停止。启用、禁用和状态清理
仍是实例操作。卸载删除全局代码和所有已知实例中的配置引用，但保留各实例业务状态。
"""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from nucleamind.builtins.registry import BUILTIN_MANIFESTS
from nucleamind.contracts import ErrorCode, JsonValue, NucleaError
from nucleamind.kernel.config import InstanceLayout
from nucleamind.kernel.observability import PluginStatus

from ...config_edit import (
    add_to_list,
    read_document,
    remove_from_list,
    remove_plugin_entry,
    write_document,
)
from ...inspect import inspect_plugins
from ...plugin_home import (
    GlobalPluginHome,
    install_plugin,
    uninstall_plugin,
    update_plugin,
    validate_uninstall,
)
from ..main import Options

__all__ = ["plugins_command"]

_USAGE = """用法：nm plugins <子命令>

子命令：
  install <来源> [--no-deps] 全局安装一个 Python 插件
  update <插件 id>          从原安装来源全局更新插件
  uninstall <插件 id>       全局卸载，并清除所有已知实例的配置引用
  list [--json]              列出已发现的插件、状态、版本与能力
  enable <插件 id>           启用外部插件，或恢复被禁用的内建插件
  disable <插件 id>          写入 plugins.disable（对内建同样有效）
  purge <插件 id> --confirm  删除插件的状态目录（先打印路径与体积）

install / update / uninstall 是全局操作，不能与 --instance / --instance-dir 一起使用，
并且执行时所有实例都必须已经停止。
"""

#: 改完配置后统一的那句话。首版不热更新，说清楚比让用户困惑地敲 `/plugins` 强。
_RESTART_HINT = "改动在实例下次启动时生效（首版不热更新）。"

_PLUGINS = "plugins"
_ENABLED = "enabled"
_DISABLE = "disable"


def plugins_command(options: Options) -> int:
    action = options.rest[0] if options.rest else ""
    if action in ("", "-h", "--help"):
        sys.stdout.write(_USAGE)
        return 0
    args = options.rest[1:]
    if action in {"install", "update", "uninstall"}:
        _require_global(options, action)
        home = GlobalPluginHome.resolve()
        match action:
            case "install":
                return _install(home, args)
            case "update":
                return _update(home, args)
            case _:
                return _uninstall(home, args)
    layout = InstanceLayout.resolve(instance_dir=options.instance_dir, instance=options.instance)
    match action:
        case "list":
            return _list(options, args)
        case "enable":
            return _enable(layout, args)
        case "disable":
            return _disable(layout, args)
        case "purge":
            return _purge(layout, args)
        case _:
            raise NucleaError(
                ErrorCode.INPUT_MALFORMED,
                f"未知的 plugins 子命令 {action!r}。",
                detail={
                    "known": [
                        "install",
                        "update",
                        "uninstall",
                        "list",
                        "enable",
                        "disable",
                        "purge",
                    ]
                },
            )


def _require_global(options: Options, action: str) -> None:
    if options.instance is None and options.instance_dir is None and not options.overrides:
        return
    raise NucleaError(
        ErrorCode.INPUT_MALFORMED,
        f"nm plugins {action} 是全局操作，不接受实例参数或 --set。",
        detail={"scope": "global"},
    )


def _plugin_id(args: Sequence[str], usage: str, *, flags: Sequence[str] = ()) -> str:
    """摘出唯一的位置参数。选项在 `flags` 里的允许出现，其余一律拒绝。"""
    positional = [item for item in args if not item.startswith("-")]
    unknown = [item for item in args if item.startswith("-") and item not in flags]
    if len(positional) != 1 or unknown:
        raise NucleaError(
            ErrorCode.INPUT_MALFORMED,
            "要给出且只给出一个插件 id。",
            detail={"usage": usage, "unknown": sorted(unknown)},
        )
    return positional[0]


# ------------------------------------------------------------------------ list


def _list(options: Options, args: Sequence[str]) -> int:
    unknown = set(args) - {"--json"}
    if unknown:
        raise NucleaError(
            ErrorCode.INPUT_MALFORMED, "未知选项。", detail={"unknown": sorted(unknown)}
        )
    inspection = inspect_plugins(
        instance_dir=options.instance_dir,
        instance=options.instance,
        overrides=options.overrides,
    )
    if "--json" in args:
        payload: dict[str, JsonValue] = {
            "instance_dir": str(inspection.loaded.layout.root),
            "plugins": [status.to_json() for status in inspection.statuses],
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return 0

    sys.stdout.write(f"实例目录：{inspection.loaded.layout.root}\n")
    statuses = inspection.statuses
    if not statuses:
        # 「没有插件」是 `EDG-101` 要求的可用形态，说一句确认比印一张空表清楚。
        sys.stdout.write("\n没有发现任何外部插件（内建能力见 nm capabilities）。\n")
        return 0
    sys.stdout.write(f"\n已发现插件（{len(statuses)}）：\n")
    for status in statuses:
        sys.stdout.write(_render(status))
    return 0


def _render(status: PluginStatus) -> str:
    """一条插件记录。

    `reason` 直接印 `inventory._SKIP_REASONS` 那张表里的原话——`D25` 定死跳过原因的文案
    只有一份，CLI 侧再写一份会让「为什么没加载」有两种说法。
    """
    version = f"  {status.version}" if status.version else ""
    lines = [f"  {status.plugin_id}{version}  [{status.state.value}]\n"]
    if status.reason:
        lines.append(f"      原因：{status.reason}\n")
    if status.capabilities:
        lines.append(f"      能力：{', '.join(status.capabilities)}\n")
    if status.failure is not None:
        phase = f"（{status.failed_phase}）" if status.failed_phase else ""
        lines.append(f"      失败{phase}：{status.failure.user_message}\n")
        for key, value in sorted(status.failure.detail.items()):
            lines.append(f"        {key}: {value}\n")
    return "".join(lines)


# --------------------------------------------------------- install / update / uninstall


def _source(args: Sequence[str]) -> tuple[str, bool]:
    positional = [item for item in args if not item.startswith("-")]
    unknown = [item for item in args if item.startswith("-") and item != "--no-deps"]
    if len(positional) != 1 or unknown or args.count("--no-deps") > 1:
        raise NucleaError(
            ErrorCode.INPUT_MALFORMED,
            "要给出且只给出一个插件安装来源。",
            detail={"usage": "nm plugins install <包名、路径或 URL>"},
        )
    return positional[0], "--no-deps" not in args


def _install(home: GlobalPluginHome, args: Sequence[str]) -> int:
    source, with_dependencies = _source(args)
    with home.mutation():
        record = install_plugin(home, source, with_dependencies=with_dependencies)
    sys.stdout.write(
        f"{record.plugin_id} {record.version}: 已全局安装到 {home.package_dir(record.plugin_id)}。\n"
        "它尚未在任何实例启用；使用 nm plugins enable <插件 id>。\n"
    )
    return 0


def _update(home: GlobalPluginHome, args: Sequence[str]) -> int:
    plugin_id = _plugin_id(args, "nm plugins update <插件 id>")
    with home.mutation():
        record = update_plugin(home, plugin_id)
    sys.stdout.write(f"{record.plugin_id}: 已全局更新到 {record.version}。\n")
    return 0


def _uninstall(home: GlobalPluginHome, args: Sequence[str]) -> int:
    """删除全局代码与实例配置引用；实例业务状态由 ``purge`` 单独管理。"""
    plugin_id = _plugin_id(args, "nm plugins uninstall <插件 id>")
    if plugin_id not in {item.plugin_id for item in home.catalog()}:
        sys.stdout.write(f"{plugin_id}: 尚未全局安装。\n")
        return 3
    with home.mutation():
        # 依赖阻塞属于纯 preflight；必须在第一份实例配置写盘之前发现。
        validate_uninstall(home, plugin_id)
        updates: list[
            tuple[Path, dict[str, JsonValue], dict[str, JsonValue]]
        ] = []
        state_dirs: list[Path] = []
        for root in home.instances():
            config_path = root / "config.json"
            if not config_path.is_file():
                continue
            document = read_document(config_path)
            enabled = remove_from_list(document, _PLUGINS, _ENABLED, plugin_id)
            disabled = remove_from_list(enabled.document, _PLUGINS, _DISABLE, plugin_id)
            entry = remove_plugin_entry(disabled.document, plugin_id)
            if enabled.changed or disabled.changed or entry.changed:
                updates.append((config_path, document, entry.document))
            state = root / "plugins" / plugin_id
            if state.is_dir():
                state_dirs.append(state)
        _commit_uninstall(home, plugin_id, updates)
    sys.stdout.write(
        f"{plugin_id}: 已从全局插件目录卸载，并清理 {len(updates)} 个实例的配置引用。\n"
        "被覆盖的内建能力会在实例下次启动时恢复。\n"
    )
    for state in state_dirs:
        sys.stdout.write(
            f"状态目录仍保留：{state}\n"
            f"  要删除它：nm plugins purge {plugin_id} --confirm "
            f"--instance-dir {state.parent.parent}\n"
        )
    return 0


def _commit_uninstall(
    home: GlobalPluginHome,
    plugin_id: str,
    updates: Sequence[
        tuple[Path, dict[str, JsonValue], dict[str, JsonValue]]
    ],
) -> None:
    """把多实例配置修改与全局代码删除提交成一个可回滚操作。

    单个 ``config.json`` 和全局目录各自已经原子替换，但跨文件系统路径不存在通用的原子
    rename。这里用补偿事务连接两者：任何配置写入或全局卸载失败，已写配置都按逆序恢复。
    ``uninstall_plugin()`` 自身在目录与 catalog 之间也有回滚，因此成功返回后没有后续的
    可失败步骤。
    """
    applied: list[tuple[Path, dict[str, JsonValue]]] = []
    try:
        for path, before, after in updates:
            write_document(path, after)
            applied.append((path, before))
        uninstall_plugin(home, plugin_id)
    except BaseException as error:
        rollback_failures: list[str] = []
        for path, before in reversed(applied):
            try:
                write_document(path, before)
            except Exception:
                rollback_failures.append(str(path))
        if rollback_failures:
            raise NucleaError(
                ErrorCode.PERSISTENCE_WRITE_FAILED,
                "插件卸载失败，且部分实例配置无法自动恢复。",
                detail={"plugin_id": plugin_id, "paths": rollback_failures},
            ) from error
        raise


# --------------------------------------------------------------- enable / disable


def _enable(layout: InstanceLayout, args: Sequence[str]) -> int:
    """启用外部插件，或撤销对内建插件的禁用。

    外部插件写入 ``plugins.enabled``；内建插件已经随 Kernel 安装，只需从
    ``plugins.disable`` 摘掉。两种路径都不能留下相反指令，否则一次明确的“启用”会静默
    失效。
    """
    plugin_id = _plugin_id(args, "nm plugins enable <插件 id>")
    installed = {item.plugin_id for item in GlobalPluginHome.resolve().catalog()}
    builtin = plugin_id in {manifest.id for manifest in BUILTIN_MANIFESTS}
    if plugin_id not in installed and not builtin:
        raise NucleaError(
            ErrorCode.PLUGIN_LOAD_FAILED,
            "插件尚未全局安装，不能在实例中启用。",
            detail={"plugin_id": plugin_id, "suggestion": "nm plugins install <来源>"},
        )
    document = read_document(layout.config_path)
    if builtin:
        undisabled = remove_from_list(document, _PLUGINS, _DISABLE, plugin_id)
        if not undisabled.changed:
            sys.stdout.write(f"{plugin_id}: 本来就已启用。\n")
            return 3
        write_document(layout.config_path, undisabled.document)
        sys.stdout.write(f"{plugin_id}: 已从 plugins.disable 移除，内建插件恢复启用。\n")
        sys.stdout.write(_RESTART_HINT + "\n")
        return 0

    added = add_to_list(document, _PLUGINS, _ENABLED, plugin_id)
    undisabled = remove_from_list(added.document, _PLUGINS, _DISABLE, plugin_id)
    if not added.changed and not undisabled.changed:
        sys.stdout.write(f"{plugin_id}: 本来就已启用。\n")
        return 3
    write_document(layout.config_path, undisabled.document)
    if added.changed:
        sys.stdout.write(f"{plugin_id}: 已写入 plugins.enabled。\n")
    if undisabled.changed:
        sys.stdout.write(f"{plugin_id}: 同时从 plugins.disable 移除（禁用会压过启用）。\n")
    sys.stdout.write(_RESTART_HINT + "\n")
    return 0


def _disable(layout: InstanceLayout, args: Sequence[str]) -> int:
    """写入 `plugins.disable`。**不动 `enabled`**——那样 `enable` 才是它的逆操作。

    对内建同样有效（`plugins.disable` 是按提供方禁用）。唯一被拒的是 CLI 入口，
    那条判定在装配根（`EDG-108`），启动时才报——这里不抄一遍，否则两份判定会分叉。
    """
    plugin_id = _plugin_id(args, "nm plugins disable <插件 id>")
    document = read_document(layout.config_path)
    edit = add_to_list(document, _PLUGINS, _DISABLE, plugin_id)
    if not edit.changed:
        sys.stdout.write(f"{plugin_id}: 本来就已禁用。\n")
        return 3
    write_document(layout.config_path, edit.document)
    sys.stdout.write(f"{plugin_id}: 已写入 plugins.disable。\n")
    sys.stdout.write(_on_disable_hint(layout, plugin_id))
    sys.stdout.write(_RESTART_HINT + "\n")
    return 0


def _on_disable_hint(layout: InstanceLayout, plugin_id: str) -> str:
    """刚被禁用的插件覆盖过别的能力时，提前把 `on_disable` 那条要求说出来（`D30`）。

    不说的话用户看到的是「已写入」，然后下一次启动以 `CONFIG_INVALID` 失败。判定**不在
    这里重写**——它只有 `runtime/plugin_disable.py` 一处，这里只是把同一件事提前一步告诉
    用户，因此写没写 `on_disable` 都印同一段话。

    读不出清单时（配置在别处坏了、插件包已经卸了）**不提示也不失败**：这条命令的正事
    已经做完了，为一句提示让它失败是本末倒置。
    """
    from ...plugin_disable import override_targets

    try:
        inventory = inspect_plugins(instance_dir=layout.root).inventory
    except NucleaError:
        return ""
    overridden: list[str] = []
    for item in inventory.skipped:
        if item.candidate.plugin_id != plugin_id:
            continue
        manifest = item.manifest
        if manifest is None:
            continue
        overridden.extend(target.target for target in override_targets(manifest))
    if not overridden:
        return ""
    return (
        f"它覆盖过 {'、'.join(overridden)}，因此还要说明那项能力怎么办：\n"
        f'  在 config.json 的 plugins.{plugin_id} 里写 "on_disable"——\n'
        "  restore_builtin（被顶掉的实现重新生效）或 leave_missing（保持缺失）。\n"
    )


# ----------------------------------------------------------------------- purge


def _purge(layout: InstanceLayout, args: Sequence[str]) -> int:
    """删除插件的状态目录。**没有 `--confirm` 就只打印，不删任何东西。**

    路径与体积在确认之前打印（`EDG-505`）：一句「确定吗」不足以让用户知道自己将要失去
    什么，而这是本命令唯一不可撤销的动作。
    """
    plugin_id = _plugin_id(args, "nm plugins purge <插件 id> --confirm", flags=["--confirm"])
    state_dir = layout.plugins_dir / plugin_id
    if not state_dir.is_dir():
        sys.stdout.write(f"{plugin_id}: 没有状态目录可删（{state_dir}）。\n")
        return 3
    files, total = _measure(state_dir)
    sys.stdout.write(f"将删除：{state_dir}\n  {files} 个文件，共 {_human(total)}\n")
    if "--confirm" not in args:
        sys.stdout.write("未删除任何东西。确认后重跑：nm plugins purge "
                         f"{plugin_id} --confirm\n")
        return 3
    try:
        shutil.rmtree(state_dir)
    except OSError as exc:
        raise NucleaError(
            ErrorCode.PERSISTENCE_WRITE_FAILED,
            "无法删除插件状态目录。",
            detail={"path": str(state_dir), "errno": exc.errno},
        ) from exc
    sys.stdout.write(f"{plugin_id}: 状态目录已删除。\n")
    return 0


def _measure(root: Path) -> tuple[int, int]:
    """`(文件数, 字节数)`。

    读不动的条目按 0 计而不是让整条命令失败：这段数字是给人看的量级参考，
    为一个权限不足的文件放弃打印，用户就连「大概多大」都不知道了。
    """
    files = 0
    total = 0
    for path in root.rglob("*"):
        try:
            if not path.is_file():
                continue
            total += path.stat().st_size
        except OSError:  # pragma: no cover - 平台相关的防御分支。
            continue
        files += 1
    return files, total


def _human(size: int) -> str:
    """人读的体积。`EDG-505` 要的是「打印体积」，而 `13421772 字节` 不是给人读的。"""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"  # pragma: no cover - 上面的循环已经覆盖了全部出口。
