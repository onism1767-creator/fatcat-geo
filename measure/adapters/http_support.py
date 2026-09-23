"""adapters 内共享的 HTTP 辅助:把传输层与 HTTP 状态错误翻译为三类契约异常。

只做翻译,不重试、不退避(13.1:重试统一归引擎调度器)。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import httpx

from adapters.base import AdapterAccountError, AdapterPermanentError, AdapterTransientError
from engine.models import RunErrorType

# 平台把"账户没钱 / 没权限"塞在 400 或 429 里时,靠响应体里的这些原话辨认(v0.4.21)。
# 只收录官方文档写明或真实撞到过的措辞;"quota" 单独一个词不算——某家的限流提示也用它。
_ACCOUNT_PHRASES = (
    "credit balance",            # 2026-09-21 真实撞到:HTTP 400 "Your credit balance is too low"
    "insufficient_quota",        # 官方文档:欠费时 HTTP 429 error.code=insufficient_quota
    "exceeded your current quota",  # 同上的 message 原话
    "billing",
    "payment required",
)


def classify_account_problem(http_status: int | None, body: str | None) -> RunErrorType | None:
    """这次 HTTP 错误是不是账户问题;是的话是哪一种(换 key 还是充值)。同一条规则也用于解读
    已落库的历史错误行(原始层不可改,读出时按现行规则重新翻译)。"""
    if http_status in (401, 403):
        return RunErrorType.ACCOUNT_AUTH
    if http_status == 402:
        return RunErrorType.ACCOUNT_CREDIT
    if http_status in (400, 429) and body:
        lowered = body.lower()
        if any(phrase in lowered for phrase in _ACCOUNT_PHRASES):
            return RunErrorType.ACCOUNT_CREDIT
    return None


_ACCOUNT_MESSAGES = {
    401: "API key 无效或已失效(HTTP 401)",
    403: "API key 没有使用权限(HTTP 403,可能是该型号或功能未对这把 key 开放)",
}


def contract_post(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_s: float,
) -> tuple[Any, int, datetime]:
    """POST 一次;返回 (响应 JSON, latency_ms, 开始时刻 UTC)。

    错误翻译规则(每类消息都带一句人话解释,进 RunError.message 随失败行落库):
    - 超时/连接失败等传输层错误 → AdapterTransientError
    - 账户问题 → AdapterAccountError:401 / 403 = key 无效或没权限(ACCOUNT_AUTH);
      402,或 400 / 429 且响应体是欠费措辞 = 额度不足或欠费(ACCOUNT_CREDIT)
    - 其余 HTTP 429(限流)/ 5xx(平台内部错误)→ AdapterTransientError(带 http_status)
    - 其余 4xx → AdapterPermanentError(非法请求)
    - 2xx 但响应体非 JSON → AdapterTransientError(疑似网关/代理污染)
    """
    started_at = datetime.now(UTC)
    t0 = time.perf_counter()
    try:
        response = client.post(url, headers=headers, json=payload, timeout=timeout_s)
    except httpx.TimeoutException as exc:
        raise AdapterTransientError(f"请求超时(超过 {timeout_s:.0f} 秒没有回复,引擎会重试): {exc}") from exc
    except httpx.HTTPError as exc:
        raise AdapterTransientError(f"传输失败: {type(exc).__name__}: {exc}") from exc
    latency_ms = int((time.perf_counter() - t0) * 1000)

    status = response.status_code
    body = response.text[:2000] if status >= 400 else None  # 错误证据存档,截断防巨响应
    account_kind = classify_account_problem(status, body)
    if account_kind is not None:
        explanation = _ACCOUNT_MESSAGES.get(status, f"账户额度不足或欠费(HTTP {status})")
        raise AdapterAccountError(
            f"{explanation}: {response.text[:200]}", error_type=account_kind,
            http_status=status, response_body=body,
        )
    if status == 429:
        raise AdapterTransientError(
            f"限流(HTTP 429,平台要求放慢请求速度,引擎会退避重试): {response.text[:200]}",
            http_status=status, response_body=body,
        )
    if status >= 500:
        raise AdapterTransientError(
            f"平台内部错误(HTTP {status},引擎会重试): {response.text[:200]}",
            http_status=status, response_body=body,
        )
    if status >= 400:
        raise AdapterPermanentError(
            f"非法请求 HTTP {status}: {response.text[:200]}",
            http_status=status,
            response_body=body,
        )
    try:
        return response.json(), latency_ms, started_at
    except ValueError as exc:
        raise AdapterTransientError("2xx 响应体不是合法 JSON") from exc
