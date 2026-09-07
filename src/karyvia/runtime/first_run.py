"""首次运行：生成最小 `config.json` 与它的 JSON Schema。

职责：把 `kernel/config/scaffold.py` 渲染出来的初始配置**落盘**（绝不覆盖既有文件），
写出派生的 `config.schema.json`，并渲染一段「填哪个文件、哪个字段、导出哪个变量」的指引。
不负责：决定什么时候调它（`karyvia init` 显式调、`karyvia run` 在配置缺失时调一次）、加载配置
（`kernel/config/loader.py`）、装配实例（`bootstrap.py`）。

**这里是 `config.json` 的创建点，`config_edit.py` 是它唯一的修改点**。
`kernel/config/` 一个字节都不写，它只以 `"rb"` 读；生成属于装配层的职责，
因此落在 `runtime/`。分工是硬的：本模块只用 `O_CREAT|O_EXCL` 建**新**文件、既有文件一个
字节都不动；`karyvia plugins enable` 那种「已经有了，改其中一个列表」走另一边。

**用 `O_CREAT|O_EXCL` 而不是「先判断存不存在再写」**： 要的是「不得静默覆盖
原文件」，而 exists-then-write 之间有一个窗口——两个 `karyvia init` 同时跑就会有一个把另一个
刚写的配置盖掉。`O_EXCL` 让「没有就建、有就退让」是一次原子操作，因此本模块**没有**
`--force` 这类开关：一个能覆盖用户配置的旋钮就是那条需求的反面。

**默认模型的四个事实各写一份**（与 `builtins/model_openai/` 对照，见下面的常量）：
`karyvia init` 不该为了读四个字符串把 httpx 拉进进程——`model_openai/__init__.py` 一路 import
到 `provider.py`，而 既定冷启动预算里没有这笔钱。对照由
`tests/runtime/test_first_run.py::test_defaults_match_the_builtin_model_provider` 钉住，
与 `estimate_tokens` / `DEFAULT_GRACE_MS` 各写一份是同一种做法。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from karyvia.contracts import ErrorCode, KaryviaError
from karyvia.kernel.config import (
    JSON_SCHEMA_FILENAME,
    InitialConfig,
    InstanceLayout,
    build_initial_config,
    config_json_schema,
    render_json,
)

__all__ = [
    "DEFAULT_MODEL_NAME",
    "MODEL_API_KEY_ENV",
    "MODEL_PLUGIN_ID",
    "MODEL_PROVIDER_NAME",
    "MODEL_SECRET_NAME",
    "FirstRunResult",
    "ensure_initial_config",
    "guidance_lines",
    "initial_config",
]

#: 内建模型供应商的能力名（`model_openai.CAPABILITY_NAME`）。
MODEL_PROVIDER_NAME: Final = "openai"

#: 它的 manifest id（`builtins/registry.py`）。配置块与凭据都挂在这个 id 下。
MODEL_PLUGIN_ID: Final = "model-openai"

#: 它的凭据名（`model_openai.SECRET_NAME`，固定不可配置）。
MODEL_SECRET_NAME: Final = "api_key"

#: 模板里引用的环境变量。**只有名字进配置文件**，值永远只在环境里。
MODEL_API_KEY_ENV: Final = "OPENAI_API_KEY"

#: 模板里的默认模型。挑一个便宜、广泛可用、且带工具调用的：首次运行的那次 turn 要能真的
#: 跑起来。换模型只需改 `/model/name` 一行，这正是把它写进模板（而不是留空）的理由。
DEFAULT_MODEL_NAME: Final = "gpt-4o-mini"


def initial_config() -> InitialConfig:
    """用内建模型供应商的事实组装一份初始配置。纯函数，不碰磁盘与环境变量。"""
    return build_initial_config(
        model_name=DEFAULT_MODEL_NAME,
        model_provider=MODEL_PROVIDER_NAME,
        plugin_secrets={MODEL_PLUGIN_ID: {MODEL_SECRET_NAME: f"${{{MODEL_API_KEY_ENV}}}"}},
    )


@dataclass(frozen=True, slots=True)
class FirstRunResult:
    """一次 `ensure_initial_config()` 的结果。"""

    config_path: Path
    schema_path: Path
    #: `config.json` 是不是这一次建的。既有文件一律不动，这时它是 `False`。
    created: bool
    #: 配置里引用到的环境变量名。
    required_env: tuple[str, ...]
    #: 其中尚未导出（或导出成空串）的那些。**只有名字**。
    missing_env: tuple[str, ...]

    @property
    def ready(self) -> bool:
        """凭据是否已就绪——再跑一次 `karyvia run` 就能对话。"""
        return not self.missing_env


def _write_new(path: Path, text: str) -> bool:
    """`O_EXCL` 建文件。已存在返回 `False` 且**不写一个字节**（见模块 docstring）。"""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except OSError as exc:
        raise _write_failed(path, exc) from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise _write_failed(path, exc) from exc
    return True


def _write_derived(path: Path, text: str) -> None:
    """写一份**由我们生成**的文件（`config.schema.json`）。

    与 `config.json` 不同，它不是用户的资产：内容与当前 `SECTION_SPECS` 不一致就该被刷新，
    否则编辑器会拿一份过期的 schema 对着新字段报假错。内容相同则一个字节都不写——
    免得每次 `karyvia init` 都改一次 mtime。
    """
    try:
        if path.exists() and path.read_text(encoding="utf-8") == text:
            return
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    except OSError as exc:
        raise _write_failed(path, exc) from exc


def _write_failed(path: Path, exc: OSError) -> KaryviaError:
    return KaryviaError(
        ErrorCode.PERSISTENCE_WRITE_FAILED,
        "无法写入初始配置。",
        detail={"path": str(path), "errno": exc.errno},
    )


def ensure_initial_config(
    layout: InstanceLayout, *, env: Mapping[str, str] | None = None
) -> FirstRunResult:
    """确保实例目录里有一份 `config.json`。**既有文件永不改动。**

    **异常约定**：写盘失败抛 `PERSISTENCE_WRITE_FAILED`（带路径与 errno，不带内容）；
    其余情形不抛——「已经有配置了」是正常结果，由 `created=False` 表达。
    """
    layout.ensure()
    initial = initial_config()
    created = _write_new(layout.config_path, initial.text)
    schema_path = layout.root / JSON_SCHEMA_FILENAME
    _write_derived(schema_path, render_json(config_json_schema()))

    environ = os.environ if env is None else env
    missing = tuple(name for name in initial.required_env if not environ.get(name))
    return FirstRunResult(
        config_path=layout.config_path,
        schema_path=schema_path,
        created=created,
        required_env=initial.required_env,
        missing_env=missing,
    )


def guidance_lines(result: FirstRunResult) -> tuple[str, ...]:
    """渲染「填哪个文件、哪个字段」的指引。

    **只印变量名，绝不印值**——本模块从头到尾就没有读过任何凭据的值。
    """
    lines = [
        ("已生成初始配置：" if result.created else "配置已存在："),
        f"  {result.config_path}",
        f"  {result.schema_path}（供编辑器补全，运行期忽略）",
        "",
    ]
    if result.missing_env:
        names = "、".join(result.missing_env)
        lines += [
            f"还差一步：导出模型凭据 {names}，然后再跑一次 karyvia run。",
            f"  凭据引用写在 /plugins/{MODEL_PLUGIN_ID}/secrets/{MODEL_SECRET_NAME}，"
            "配置文件里只有变量名，没有凭据本身。",
            f"  要换模型改 /model/name（当前 {DEFAULT_MODEL_NAME}）。",
            "",
            f"  Linux/macOS：export {MODEL_API_KEY_ENV}=<你的密钥>",
            f"  Windows：    set {MODEL_API_KEY_ENV}=<你的密钥>",
            "",
            f"  用本地模型服务（Ollama / vLLM / LM Studio）则不需要凭据："
            f'在 /plugins/{MODEL_PLUGIN_ID}/config 里把 base_url 指过去、auth 设成 "none"。',
        ]
    else:
        lines += [
            f"凭据 {'、'.join(result.required_env)} 已就绪，再跑一次 karyvia run 即可开始对话。",
            f"  要换模型改 /model/name（当前 {DEFAULT_MODEL_NAME}）。",
        ]
    return tuple(lines)
