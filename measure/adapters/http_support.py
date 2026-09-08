"""adapters 内共享的 HTTP 辅助:把传输层与 HTTP 状态错误翻译为三类契约异常。

只做翻译,不重试、不退避(13.1:重试统一归引擎调度器)。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import httpx

from adapters.base import AdapterPermanentError, AdapterTransientError


def contract_post(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout_s: float,
) -> tuple[Any, int, datetime]:
    """POST 一次;返回 (响应 JSON, latency_ms, 开始时刻 UTC)。

    错误翻译规则:
    - 超时/连接失败等传输层错误 → AdapterTransientError
    - HTTP 429 / 5xx → AdapterTransientError(带 http_status)
    - HTTP 401 / 403 → AdapterPermanentError(认证/授权)
    - 其余 4xx → AdapterPermanentError(非法请求)
    - 2xx 但响应体非 JSON → AdapterTransientError(疑似网关/代理污染)
    """
    started_at = datetime.now(UTC)
    t0 = time.perf_counter()
    try:
        response = client.post(url, headers=headers, json=payload, timeout=timeout_s)
    except httpx.TimeoutException as exc:
        raise AdapterTransientError(f"请求超时: {exc}") from exc
    except httpx.HTTPError as exc:
        raise AdapterTransientError(f"传输失败: {type(exc).__name__}: {exc}") from exc
    latency_ms = int((time.perf_counter() - t0) * 1000)

    status = response.status_code
    body = response.text[:2000] if status >= 400 else None  # 错误证据存档,截断防巨响应
    if status == 429 or status >= 500:
        raise AdapterTransientError(f"HTTP {status}", http_status=status, response_body=body)
    if status in (401, 403):
        raise AdapterPermanentError(
            f"认证/授权失败 HTTP {status}", http_status=status, response_body=body
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
