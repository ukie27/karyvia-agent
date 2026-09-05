"""改写 `config.json` 里的一个字符串列表（`D29`，技术方案 §10.4）。

职责：把 `karyvia plugins enable / disable / uninstall` 的意图落到磁盘上——读 `config.json`
**那一层**、增删 `plugins.enabled` / `plugins.disable` 里的一项、原子替换写回。
不负责：生成初始配置（`first_run.py`，那是「没有就建」）、合并四层与校验
（`kernel/config/`）、决定谁调它（`runtime/cli/commands/plugins.py`）、解析 `${VAR}`。

**这是全项目第二个、也是唯一一个「修改既有 `config.json`」的地方**。与 `first_run.py`
的分工是硬的：那边只用 `O_CREAT|O_EXCL` 建新文件、既有文件一个字节都不动；这边只在文件
**已经存在**时改其中一个列表。`kernel/config/` 仍然一个字节都不写（`EDG-501`）。

三条约束让「改用户的配置」不至于毁掉别的东西：

- **只读写 `config.json` 那一层**，不是 `LoadedConfig` 那棵合并树。写回合并结果会把
  四十多个默认值、`KARYVIA_CFG_*` 与 `--set` 的临时覆盖一并物化进文件，
  `karyvia config show --origins` 从此答不出「我改过什么」（`D24`「模板只放用户真的要改的
  键」是同一条理由）。
- **从不解析 secret**，因此写回时没有别的东西可写：文件里原本是什么 `${VAR}` 字面量，
  写回去还是什么（`CFG-003` 的结构性保证）。`D11` 的 `prepare_for_write()` 是给「解析过
  之后又要落盘」准备的闸门，这条路上解析从未发生，用不上它。
- **原子替换**：同目录临时文件 → `fsync` → `os.replace`。中途失败时原文件一个字节没动，
  与 `session_jsonl` 的整批原子性、`first_run._write_derived` 是同一种做法。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from karyvia.contracts import ErrorCode, JsonValue, KaryviaError
from karyvia.kernel.config import read_config_file

__all__ = [
    "ListEdit",
    "add_to_list",
    "read_document",
    "remove_from_list",
    "remove_plugin_entry",
    "write_document",
]


@dataclass(frozen=True, slots=True)
class ListEdit:
    """一次列表改动的结果。`changed=False` 表示目标状态本来就成立。

    「本来就启用着」不是错误，但也不该报告成「已改动」——调用方据此决定退出码
    （没事可做时返回 3）。
    """

    document: dict[str, JsonValue]
    changed: bool
    values: tuple[str, ...]


def read_document(path: Path) -> dict[str, JsonValue]:
    """读 `config.json` 那一层，交出可改的副本。

    **异常约定**：文件不存在抛 `CONFIG_INVALID` 并指路 `karyvia init`——`read_config_file()`
    对缺文件返回空映射（默认值本身就是一份合法配置），但**写**这条路上不能沿用那条语义：
    凭空造一份只有 `plugins.enabled` 的配置会绕过首次运行的模板与 `config.schema.json`。
    JSON 坏掉、不是对象、过大都由 `read_config_file()` 原样抛出。
    """
    if not path.exists():
        raise KaryviaError(
            ErrorCode.CONFIG_INVALID,
            "实例还没有 config.json，先跑一次 karyvia init。",
            detail={"path": str(path), "suggestion": "karyvia init"},
        )
    return dict(read_config_file(path))


def _section(document: Mapping[str, JsonValue], section: str) -> dict[str, JsonValue]:
    """取出一个小节的可改副本。缺失即空对象——那是「还没写过」，不是错误。"""
    value = document.get(section)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise _malformed(f"/{section}", "对象", value)
    return dict(value)


def _string_list(section: Mapping[str, JsonValue], section_name: str, key: str) -> list[str]:
    """取出一个字符串列表的可改副本。

    **形状不对即拒绝，不静默修正**（原则 7）：把 `"enabled": "acme"` 当成 `["acme"]`
    会让用户的下一次 `karyvia config show` 看到一份他没写过的配置。
    """
    value = section.get(key)
    if value is None:
        return []
    pointer = f"/{section_name}/{key}"
    if not isinstance(value, (list, tuple)):
        raise _malformed(pointer, "字符串数组", value)
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise _malformed(f"{pointer}/{index}", "字符串", item)
        items.append(item)
    return items


def _malformed(pointer: str, expected: str, actual: JsonValue) -> KaryviaError:
    return KaryviaError(
        ErrorCode.CONFIG_INVALID,
        f"配置里 {pointer} 的形状不对，应当是{expected}；请先手工修正它。",
        detail={"pointer": pointer, "expected": expected, "actual_type": type(actual).__name__},
    )


def _apply(
    document: Mapping[str, JsonValue],
    section_name: str,
    key: str,
    values: Sequence[str],
) -> dict[str, JsonValue]:
    """把改好的列表放回文档。返回新文档，入参不变。"""
    section = _section(document, section_name)
    section[key] = list(values)
    updated = dict(document)
    updated[section_name] = section
    return updated


def add_to_list(
    document: Mapping[str, JsonValue], section_name: str, key: str, value: str
) -> ListEdit:
    """把一项加进列表末尾。已经在里面就原样返回（`changed=False`）。

    **追加而不是插入后排序**：这份文件是用户的资产，重排他写的顺序是没必要的改动。
    """
    section = _section(document, section_name)
    items = _string_list(section, section_name, key)
    if value in items:
        return ListEdit(document=dict(document), changed=False, values=tuple(items))
    items.append(value)
    return ListEdit(
        document=_apply(document, section_name, key, items), changed=True, values=tuple(items)
    )


def remove_from_list(
    document: Mapping[str, JsonValue], section_name: str, key: str, value: str
) -> ListEdit:
    """把一项从列表里删掉（重复项一并删）。不在里面就原样返回（`changed=False`）。"""
    section = _section(document, section_name)
    items = _string_list(section, section_name, key)
    kept = [item for item in items if item != value]
    if len(kept) == len(items):
        return ListEdit(document=dict(document), changed=False, values=tuple(items))
    return ListEdit(
        document=_apply(document, section_name, key, kept), changed=True, values=tuple(kept)
    )


def remove_plugin_entry(
    document: Mapping[str, JsonValue], plugin_id: str
) -> ListEdit:
    """移除 ``plugins.<id>`` 的实例配置块，供全局卸载收回所有实例引用。"""
    section = _section(document, "plugins")
    if plugin_id not in section:
        return ListEdit(document=dict(document), changed=False, values=())
    del section[plugin_id]
    updated = dict(document)
    updated["plugins"] = section
    return ListEdit(document=updated, changed=True, values=())


def write_document(path: Path, document: Mapping[str, JsonValue]) -> None:
    """原子写回。

    **不排序键**：`sort_keys=True` 会把用户手写的顺序整份打乱，而这次改动只碰了一个列表。
    末尾留一个换行，`$schema` 与缩进沿用 `karyvia init` 生成的那套（2 空格、不转义非 ASCII）。

    **异常约定**：写盘失败抛 `PERSISTENCE_WRITE_FAILED`（带路径与 errno，**不带内容**）。
    临时文件与目标同目录——跨设备的 `os.replace` 不是原子的。
    """
    text = json.dumps(dict(document), ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        # 失败时清掉半份临时文件；清不掉就算了，原文件反正没被动过。
        try:
            tmp.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - 清理失败不该盖住真正的原因。
            pass
        raise KaryviaError(
            ErrorCode.PERSISTENCE_WRITE_FAILED,
            "无法写回配置文件。",
            detail={"path": str(path), "errno": exc.errno},
        ) from exc
