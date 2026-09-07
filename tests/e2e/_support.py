"""录制响应的构造器：把一次对话写成 OpenAI 兼容的线格式。

职责：把「模型这一轮说了什么、调了哪个工具」渲染成 SSE 事件流，供 `conftest.Recorder`
回放。
不负责：断言、装配实例。

**为什么是 SSE 而不是一次性 JSON**：装配根默认 `stream=True`（`OrchestratorDeps.stream`），
因此真实路径走的是流式分片与 `StreamFolder` 的增量拼装。用非流式响应录制会让这套用例绕开
 最容易出错的那一段，而那正是「开箱可用」最先撞上的地方。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence

import httpx

from karyvia.contracts import JsonValue

__all__ = ["say", "sse_response", "use_tool"]


def _sse(events: Sequence[Mapping[str, JsonValue]]) -> str:
    lines = [f"data: {json.dumps(dict(event))}\n\n" for event in events]
    lines.append("data: [DONE]\n\n")
    return "".join(lines)


def sse_response(
    events: Sequence[Mapping[str, JsonValue]],
) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _: httpx.Response(
        200, text=_sse(events), headers={"content-type": "text/event-stream"}
    )


def say(text: str) -> Callable[[httpx.Request], httpx.Response]:
    """模型这一轮只说话。正文切成两片：单片流永远发现不了拼装的问题。"""
    half = max(1, len(text) // 2)
    return sse_response(
        [
            {"choices": [{"index": 0, "delta": {"content": text[:half]}}]},
            {"choices": [{"index": 0, "delta": {"content": text[half:]}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
    )


def use_tool(
    name: str, arguments: Mapping[str, JsonValue], *, call_id: str = "call-1"
) -> Callable[[httpx.Request], httpx.Response]:
    """模型这一轮要调一个工具。

    参数**故意切成两片**发：`arguments` 在线格式里是被切碎的字符串，只能 `+=`，
    流结束才 `json.loads`。一次发完的录制验不到那条路。
    """
    encoded = json.dumps(dict(arguments))
    half = max(1, len(encoded) // 2)
    return sse_response(
        [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": call_id,
                                    "function": {"name": name, "arguments": encoded[:half]},
                                }
                            ]
                        },
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": encoded[half:]}}
                            ]
                        },
                    }
                ]
            },
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )
