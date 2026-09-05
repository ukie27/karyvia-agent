"""首次运行的最小配置模板（`D24`，`EDG-506`、`BAS-006`）。

职责：把「一份刚好能跑起来的 `config.json` 长什么样」组装成一个已自校验的文档 +
它的 JSON 文本 + 它需要哪些环境变量（`${VAR}` 引用）。
不负责：写盘（`runtime/first_run.py`——`kernel/config/` 全包一个字节都不写，`EDG-501`）、
决定用哪个模型供应商（那是装配根的事，见下）、派生 JSON Schema（`json_schema.py`）。

**本模块不认识任何具体内建**。模板需要一个模型名与一份 `plugins.<id>.secrets`，但
「默认模型供应商叫 model-openai、它的凭据叫 api_key、习惯上从 `OPENAI_API_KEY` 取」
全都是内建的事实，而 `R2` 禁止 `kernel/` 够到 `builtins/`。因此那几样由调用方传进来
（`runtime/first_run.py` 知道它们），本模块只负责**形状**与两条保证：

1. **生成的配置一定能被自己加载。** `build_initial_config()` 在返回之前把文档过一遍
   `validate_config()`。这不是防御性编程——`$schema` 那条顶层例外若哪天被删掉，最先受害
   的就是刚 `karyvia init` 完的用户，而他会看到「配置无效」并且完全不知道是我们生成的。
   在这里失败，失败的是我们的测试。
2. **文本形态跨平台一致**：`\\n` 换行、UTF-8 无 BOM、两空格缩进、末尾一个换行。
   `json.dumps` 默认会把非 ASCII 转义，`ensure_ascii=False` 让中文注释性字段保持可读。

**模板里只放用户真的要改的键**。把 `defaults()` 整份倒进去看起来「更完整」，实际是把
四十多个字段变成用户不敢动的噪声，而且每一个都会被 `karyvia config show --origins` 记成
「来自 config.json」——那让「我改过什么」这个问题永远答不上来。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Mapping

from .json_schema import JSON_SCHEMA_FILENAME
from .schema import SCHEMA_KEY, validate_config
from .secrets import scan_secret_refs

if TYPE_CHECKING:
    from ...contracts import JsonValue

__all__ = ["InitialConfig", "build_initial_config", "render_json"]

#: 生成的 JSON 缩进。两空格：这是一份要被手工编辑的文件。
_INDENT: Final = 2


def render_json(document: Mapping[str, JsonValue]) -> str:
    """渲染成要落盘的文本。见模块 docstring 的第 2 条保证。"""
    return json.dumps(dict(document), indent=_INDENT, ensure_ascii=False) + "\n"


@dataclass(frozen=True, slots=True)
class InitialConfig:
    """一份待写入的初始配置。构造它不碰磁盘，也不读环境变量。"""

    #: 文档本体。持有 `${VAR}` **字面量**，明文自始至终不在这里（`CFG-003`）。
    document: Mapping[str, JsonValue]
    #: 要写进 `config.json` 的文本。
    text: str
    #: 需要用户导出的环境变量名，按出现顺序去重。指引就是照它印的（`EDG-502`：
    #: 只说变量名，不说值——这里根本没有值可说）。
    required_env: tuple[str, ...]


def build_initial_config(
    *,
    model_name: str,
    model_provider: str | None = None,
    plugin_secrets: Mapping[str, Mapping[str, str]] | None = None,
    schema_ref: str | None = JSON_SCHEMA_FILENAME,
) -> InitialConfig:
    """组装最小配置。

    `plugin_secrets` 形如 `{"model-openai": {"api_key": "${OPENAI_API_KEY}"}}`，落进
    `plugins.<id>.secrets`——**不是** `plugins.<id>.config`（`D19`/`D23` 定死的分界：
    凭据是 `config` 的兄弟键，因此插件自己的配置块里没有可泄漏的东西）。

    `schema_ref` 给 `None` 即不写 `$schema`（`karyvia init` 之外的调用方不必接受那个文件）。

    **异常约定**：组装出来的文档若通不过 `validate_config()`，原样抛它的
    `KaryviaError`——那是我们的 bug，不该被包装成别的东西。
    """
    document: dict[str, JsonValue] = {}
    if schema_ref is not None:
        document[SCHEMA_KEY] = f"./{schema_ref}"

    model: dict[str, JsonValue] = {"name": model_name}
    if model_provider is not None:
        model["provider"] = model_provider
    document["model"] = model

    if plugin_secrets:
        document["plugins"] = {
            plugin_id: {"secrets": dict(secrets)}
            for plugin_id, secrets in sorted(plugin_secrets.items())
        }
    validate_config(document)

    seen: dict[str, None] = {}
    for ref in scan_secret_refs(document):
        for name in ref.names:
            seen[name] = None

    return InitialConfig(
        document=document,
        text=render_json(document),
        required_env=tuple(seen),
    )
