"""L1 共享 HTTP 翻译(13.1)的错误分类:账户问题(v0.4.21)必须与普通 4xx / 限流分开。
respx 拦截,零真实网络。"""

from datetime import UTC, datetime

import httpx
import pytest
import respx

from adapters.base import AdapterAccountError, AdapterPermanentError, AdapterTransientError
from adapters.http_support import classify_account_problem, contract_post, retry_after_seconds
from engine.models import RunErrorType

URL = "https://example.test/v1/x"


def _post(status: int, body: str, headers: dict[str, str] | None = None):
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(status, text=body, headers=headers))
        with httpx.Client() as client:
            return contract_post(client, URL, headers={}, payload={}, timeout_s=5.0)


@pytest.mark.parametrize("status, body, kind, words", [
    (400, '{"type":"error","error":{"type":"invalid_request_error",'
          '"message":"Your credit balance is too low to access the API."}}', RunErrorType.ACCOUNT_CREDIT, "额度不足"),
    (429, '{"error":{"message":"You exceeded your current quota, please check your plan and billing details.",'
          '"code":"insufficient_quota"}}', RunErrorType.ACCOUNT_CREDIT, "额度不足"),
    (402, "Payment Required", RunErrorType.ACCOUNT_CREDIT, "额度不足"),
    (401, '{"error":"invalid api key"}', RunErrorType.ACCOUNT_AUTH, "无效"),
    (403, '{"error":{"status":"PERMISSION_DENIED"}}', RunErrorType.ACCOUNT_AUTH, "没有使用权限"),
])
def test_account_problems_are_split_into_auth_and_credit(status, body, kind, words):
    """两种账户问题两种代码:换 key(AUTH)还是充值(CREDIT),消息里带人话解释 + 平台原话。"""
    with pytest.raises(AdapterAccountError) as exc_info:
        _post(status, body)
    exc = exc_info.value
    assert exc.http_status == status and exc.error_type is kind
    assert words in str(exc) and body[:40] in str(exc)
    assert isinstance(exc, AdapterPermanentError)  # 仍属永久类:不重试


def test_account_error_refuses_non_account_type():
    with pytest.raises(ValueError):
        AdapterAccountError("x", error_type=RunErrorType.TRANSIENT)


RATE_LIMIT_BODIES = (  # 普通限流的真实措辞:提了 quota / billing,但不是欠费(审核 2026-10-27 红 3)
    '{"error":{"message":"Quota exceeded for metric requests per minute. Retry later.","status":"RESOURCE_EXHAUSTED"}}',
    '{"error":{"code":429,"message":"You exceeded your current quota, please check your plan and billing details. '
    'For more information on this error, head to: https://example.test/docs/rate-limits.","status":"RESOURCE_EXHAUSTED"}}',
    '{"error":{"message":"Rate limit reached for model-x on requests per min (RPM): Limit 3, Used 3, Requested 1. '
    'Please try again in 20s. You can increase your rate limit by adding a payment method to your account at '
    'https://example.test/account/billing.","type":"requests","code":"rate_limit_exceeded"}}',
)


def test_ordinary_errors_keep_their_classes_and_explain_themselves():
    with pytest.raises(AdapterPermanentError) as bad_request:
        _post(400, '{"error":{"message":"max_tokens must be at least 16"}}')
    assert not isinstance(bad_request.value, AdapterAccountError)
    with pytest.raises(AdapterPermanentError) as bad_field:  # 400 里提到 billing 不等于欠费
        _post(400, '{"error":{"message":"Unknown parameter: billing_address"}}')
    assert not isinstance(bad_field.value, AdapterAccountError)
    for body in RATE_LIMIT_BODIES:  # 一次普通限流绝不能被当成欠费整单停:仍是瞬时错误,交给调度器退避重试
        with pytest.raises(AdapterTransientError) as limited:
            _post(429, body)
        assert not isinstance(limited.value, AdapterAccountError)
        assert "限流" in str(limited.value) and "重试" in str(limited.value)
    with pytest.raises(AdapterTransientError) as outage:
        _post(503, "upstream unavailable")
    assert "平台内部错误" in str(outage.value)


def test_retry_after_is_translated_into_the_exception_not_obeyed_here():
    """v0.4.26 C5:适配器只把 Retry-After 翻译进异常;等不等、等多久归调度器。"""
    with pytest.raises(AdapterTransientError) as limited:
        _post(429, "slow down", {"Retry-After": "7"})
    assert limited.value.retry_after_s == 7.0 and "7 秒后再试" in str(limited.value)
    with pytest.raises(AdapterTransientError) as outage:
        _post(503, "maintenance", {"Retry-After": "120"})
    assert outage.value.retry_after_s == 120.0
    with pytest.raises(AdapterTransientError) as plain:
        _post(429, "slow down")  # 没有头 → None,不猜
    assert plain.value.retry_after_s is None and "秒后再试" not in str(plain.value)


def test_retry_after_seconds_accepts_seconds_or_http_date_only():
    now = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)
    assert retry_after_seconds(httpx.Headers({"Retry-After": "120"})) == 120.0
    assert retry_after_seconds(httpx.Headers({"retry-after": " 3 "})) == 3.0
    assert retry_after_seconds(httpx.Headers({"Retry-After": "Mon, 12 Oct 2026 12:01:30 GMT"}), now=now) == 90.0
    expired = httpx.Headers({"Retry-After": "Mon, 12 Oct 2026 11:00:00 GMT"})
    assert retry_after_seconds(expired, now=now) == 0.0  # 已过期 → 0,不为负
    assert retry_after_seconds(httpx.Headers({})) is None
    assert retry_after_seconds(httpx.Headers({"Retry-After": "soon"})) is None


def test_classify_rule_is_reusable_on_stored_evidence():
    assert classify_account_problem(400, "... Your credit balance is too low ...") is RunErrorType.ACCOUNT_CREDIT
    assert classify_account_problem(401, None) is RunErrorType.ACCOUNT_AUTH
    assert classify_account_problem(400, "unknown field: foo") is None
    assert classify_account_problem(None, "credit balance is too low") is None  # 没有状态码的传输层错误不算
    for body in RATE_LIMIT_BODIES:  # 历史错误行按同一条规则重读:旧规则按词误判成欠费的限流行,现在读出来是限流
        assert classify_account_problem(429, body) is None
