"""示例插件 `echo-tool`：一个插件最少要写的东西（开发方案 `D30`）。

职责：声明一份 `PluginManifest`，在 `setup(api)` 里注册一个工具能力 `echo.say`。
不负责：任何持久化、出网与子进程——本插件只使用 `ctx.config`，不调用其他宿主资源服务。

**只 import `nucleamind.contracts` 与 `nucleamind.sdk`**（依赖规则 `R4`）。插件够不着
`nucleamind.kernel.*`，`tests/architecture/test_import_boundaries.py` 会拦下任何尝试。

**MANIFEST 在模块顶层、导入无副作用**（技术方案 §7.2 的硬约束）：发现阶段只 import 本模块
取那个对象，此时不该发生任何 IO。真正的工作全在 `setup()` 里，而 `setup()` 只有在插件被
`plugins.enabled` 列出、且过了阶段 A 之后才会被调用。
"""

from __future__ import annotations

from typing import Final

from nucleamind.contracts import (
    CancelSignal,
    CapabilityKind,
    ErrorCode,
    JsonValue,
    NucleaError,
    RiskLevel,
    SideEffect,
    ToolInvocation,
    ToolResult,
    ToolSpec,
)
from nucleamind.sdk import CapabilityDecl, NucleaAPI, PluginContext, PluginManifest

__all__ = ["CONFIG_PREFIX_KEY", "MANIFEST", "TOOL_NAME", "EchoTool", "setup"]

#: 能力名。与 `CapabilityDecl.name` 同一个常量——写两遍字面量就会在改名时对不上，
#: 而「声明了却没注册」是 `PLUGIN_LOAD_FAILED`（`kernel/plugins/host.py`）。
TOOL_NAME: Final = "echo.say"

#: 本插件在 `plugins.echo-tool.config` 里认得的唯一一个键。
CONFIG_PREFIX_KEY: Final = "prefix"

MANIFEST: Final = PluginManifest(
    id="echo-tool",
    version="0.1.0",
    # SDK 兼容区间由**插件**声明，宿主据此判断要不要加载（`SDK-005`）。落在区间外时
    # 拒绝加载并报 `PLUGIN_SDK_INCOMPATIBLE`，不带病运行。
    sdk_range=">=5.0.0,<6.0.0",
    setup="nucleamind_plugin_echo_tool:setup",
    capabilities=(CapabilityDecl(kind=CapabilityKind.TOOL, name=TOOL_NAME),),
    config_schema={
        "type": "object",
        "properties": {
            CONFIG_PREFIX_KEY: {
                "type": "string",
                "description": "回显时加在前面的字符串。",
            }
        },
        "additionalProperties": False,
    },
)


class EchoTool:
    """把 `text` 原样回显，可选带一个来自配置的前缀。

    **失败是一等结果而不是异常**：`ToolHandler` 契约要求把可预期的失败折成
    `ok=False` 的 `ToolResult`。逸出的异常会让 Kernel 无法判断副作用发生没有，只能标成
    `SideEffect.UNKNOWN`——一个只读工具因此凭空多出一次「可能改了什么」的记录。
    """

    def __init__(self, prefix: str = "") -> None:
        self._prefix = prefix

    async def execute(self, invocation: ToolInvocation, cancel: CancelSignal) -> ToolResult:
        """本工具在内存里瞬间完成，因此不检查取消——`cancel` 只作签名兼容。"""
        del cancel
        text = invocation.call.arguments.get("text")
        if not isinstance(text, str):
            return ToolResult(
                call_id=invocation.call.call_id,
                ok=False,
                content="text 必须是字符串。",
                truncated=False,
                # 参数非法是在**产生任何副作用之前**判出来的，因此敢标 `NONE`。
                side_effect=SideEffect.NONE,
                error=NucleaError(ErrorCode.INPUT_MALFORMED, "text 必须是字符串。"),
            )
        return ToolResult(
            call_id=invocation.call.call_id,
            ok=True,
            content=f"{self._prefix}{text}",
            truncated=False,
            side_effect=SideEffect.NONE,
        )


def resolve_prefix(ctx: PluginContext) -> str:
    """从 `ctx.config` 取前缀。**只看得见自己那一块**配置（`CFG-002`）。

    形状已经由 manifest 的 `config_schema` 在阶段 A 校验过，因此这里不重复判类型；
    非字符串的值根本走不到这里（那次校验的失败是 `CONFIG_INVALID`，插件不会被加载）。
    """
    value: JsonValue | None = ctx.config.get(CONFIG_PREFIX_KEY)
    return value if isinstance(value, str) else ""


def setup(api: NucleaAPI) -> None:
    """注册入口。manifest 的 `setup` 字段指向它。

    **在同步返回前完成全部注册**：注册先进暂存批次，`setup` 正常返回才一次性并入
    registry；中途抛异常则整批丢弃（`EDG-103`）。因此这里不该派生后台任务去「稍后注册」。
    """
    api.register_tool(
        ToolSpec(
            name=TOOL_NAME,
            description="原样回显传入的 text。",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string", "description": "要回显的文本。"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            read_only=True,
            # `read_only=True` 时风险等级**必须**是 SAFE，否则 `ToolSpec` 构造就失败。
            risk=RiskLevel.SAFE,
        ),
        EchoTool(resolve_prefix(api.ctx)),
    )
