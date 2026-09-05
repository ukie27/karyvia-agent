"""把只读视图渲染成给人看的文本。纯函数，一次 IO 都没有。

职责：`/help`、`/config`、`/session`、`/plugins`、`/capabilities` 的输出格式，以及统一的
长度截断。
不负责：取数据（`ctx.instance`）、决定谁被调用（dispatcher）、脱敏规则本身
（`contracts.errors.redact`）。

**为什么单独成模块**：格式的每一条都能在这里逐字符钉住，不需要构造 `CommandInvocation`、
不需要事件循环。混进 handler 里会让「少了一行表头」这种错误只能从一次异步调用的返回值里
看出来——与 `model_openai` 把 `wire.py` 拆出来是同一条分界线（碰不碰 IO）。

**截断标记算在上限内**：先按最坏情况算能留多少字符，再用真实的 `shown` 渲染标记，
因此返回值长度恒 ≤ 上限，且标记里报的数字与实际截出来的长度是同一个数（`D20` 的做法）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from karyvia.contracts import (
    CommandSpec,
    JsonValue,
    SessionSnapshot,
    TurnId,
    redact,
    scrub,
)

__all__ = [
    "render_capabilities",
    "render_config",
    "render_help",
    "render_plugins",
    "render_session",
    "render_turns",
    "truncate",
]


def truncate(text: str, limit: int) -> str:
    """按字符截断并附上可读标记。`limit` 之内原样返回。"""
    if len(text) <= limit:
        return text
    marker = "\n…（输出已截断，共 {total} 字符，显示前 {shown} 字符）"
    # 最坏情况下标记本身有多长——用 total 与 limit 的位数上界估，宁可少显示几个字符。
    reserve = len(marker.format(total=len(text), shown=limit))
    shown = max(0, limit - reserve)
    return text[:shown] + marker.format(total=len(text), shown=shown)


def _usage(spec: CommandSpec, prefix: str) -> str:
    """一行用法。与 `kernel/routing/dispatcher.py::_usage` 同型——那份用于参数不符时的
    报错，这份用于 `/help`，两处各写一份而不是跨层 import（`R4` 挡着 `builtins → kernel`）。
    """
    parts = [
        f"<{param.name}>" if param.required else f"[{param.name}]" for param in spec.parameters
    ]
    tail = "..." if spec.parameters and spec.parameters[-1].repeated else ""
    return " ".join([f"{prefix}{spec.name}", *parts]).strip() + tail


def render_help(specs: Sequence[CommandSpec], prefix: str) -> str:
    """全部命令的用法与说明；仅操作者命令标记为 ``[管理员]``。"""
    if not specs:
        return "当前没有可用的命令。"
    lines = ["可用命令："]
    for spec in specs:
        flags = " [管理员]" if spec.operator_only else ""
        lines.append(f"  {_usage(spec, prefix)}{flags}")
        lines.append(f"      {spec.description}")
        if spec.aliases:
            lines.append(f"      别名：{', '.join(prefix + a for a in sorted(spec.aliases))}")
        for param in spec.parameters:
            lines.append(f"      {param.name}：{param.description}")
    return "\n".join(lines)


def _as_json(payload: object) -> str:
    """稳定的 JSON 渲染：按键排序、不转义非 ASCII（中文说明要能读）。"""
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def render_config(document: Mapping[str, JsonValue]) -> str:
    """完整配置文档。

    **脱敏在这里是纵深防御而不是唯一防线**：`D11` 定死配置树自始至终持有 `${VAR}` 字面量，
    解析出的明文只在 `SecretMap` 里，因此这份文档「没有别的东西可泄漏」。仍然过一道
    `redact()`，因为「结构上不该有」与「确实没有」之间隔着每一个未来会改这条路径的人。
    """
    if not document:
        return "当前没有生效的配置项。"
    redacted, secrets = redact(dict(document))
    # 键名脱敏挡不住「密钥被顺手拼进了某个自由文本字段」，因此渲染完再擦一遍密文
    # ——这正是 `redact()` 交回那个集合的用途。
    return scrub(_as_json(redacted), secrets)


def _provider_of(ref: Mapping[str, JsonValue]) -> str:
    """一条能力引用里的提供方标签。`ResolutionReport.to_json()` 已经渲染成
    `"builtin"` / `"plugin:<id>"`，这里只负责在拿不到时不炸。"""
    provider = ref.get("provider")
    return provider if isinstance(provider, str) else "未知"


def _rows(value: JsonValue | None) -> tuple[JsonValue, ...]:
    """把报告里的一段取成序列。不是序列（含字符串）就当作空——渲染不该因为上游多了
    一个字段形状就抛异常，那会让一次只读查询变成一次失败。"""
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return ()


def render_capabilities(report: Mapping[str, JsonValue]) -> str:
    """覆盖解析报告：生效 / 被覆盖 / 已禁用 / 冲突，每项标明提供方（`PLG-006`）。

    **四段都印，哪怕是空的**：`failures` 为空是一条有价值的结论（这份配置能启动），
    只在非空时才提它会让用户不确定到底查没查。
    """
    active = _rows(report.get("active"))
    lines = [f"生效能力（{len(active)}）：" if active else "生效能力：无"]
    for item in active:
        if isinstance(item, Mapping):
            kind = item.get("kind")
            name = item.get("name")
            lines.append(f"  {kind}:{name}  ← {_provider_of(item)}")
    for key, label in (("shadowed", "被覆盖"), ("disabled", "已禁用"), ("failures", "冲突")):
        lines.append(f"{label}：{len(_rows(report.get(key)))}")
    lines.append("")
    lines.append(_as_json(report))
    return "\n".join(lines)


def render_plugins(statuses: Sequence[Mapping[str, JsonValue]]) -> str:
    """每个插件的状态、版本与已注册能力。

    **没有插件时说「没有插件」而不是印一个空表**：`EDG-101` 要求零插件是可用形态，
    用户看到的应当是一句确认，而不是一个让人怀疑查询失败了的空白。
    """
    if not statuses:
        return "当前没有加载任何外部插件（内建能力见 /capabilities）。"
    lines = [f"已加载插件（{len(statuses)}）："]
    for status in statuses:
        lines.append(
            f"  {status.get('plugin_id')}  {status.get('version')}  [{status.get('state')}]"
        )
        capabilities = _rows(status.get("capabilities"))
        if capabilities:
            lines.append(f"      能力：{', '.join(str(c) for c in capabilities)}")
        failure = status.get("failure")
        if failure is not None:
            phase = status.get("failed_phase")
            lines.append(f"      失败{f'（{phase}）' if phase else ''}：{_as_json(failure)}")
    return "\n".join(lines)


def render_session(snapshot: SessionSnapshot) -> str:
    """当前会话的条数、压缩水位与时间戳。

    **不印消息内容**：`/session` 回答的是「这个会话现在多大、压到哪了」，而历史内容用户
    刚刚才在屏幕上看过。把几百条消息重新吐一遍只会把终端冲掉。
    """
    key = snapshot.session_key
    lines = [
        f"会话：{key.storage_id()}",
        f"  渠道：{key.channel_id}    会话：{key.conversation_id}    范围：{key.scope}",
        f"  记录数：{len(snapshot.messages)}（未压缩 {len(snapshot.live_messages)}，"
        f"压缩水位 {snapshot.compacted_through}）",
        f"  存储格式版本：{snapshot.schema_version}",
    ]
    if snapshot.created_at is not None:
        lines.append(f"  创建于：{snapshot.created_at.isoformat()}")
    if snapshot.updated_at is not None:
        lines.append(f"  更新于：{snapshot.updated_at.isoformat()}")
    return "\n".join(lines)


def render_turns(live: Sequence[TurnId], cancelled: TurnId | None = None) -> str:
    """`/cancel` 的输出：取消结果，或在缺参数时列出可取消的 turn。"""
    if cancelled is not None:
        return f"已请求取消 turn {cancelled}。"
    if not live:
        return "当前没有正在执行的 turn。"
    listed = "\n".join(f"  {turn_id}" for turn_id in live)
    return f"正在执行的 turn（{len(live)}）：\n{listed}"
