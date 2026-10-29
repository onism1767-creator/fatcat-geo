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

# 401 / 403 不一定是 key 的事(用户 2026-10-29 定:分不清是致命的——原来一律叫人换 key)。同样只认官方写明的错误码或原话
# (2026-10-29 核对各家官方文档;Anthropic、Perplexity 的地区 / 欠费没查到官方写法,不猜,落到下面的状态码兜底):
_ACCOUNT_REGION_MARKERS = (  # 不对请求所在的国家或地区开放:和 key、账户无关
    "unsupported_country_region_territory",          # OpenAI 403 的 error.code
    "country, region, or territory not supported",   # OpenAI 错误码页原话
)
_ACCOUNT_KEY_MARKERS = (  # 确实是 key 的事:换 key 有用
    "your api key was reported as leaked",           # Gemini 403(故障排除页原话)
)
_ACCOUNT_RESTRICTED_MARKERS = (  # 账户或项目被限制:没开通付费、免费额度不对项目开放、有欠款、被暂停
    "your project has been denied access",           # Gemini 403(故障排除页原话)
    "failed_precondition",                           # Gemini 400(错误码页:前置条件不满足,比如没开通计费)
)
_ACCOUNT_QUOTA_MARKERS = (  # 这个周期的额度用完:重试没用,等重置或申请提额
    "quota_exceeded",                                # Gemini 429(错误码页:超过每日额度;按分钟的限流是别的码)
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
    """这次 HTTP 错误是不是账户问题;是的话是哪一种——各自要用户做的事不同(换 key / 充值 / 地区不支持 / 账户受限 /
    额度到期)。先认官方写明的错误码或原话,认不出才按状态码兜底(401 / 403 → AUTH,402 → CREDIT)。
    同一条规则也用于解读已落库的历史错误行(原始层不可改,读出时按现行规则重新翻译)。"""
    lowered = (body or "").lower()

    def says(markers: tuple[str, ...]) -> bool:
        return any(marker in lowered for marker in markers)

    if http_status in (400, 403) and says(_ACCOUNT_REGION_MARKERS):
        return RunErrorType.ACCOUNT_REGION
    if http_status == 403 and says(_ACCOUNT_KEY_MARKERS):
        return RunErrorType.ACCOUNT_AUTH
    if http_status in (400, 403) and says(_ACCOUNT_RESTRICTED_MARKERS):
        return RunErrorType.ACCOUNT_RESTRICTED
    if http_status == 429 and says(_ACCOUNT_QUOTA_MARKERS):
        return RunErrorType.ACCOUNT_QUOTA
    if http_status in (400, 429) and says(_ACCOUNT_CREDIT_MARKERS):
        return RunErrorType.ACCOUNT_CREDIT
    if http_status in (401, 403):
        return RunErrorType.ACCOUNT_AUTH
    if http_status == 402:
        return RunErrorType.ACCOUNT_CREDIT
    return None


def _account_explanation(kind: RunErrorType, status: int) -> str:
    """账户问题的一句人话解释(进 RunError.message,任务页的「平台原话」前面那句)。"""
    if kind is RunErrorType.ACCOUNT_AUTH:
        if status == 401:
            return "API key 无效或已失效(HTTP 401)"
        return "API key 没有使用权限,或者账户被平台限制(HTTP 403,平台没写明是哪种,先看平台原话)"
    return {
        RunErrorType.ACCOUNT_CREDIT: f"账户额度不足、欠费或到了花费上限(HTTP {status})",
        RunErrorType.ACCOUNT_REGION: f"这家不对当前网络所在的国家或地区开放接口,换 key 没用(HTTP {status})",
        RunErrorType.ACCOUNT_RESTRICTED: f"账户或项目被平台限制了(没开通付费、有欠款或被暂停),换 key 没用"
                                         f"(HTTP {status})",
        RunErrorType.ACCOUNT_QUOTA: f"这个周期的调用额度用完了,重试没用,要等额度重置(HTTP {status})",
    }[kind]


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
    - 账户问题 → AdapterAccountError,分五种(classify_account_problem):先按官方写明的错误码或原话认地区不支持(REGION)、
      key 泄露(AUTH)、账户受限(RESTRICTED)、周期额度用完(QUOTA)、欠费 / 花费上限(CREDIT);认不出的 401 / 403 → AUTH、
      402 → CREDIT
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
        explanation = _account_explanation(account_kind, status)
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
