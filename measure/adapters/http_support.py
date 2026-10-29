"""adapters 内共享的 HTTP 辅助:把传输层与 HTTP 状态错误翻译为三类契约异常。

只做翻译,不重试、不退避(13.1:重试统一归引擎调度器)。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Final
from urllib.parse import urlsplit

import httpx

from adapters.base import AdapterAccountError, AdapterPermanentError, AdapterTransientError
from engine.models import RunErrorType

# 平台把"账户没钱"塞在 400 或 429 里时,靠响应体里的这些标记辨认(v0.4.21)。只认结构化错误码或官方文档写明的
# 整句,不按单词:普通限流的提示也会提 quota / billing(某家限流 429 的原话就是 "You exceeded your current quota,
# please check your plan and billing details",另一家免费档限流提示带 billing 链接),按词会把一次限流当成欠费、
# 整单立停让用户充值(审核 2026-10-27 红 3)。"payment required" 由 HTTP 402 状态码本身覆盖,不再按词认。
# 花费上限与欠费同类:重试不会恢复,只能充值、调高上限或等到下个计费周期(第二轮审核 25;2026-10-29 核对官方文档)。
_ACCOUNT_CREDIT_MARKERS = (
    # OpenAI 错误码页:欠费 / 花费上限 / 用量上限都是 HTTP 429,靠 error.code 与限流区分;error.type 仍可能是
    # insufficient_quota(原来注释写成 error.code,已过时)
    "insufficient_quota",
    "credit_balance_exhausted",            # 预付额度用完
    "organization_spend_limit_exceeded",   # 组织到了自己设的花费上限
    "project_spend_limit_exceeded",        # 项目到了自己设的花费上限
    "organization_usage_limit_exceeded",   # 组织到了平台分配的用量上限
    # Anthropic 限额页:用量档的月度封顶是 429 rate_limit_error、没有 retry-after,只能靠 details.error_code 认;
    # 用户自己设的花费上限是 400 invalid_request_error,原话以下面两句开头(组织级 / 工作区级)
    "enforced_spend_limit_reached",
    "you have reached your specified api usage limits",
    "you have reached your specified workspace api usage limits",
    # 2026-09-21 真实撞到:HTTP 400 "Your credit balance is too low to access the API"
    "credit balance is too low",
)

# HTTP 错误页只留头尾各这么多字:代理 / 网关可能吐一整张网页,平台的错误说明常在开头,结尾常是请求号(第二轮审核 26)
_ERROR_BODY_KEEP: Final = 8000


def domain_of(url: str) -> str:
    """URL 的主机名(平台给的 URL 原样翻译,不做归属判定)。网址坏了取不出 → 空串:原来 urlsplit 抛错被结构防线接住,
    一条坏引用让整条好回答记成永久失败(第二轮审核 B4);网址本身照原样留在引用里。"""
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def full_body(data: Any) -> str:
    """HTTP 2xx 却报失败(status=failed、工具错误块、结构不对……)时,把整份响应带进异常,随失败行进 raw_payload
    存档——失败的证据也不蒸发(13.1;审核 2026-10-27 黄 21)。和成功回答一样全量存(硬边界 6):原来只存前 2000 字,
    错误说明在后面的(工具错误块常在长正文之后)就丢了(第二轮审核 26)。"""
    return json.dumps(data, ensure_ascii=False, default=str)


def clipped(text: str) -> str:
    """HTTP 错误页 / 非 JSON 回包:留头尾各 _ERROR_BODY_KEEP 字,中间注明省了多少。"""
    if len(text) <= 2 * _ERROR_BODY_KEEP:
        return text
    dropped = len(text) - 2 * _ERROR_BODY_KEEP
    return f"{text[:_ERROR_BODY_KEEP]}\n…[中间省略 {dropped} 字]…\n{text[-_ERROR_BODY_KEEP:]}"


def spent_on(compute: Callable[[], float]) -> float:
    """平台回了话却报失败时,这次已经花掉的钱:调用方按该家价目从响应里折算。响应里的用量字段本身也怪 → 0
    (看不出花了多少就不臆造;失败行照样落,原始响应在 raw_payload 里,以后能重算)。"""
    try:
        return float(compute())
    except (KeyError, TypeError, AttributeError, ValueError):
        return 0.0


def classify_account_problem(http_status: int | None, body: str | None) -> RunErrorType | None:
    """这次 HTTP 错误是不是账户问题;是的话是哪一种(换 key 还是充值)。同一条规则也用于解读
    已落库的历史错误行(原始层不可改,读出时按现行规则重新翻译)。"""
    if http_status in (401, 403):
        return RunErrorType.ACCOUNT_AUTH
    if http_status == 402:
        return RunErrorType.ACCOUNT_CREDIT
    if http_status in (400, 429) and body:
        lowered = body.lower()
        if any(marker in lowered for marker in _ACCOUNT_CREDIT_MARKERS):
            return RunErrorType.ACCOUNT_CREDIT
    return None


_ACCOUNT_MESSAGES = {
    401: "API key 无效或已失效(HTTP 401)",
    403: "API key 没有使用权限(HTTP 403,可能是该型号或功能未对这把 key 开放)",
}


def retry_after_seconds(headers: httpx.Headers, *, now: datetime | None = None) -> float | None:
    """Retry-After 头 → 秒数(RFC 9110:整数秒,或 HTTP 日期)。没有这个头、或看不懂 → None,不猜。"""
    raw = headers.get("retry-after")
    if raw is None:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return float(raw)
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


def _retry_hint(retry_after_s: float | None) -> str:
    return f",平台要求 {retry_after_s:.0f} 秒后再试" if retry_after_s is not None else ""


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
      402,或 400 / 429 且响应体带欠费 / 花费上限的错误码或官方原话 = 额度不足、欠费或到了上限(ACCOUNT_CREDIT)
    - 其余 HTTP 429(限流)/ 5xx(平台内部错误)→ AdapterTransientError(带 http_status;
      429 / 503 的 Retry-After 头翻译成 retry_after_s,v0.4.26 C5——只翻译,不在这里等)
    - 其余 4xx → AdapterPermanentError(非法请求)
    - 2xx 但响应体非 JSON → AdapterTransientError(疑似网关/代理污染;回包头尾带进异常存档)
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
    body = clipped(response.text) if status >= 400 else None  # 错误证据存档,留头尾防巨响应
    account_kind = classify_account_problem(status, body)
    if account_kind is not None:
        explanation = _ACCOUNT_MESSAGES.get(status, f"账户额度不足、欠费或到了花费上限(HTTP {status})")
        raise AdapterAccountError(
            f"{explanation}: {response.text[:200]}", error_type=account_kind,
            http_status=status, response_body=body,
        )
    retry_after = retry_after_seconds(response.headers) if status in (429, 503) else None
    if status == 429:
        raise AdapterTransientError(
            f"限流(HTTP 429,平台要求放慢请求速度{_retry_hint(retry_after)},引擎会退避重试): {response.text[:200]}",
            http_status=status, response_body=body, retry_after_s=retry_after,
        )
    if status >= 500:
        raise AdapterTransientError(
            f"平台内部错误(HTTP {status}{_retry_hint(retry_after)},引擎会重试): {response.text[:200]}",
            http_status=status, response_body=body, retry_after_s=retry_after,
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
        raise AdapterTransientError(
            f"HTTP {status} 响应体不是合法 JSON(疑似网关或代理插了一张网页,引擎会重试)",
            http_status=status, response_body=clipped(response.text),
        ) from exc
