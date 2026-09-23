"""L1 共享 HTTP 翻译(13.1)的错误分类:账户问题(v0.4.21)必须与普通 4xx / 限流分开。
respx 拦截,零真实网络。"""

import httpx
import pytest
import respx

from adapters.base import AdapterAccountError, AdapterPermanentError, AdapterTransientError
from adapters.http_support import contract_post, is_account_problem

URL = "https://example.test/v1/x"


def _post(status: int, body: str):
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(status, text=body))
        with httpx.Client() as client:
            return contract_post(client, URL, headers={}, payload={}, timeout_s=5.0)


@pytest.mark.parametrize("status, body", [
    (400, '{"type":"error","error":{"type":"invalid_request_error",'
          '"message":"Your credit balance is too low to access the API."}}'),
    (429, '{"error":{"message":"You exceeded your current quota, please check your plan and billing details.",'
          '"code":"insufficient_quota"}}'),
    (401, '{"error":"invalid api key"}'),
    (402, "Payment Required"),
    (403, '{"error":{"status":"PERMISSION_DENIED"}}'),
])
def test_account_problems_are_translated_to_account_error(status, body):
    with pytest.raises(AdapterAccountError) as exc_info:
        _post(status, body)
    assert exc_info.value.http_status == status
    assert isinstance(exc_info.value, AdapterPermanentError)  # 仍属永久类:不重试


def test_ordinary_errors_keep_their_old_classes():
    with pytest.raises(AdapterPermanentError) as bad_request:
        _post(400, '{"error":{"message":"max_tokens must be at least 16"}}')
    assert not isinstance(bad_request.value, AdapterAccountError)
    with pytest.raises(AdapterTransientError):  # 限流提示里的 quota 一词不算欠费
        _post(429, '{"error":{"message":"Quota exceeded for metric requests per minute. Retry later.",'
                   '"status":"RESOURCE_EXHAUSTED"}}')
    with pytest.raises(AdapterTransientError):
        _post(503, "upstream unavailable")


def test_is_account_problem_rule_is_reusable_on_stored_evidence():
    assert is_account_problem(400, "... Your credit balance is too low ...")
    assert is_account_problem(401, None)
    assert not is_account_problem(400, "unknown field: foo")
    assert not is_account_problem(None, "credit balance")  # 没有状态码的传输层错误不算
