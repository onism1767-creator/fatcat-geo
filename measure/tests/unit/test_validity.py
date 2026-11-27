"""有效回答口径与对外错误码(产品文档 15.2;架构书附录 v0.4.26 C6;S3)。

一处定义、全项目共用:判定器拿它决定「判不判」,记账拿它决定「进不进分母」,
日志 / 导出拿它决定「写哪个码」。零网络零数据库。
"""

from datetime import UTC, datetime

import pytest

from engine.models import Answer, Outcome, RunError, RunErrorType, SearchMode, SurfaceRun
from engine.validity import PublicErrorCode, is_empty_answer, is_valid_answer, public_error_code

PERMANENT = Outcome.PERMANENT_FAILURE


def make_run(*, outcome=Outcome.SUCCESS, text: str | None = "回答正文", error: RunError | None = None) -> SurfaceRun:
    return SurfaceRun(
        run_id="r", prompt_id="p", run_index=1,
        provider="test_provider", surface="test_surface", model="m1",
        search_mode=SearchMode.DEFAULT, locale="zh-CN",
        outcome=outcome, error=error,
        answer=Answer(answer_text=text, citations_available=True) if text is not None else None,
        raw_payload={}, timestamp=datetime(2026, 10, 12, tzinfo=UTC), latency_ms=1, cost_usd=0.0,
    )


def failed(error_type: RunErrorType, *, http_status: int | None, outcome=Outcome.TRANSIENT_FAILURE) -> SurfaceRun:
    return make_run(outcome=outcome, text=None,
                    error=RunError(error_type=error_type, message="x", http_status=http_status))


def refused(text: str | None) -> SurfaceRun:
    return make_run(outcome=Outcome.REFUSED, text=text,
                    error=RunError(error_type=RunErrorType.REFUSAL, message="filtered"))


def test_valid_means_success_with_non_blank_text():
    assert is_valid_answer(make_run(text="回答正文")) and not is_empty_answer(make_run(text="回答正文"))
    for blank in ("", "   ", "\n\t "):
        assert is_empty_answer(make_run(text=blank)) and not is_valid_answer(make_run(text=blank))
    assert is_empty_answer(make_run(text=None)) and not is_valid_answer(make_run(text=None))
    assert not is_valid_answer(refused(None)) and not is_empty_answer(refused(None))  # 拒答是拒答,不是空回答


@pytest.mark.parametrize("run, code", [
    (make_run(text="回答正文"), None),
    (make_run(text="  "), PublicErrorCode.AI_RESPONSE_INVALID),
    (make_run(text=None), PublicErrorCode.AI_RESPONSE_INVALID),
    (refused("部分正文"), PublicErrorCode.AI_RESPONSE_INVALID),
    (failed(RunErrorType.TRANSIENT, http_status=429), PublicErrorCode.AI_PROVIDER_RATE_LIMITED),
    (failed(RunErrorType.TRANSIENT, http_status=503), PublicErrorCode.AI_PROVIDER_ERROR),
    (failed(RunErrorType.TRANSIENT, http_status=None), PublicErrorCode.AI_PROVIDER_ERROR),  # 超时
    (failed(RunErrorType.PERMANENT, http_status=400, outcome=PERMANENT), PublicErrorCode.AI_PROVIDER_ERROR),
    (failed(RunErrorType.ACCOUNT_AUTH, http_status=401, outcome=PERMANENT), PublicErrorCode.ACCOUNT_AUTH),
    (failed(RunErrorType.ACCOUNT_CREDIT, http_status=429, outcome=PERMANENT), PublicErrorCode.ACCOUNT_CREDIT),
])
def test_public_error_code_mapping(run, code):
    """产品文档三类 + 我方账户两类;429 欠费是账户问题,不是限流(账户类先于状态码判)。"""
    assert public_error_code(run) is code


def test_public_codes_include_the_product_doc_names():
    names = {c.value for c in PublicErrorCode}
    assert {"AI_PROVIDER_ERROR", "AI_PROVIDER_RATE_LIMITED", "AI_RESPONSE_INVALID"} <= names
