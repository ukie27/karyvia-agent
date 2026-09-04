"""Anthropic 的 HTTP 状态码、错误体与 httpx 异常到 `NucleaError` 的映射（`MOD-003`）。

职责：把「限流、超时、认证失败、过载」几类外部故障折成可分类、`retryable` 标注如实的
`NucleaError`；解析退避提示。
不负责：发起请求、决定要不要重试（那是调用方的策略）、把 refusal 当异常——
**refusal 是 HTTP 200 上的正常响应**，走 `StopReason.CONTENT_FILTER`，见 `decode.py`。

三条判定：

- **`detail` 不放 `error.message`。** 那段自由文本会回显用户的 prompt，也可能带着被原样
  echo 回来的凭据。只放状态码、`error.type`、退避提示与 `request_id`——先例是 `D13` 的
  「命令 handler 异常只留类型名不留消息」。`redact()` 是最后一道防线，不是把明文放进去
  的理由。
- **429 按 `error.type` 分两类，不按状态码。** 撞上限速等一会儿就好，欠费重试一万次也不会
  好。未知 type 默认**可**重试——限速远比欠费常见，且那是对用户更友好的一侧。
- **529 与 5xx 一样可重试。** `overloaded_error` 是 Anthropic 特有的一个状态码，它表达的
  恰恰是「现在很忙、待会儿再来」。

**`retry_after_ms()` 是内建 `model_openai/faults.py` 那份的第二份实现**，逐条相同。
`R4` 禁止插件 import `builtins/`，因此只能各写一份，由插件用例里的对照断言钉住
（`AGENTS.md` 原则 5：优先重复而非过早抽象）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, cast

import httpx

from nucleamind.contracts import ErrorCode, JsonValue, NucleaError

__all__ = [
    "CONTEXT_OVERFLOW_ERROR_TYPES",
    "QUOTA_ERROR_TYPES",
    "RETRYABLE_ERROR_TYPES",
    "error_for_event",
    "error_for_status",
    "error_for_transport",
    "retry_after_ms",
]

#: 表示「额度/计费用尽」的 `error.type` / `error.code`。它们与限速共用 429，
#: 但重试永远不会成功。
QUOTA_ERROR_TYPES: Final[frozenset[str]] = frozenset(
    {
        "insufficient_quota",
        "billing_error",
        "credit_balance_too_low",
        "quota_exceeded",
    }
)

CONTEXT_OVERFLOW_ERROR_TYPES: Final[frozenset[str]] = frozenset(
    {"context_length_exceeded", "context_window_exceeded", "prompt_too_long"}
)

#: SSE `error` 事件里表示「待会儿再来」的 `error.type`。流已经建立之后才收到的错误
#: 只有事件载荷可判，没有状态码。
RETRYABLE_ERROR_TYPES: Final[frozenset[str]] = frozenset(
    {"overloaded_error", "api_error", "timeout_error", "rate_limit_error"}
)

#: 明确可重试的状态码。其余 4xx 是请求本身的问题，重试只是再错一次。
_RETRYABLE_STATUS: Final[frozenset[int]] = frozenset({408, 409})

_AUTH_FAILED: Final = "模型供应商拒绝了凭据。"
_FORBIDDEN: Final = "模型供应商拒绝了本次访问。"
_TOO_LARGE: Final = "请求体超过了模型供应商的上限。"
_RATE_LIMITED: Final = "模型供应商限流。"
_QUOTA_EXHAUSTED: Final = "模型供应商的额度或计费已用尽。"
_UPSTREAM_FAILED: Final = "模型供应商返回了错误响应。"
_STREAM_FAILED: Final = "模型供应商在流式响应中报告了错误。"
_TIMED_OUT: Final = "模型请求超时。"
_TRANSPORT_FAILED: Final = "无法连接模型供应商。"


def _error_fields(body: object) -> dict[str, JsonValue]:
    """取错误体里的 `type` / `code`。

    两种形状都认：Anthropic 标准的 `{"type":"error","error":{"type":…}}` 与中转常见的
    扁平 `{"type":…}`。**`message` 不取**，理由见模块 docstring。
    """
    if not isinstance(body, dict):
        return {}
    # 边界窄化：错误体来自 `json.loads`，在这里定型成契约层的 `JsonValue`。
    document = cast("Mapping[str, JsonValue]", body)
    nested = document.get("error")
    source = cast("Mapping[str, JsonValue]", nested) if isinstance(nested, dict) else document
    fields: dict[str, JsonValue] = {}
    for key in ("type", "code"):
        value = source.get(key)
        if isinstance(value, str) and value:
            fields[key] = value
    request_id = document.get("request_id")
    if isinstance(request_id, str) and request_id:
        # 不敏感，且是 Anthropic 支持工单唯一认的东西。
        fields["request_id"] = request_id
    return fields


def retry_after_ms(headers: Mapping[str, str]) -> int | None:
    """解析退避提示，毫秒。没有可用提示时返回 `None`。

    `retry-after-ms` 优先于 `retry-after`：前者精度更高，且部分网关两个都发。
    HTTP-date 形式的 `retry-after` **不解析**——这些 API 实际只发秒数，解析失败按
    「没有提示」处理即可。
    """
    raw_ms = headers.get("retry-after-ms")
    if raw_ms is not None:
        try:
            return max(0, int(float(raw_ms)))
        except ValueError:
            pass
    raw = headers.get("retry-after")
    if raw is not None:
        try:
            return max(0, int(float(raw) * 1000))
        except ValueError:
            pass
    return None


def _is_quota(fields: Mapping[str, JsonValue]) -> bool:
    return any(
        isinstance(fields.get(key), str) and fields[key] in QUOTA_ERROR_TYPES
        for key in ("type", "code")
    )


def error_for_status(
    status: int,
    *,
    body: object = None,
    headers: Mapping[str, str] | None = None,
) -> NucleaError:
    """非 2xx 响应 → `NucleaError`。调用方只在状态码不是 2xx 时调它。

    401 与 403 刻意分开：前者是「凭据不对或没配」，补救是去补配置，因此复用
    `CONFIG_SECRET_MISSING`——它与 `ctx.secret()` 在凭据缺失时抛的是同一个码，用户看到的
    是同一件事。403 是「凭据没问题但这个账号不许这么用」，补救在供应商那边。
    413 与结构化上下文超限 code/type 统一映射为可恢复语义，供 Kernel 在没有实质输出时
    压缩并重试一次。
    """
    fields = _error_fields(body)
    detail: dict[str, JsonValue] = {"status": status, **fields}
    delay = retry_after_ms(headers or {})
    if delay is not None:
        detail["retry_after_ms"] = delay
    if status == 401:
        return NucleaError(ErrorCode.CONFIG_SECRET_MISSING, _AUTH_FAILED, detail=detail)
    if status == 403:
        return NucleaError(ErrorCode.PERMISSION_DENIED, _FORBIDDEN, detail=detail)
    overflow = status == 413 or any(
        fields.get(key) in CONTEXT_OVERFLOW_ERROR_TYPES for key in ("type", "code")
    )
    if overflow:
        return NucleaError(
            ErrorCode.EXTERNAL_MODEL_CONTEXT_OVERFLOW,
            _TOO_LARGE,
            detail=detail,
        )
    if status == 429:
        exhausted = _is_quota(fields)
        return NucleaError(
            ErrorCode.EXTERNAL_MODEL_PROVIDER,
            _QUOTA_EXHAUSTED if exhausted else _RATE_LIMITED,
            detail=detail,
            retryable=not exhausted,
        )
    return NucleaError(
        ErrorCode.EXTERNAL_MODEL_PROVIDER,
        _UPSTREAM_FAILED,
        detail=detail,
        retryable=status >= 500 or status in _RETRYABLE_STATUS,
    )


def error_for_event(payload: object) -> NucleaError:
    """SSE `error` 事件 → `NucleaError`。

    流一旦建立，HTTP 状态码就已经是 200 了；此后的故障只能由事件载荷判。可重试性因此
    只看 `error.type`，走与状态码路径同一份配额判定。
    """
    fields = _error_fields(payload)
    kind = fields.get("type")
    if any(fields.get(key) in CONTEXT_OVERFLOW_ERROR_TYPES for key in ("type", "code")):
        return NucleaError(
            ErrorCode.EXTERNAL_MODEL_CONTEXT_OVERFLOW,
            _TOO_LARGE,
            detail=dict(fields),
        )
    retryable = isinstance(kind, str) and kind in RETRYABLE_ERROR_TYPES and not _is_quota(fields)
    return NucleaError(
        ErrorCode.EXTERNAL_MODEL_PROVIDER,
        _STREAM_FAILED,
        detail=dict(fields),
        retryable=retryable,
    )


def error_for_transport(exc: Exception) -> NucleaError:
    """httpx 的传输层异常 → `NucleaError`。

    供应商客户端库的原生异常不得从 `complete()` / `stream()` 逸出
    （`protocols.py` 的异常约定），这里是唯一的收口。超时与连接失败都标可重试：
    两者都可能是一次性的网络抖动。
    """
    if isinstance(exc, httpx.TimeoutException):
        return NucleaError(
            ErrorCode.TIMEOUT_MODEL_REQUEST,
            _TIMED_OUT,
            detail={"exception": type(exc).__name__},
            retryable=True,
        )
    return NucleaError(
        ErrorCode.EXTERNAL_MODEL_PROVIDER,
        _TRANSPORT_FAILED,
        detail={"exception": type(exc).__name__},
        retryable=True,
    )
