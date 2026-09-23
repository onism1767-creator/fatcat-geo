"""L1 共享 HTTP 翻译(13.1)的错误分类:账户问题(v0.4.21)必须与普通 4xx / 限流分开。
respx 拦截,零真实网络。"""

import httpx
import pytest
import respx

from adapters.base import AdapterAccountError, AdapterPermanentError, AdapterTransientError
from adapters.http_support import classify_account_problem, contract_post
from engine.models import RunErrorType

URL = "https://example.test/v1/x"


def _post(status: int, body: str):
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(status, text=body))
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


def test_ordinary_errors_keep_their_classes_and_explain_themselves():
    with pytest.raises(AdapterPermanentError) as bad_request:
        _post(400, '{"error":{"message":"max_tokens must be at least 16"}}')
    assert not isinstance(bad_request.value, AdapterAccountError)
    with pytest.raises(AdapterTransientError) as limited:  # 限流提示里的 quota 一词不算欠费
        _post(429, '{"error":{"message":"Quota exceeded for metric requests per minute. Retry later.",'
                   '"status":"RESOURCE_EXHAUSTED"}}')
    assert "限流" in str(limited.value) and "重试" in str(limited.value)
    with pytest.raises(AdapterTransientError) as outage:
        _post(503, "upstream unavailable")
    assert "平台内部错误" in str(outage.value)


def test_classify_rule_is_reusable_on_stored_evidence():
    assert classify_account_problem(400, "... Your credit balance is too low ...") is RunErrorType.ACCOUNT_CREDIT
    assert classify_account_problem(401, None) is RunErrorType.ACCOUNT_AUTH
    assert classify_account_problem(400, "unknown field: foo") is None
    assert classify_account_problem(None, "credit balance") is None  # 没有状态码的传输层错误不算
