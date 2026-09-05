"""从 `SECTION_SPECS` 派生一份 JSON Schema（`D24`，`EDG-506`）。

职责：把字段表翻译成一份 Draft 2020-12 的 JSON Schema 文档，供 `karyvia init` 写进实例目录、
被生成的 `config.json` 用 `$schema` 引用，从而让编辑器给出补全与就地校验。
不负责：校验配置（那是 `schema.validate_config()`，**运行期的唯一判定**）、读写任何文件
（写盘在 `runtime/first_run.py`）、决定初始配置里放哪些键（`scaffold.py`）。

**这份 schema 是派生物，不是第二份真相**。`SECTION_SPECS` 仍然是字段、默认值与
`extra="forbid"` 的唯一依据；本模块只把它翻译成另一种表示。因此这里**一个字段名都不
认识**——与 `fields.py` 同一条分界线。字段表长出新项时，这份 schema 自动跟着长，
`test_json_schema_covers_every_known_field` 是那条自动性的可执行形态。

**编辑器里的校验与运行期的校验不等价，这是刻意的**：JSON Schema 表达不了
「`plugins.<id>` 的未知键是插件 id 而不是拼错」这类规则，也表达不了 `${VAR}` 的语义。
凡是 schema 说不清的地方一律**放宽**（`additionalProperties: true`），让编辑器少报假错；
真正的把关永远在 `validate_config()`。反过来做会让用户对着一份写对了的配置看到红波浪线。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Mapping

from .fields import FieldKind, FieldSpec
from .plugin_blocks import CONFIG_KEY, PLUGINS_SECTION, SECRETS_KEY
from .schema import SCHEMA_KEY, SECTION_SPECS

if TYPE_CHECKING:
    from ...contracts import JsonValue

__all__ = ["JSON_SCHEMA_DIALECT", "JSON_SCHEMA_FILENAME", "config_json_schema"]

#: 生成的 schema 文件名。`config.json` 用**相对路径**引用它（两个文件同在实例目录里），
#: 绝对路径会让实例目录搬家之后引用失效。
JSON_SCHEMA_FILENAME: Final = "config.schema.json"

JSON_SCHEMA_DIALECT: Final = "https://json-schema.org/draft/2020-12/schema"

#: `FieldKind` -> JSON Schema 的类型片段。六种形状各一条，缺项在 `_field_schema` 里
#: 直接 KeyError——一个没有 schema 表示的字段类型不该悄悄退化成「任意值」。
_KIND_SCHEMAS: Final[Mapping[FieldKind, Mapping[str, JsonValue]]] = {
    FieldKind.BOOL: {"type": "boolean"},
    FieldKind.POSITIVE_INT: {"type": "integer", "exclusiveMinimum": 0},
    FieldKind.OPTIONAL_POSITIVE_INT: {
        "type": ["integer", "null"],
        "exclusiveMinimum": 0,
    },
    FieldKind.STR: {"type": "string"},
    FieldKind.OPTIONAL_STR: {"type": ["string", "null"]},
    FieldKind.STR_LIST: {"type": "array", "items": {"type": "string"}},
}

#: `plugins` 小节里每个插件条目的形状（`kernel/config/plugin_blocks.py` 的可执行形态）。
#: `config` 放任何键：逐字段校验用的是 manifest 自带的 `config_schema`（`D25` 阶段 A），
#: 而编辑器在打开 `config.json` 时并不知道装了哪些插件。
#: `secrets` 的值只能是字符串——那是 `${VAR}` 引用，不是嵌套结构。
_PLUGIN_ENTRY_SCHEMA: Final[Mapping[str, JsonValue]] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        CONFIG_KEY: {"type": "object"},
        SECRETS_KEY: {"type": "object", "additionalProperties": {"type": "string"}},
    },
}


def _field_schema(spec: FieldSpec) -> dict[str, JsonValue]:
    """一个字段的 schema 片段：类型 + 默认值 + 受限取值。

    `default` 写进去是为了编辑器把它显示成提示，**不是**为了让 schema 参与取默认值——
    默认值的唯一来源仍是 `SECTION_SPECS`（`CFG-005` 的来源追踪依赖这一点）。

    **元组要转成列表**：`STR_LIST` 字段的默认值在字段表里是 `()`，而 JSON 里没有元组。
    `json.dumps` 会替我们转，但那样这份文档就只在**序列化之后**才是合法的 JSON Schema，
    而它同时也是被直接传给 `jsonschema.validate()` 的那个对象。
    """
    fragment: dict[str, JsonValue] = dict(_KIND_SCHEMAS[spec.kind])
    default = spec.default
    fragment["default"] = list(default) if isinstance(default, tuple) else default
    if spec.choices:
        fragment["enum"] = list(spec.choices)
    return fragment


def _section_schema(fields: Mapping[str, FieldSpec], *, section: str) -> dict[str, JsonValue]:
    """一个小节的 schema。

    **只有 `plugins` 放行未知键**，且理由与 `_validate_section` 完全相同：那些键是插件
    id。其余小节 `additionalProperties: false`，让编辑器当场标出拼错的字段名——那正是
    `CONFIG_UNKNOWN_FIELD` 想帮用户避免的往返。
    """
    properties: dict[str, JsonValue] = {
        name: _field_schema(spec) for name, spec in fields.items()
    }
    if section == PLUGINS_SECTION:
        return {
            "type": "object",
            "properties": properties,
            "additionalProperties": _PLUGIN_ENTRY_SCHEMA,
        }
    return {"type": "object", "additionalProperties": False, "properties": properties}


def config_json_schema(*, title: str = "Karyvia 实例配置") -> dict[str, JsonValue]:
    """派生整份 schema。纯函数：同一份 `SECTION_SPECS` 永远给出同一份文档。"""
    properties: dict[str, JsonValue] = {
        SCHEMA_KEY: {
            "type": "string",
            "description": "本文件的 schema 引用；运行期忽略。",
        }
    }
    for section, fields in SECTION_SPECS.items():
        properties[section] = _section_schema(fields, section=section)
    return {
        SCHEMA_KEY: JSON_SCHEMA_DIALECT,
        "title": title,
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }
