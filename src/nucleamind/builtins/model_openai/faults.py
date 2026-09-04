"""HTTP 状态码与 httpx 异常到 `NucleaError` 的映射（`MOD-003`）。

职责：把「限流、超时、认证失败、上下文超限」等外部故障折成可分类、`retryable` 标注如实的
`NucleaError`；解析退避提示。
不负责：发起请求、决定要不要重试（那是调用方的策略）、把内容过滤当异常——
**内容过滤是 HTTP 200 上的正常响应**，走 `StopReason.CONTENT_FILTER`，见 `wire.py`。

两条值得单独记下来的判定：

- **429 按语义分两类，不按状态码。** 撞上限速等一会儿就好，欠费或超额重试一万次也不会
  好，而两者都是 429。靠供应商的 `error.code` 区分，未知 code 默认**可**重试——那是对
  用户更友好的一侧，且限速远比欠费常见。
- **`detail` 不放 `error.message`。** 那段自由文本会回显用户的 prompt，也可能带着被原样
  echo 回来的凭据。只放状态码与 `error.type` / `error.code`——先例是 `D13` 的「命令
  handler 异常只留类型名不留消息」。`redact()` 是最后一道防线，不是把明文放进去的理由。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, cast

import httpx

from nucleamind.contracts import ErrorCode, JsonValue, NucleaError

__all__ = [
    "CONTEXT_OVERFLOW_ERROR_CODES",
    "QUOTA_ERROR_CODES",
    "error_for_status",
    "error_for_transport",
    "retry_after_ms",
]

#: 表示「额度/计费用尽」的供应商 `error.code`。它们与限速共用 429，但重试永远不会成功。
QUOTA_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "insufficient_quota",
        "billing_hard_limit_reached",
        "billing_not_active",
        "credit_balance_too_low",
        "quota_exceeded",
    }
)

# OpenAI 与兼容网关常用的结构化上下文超限 code/type。只看结构字段，不扫描可能回显
# prompt 的自由文本 message。
CONTEXT_OVERFLOW_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "context_length_exceeded",
        "context_window_exceeded",
        "prompt_too_long",
        "request_too_large",
    }
)

#: 明确可重试的状态码。其余 4xx 是请求本身的问题，重试只是再错一次。
_RETRYABLE_STATUS: Final[frozenset[int]] = frozenset({408, 409})

_AUTH_FAILED: Final = "模型供应商拒绝了凭据。"
_FORBIDDEN: Final = "模型供应商拒绝了本次访问。"
_RATE_LIMITED: Final = "模型供应商限流。"
_QUOTA_EXHAUSTED: Final = "模型供应商的额度或计费已用尽。"
_UPSTREAM_FAILED: Final = "模型供应商返回了错误响应。"
_CONTEXT_OVERFLOW: Final = "模型请求超过供应商的上下文窗口。"
_TIMED_OUT: Final = "模型请求超时。"
_TRANSPORT_FAILED: Final = "无法连接模型供应商。"


def _error_fields(body: object) -> dict[str, JsonValue]:
    """取供应商错误体里的 `type` / `code`。

    两种形状都认：OpenAI 标准的 `{"error": {...}}` 与兼容网关常见的扁平 `{...}`。
    **`message` 不取**，理由见模块 docstring。
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
    return fields


def retry_after_ms(headers: Mapping[str, str]) -> int | None:
    """解析退避提示，毫秒。没有可用提示时返回 `None`。

    `retry-after-ms` 优先于 `retry-after`：前者精度更高，且部分网关两个都发。
    HTTP-date 形式的 `retry-after` **不解析**——这些 API 实际只发秒数，为一个没人发的
    形式引入 `email.utils` 不划算，解析失败按「没有提示」处理即可。
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


def _rate_limit_error(fields: Mapping[str, JsonValue], detail: dict[str, JsonValue]) -> NucleaError:
    code = fields.get("code")
    exhausted = isinstance(code, str) and code in QUOTA_ERROR_CODES
    return NucleaError(
        ErrorCode.EXTERNAL_MODEL_PROVIDER,
        _QUOTA_EXHAUSTED if exhausted else _RATE_LIMITED,
        detail=detail,
        retryable=not exhausted,
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
    if status == 429:
        return _rate_limit_error(fields, detail)
    if status == 413 or any(
        fields.get(key) in CONTEXT_OVERFLOW_ERROR_CODES for key in ("code", "type")
    ):
        return NucleaError(
            ErrorCode.EXTERNAL_MODEL_CONTEXT_OVERFLOW,
            _CONTEXT_OVERFLOW,
            detail=detail,
        )
    return NucleaError(
        ErrorCode.EXTERNAL_MODEL_PROVIDER,
        _UPSTREAM_FAILED,
        detail=detail,
        retryable=status >= 500 or status in _RETRYABLE_STATUS,
    )


def error_for_transport(exc: Exception) -> NucleaError:
    """httpx 的传输层异常 → `NucleaError`。

    供应商 SDK / 客户端库的原生异常不得从 `complete()` / `stream()` 逸出
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
