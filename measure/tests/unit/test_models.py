"""数据模型行为测试:不可变性、outcome-error 一致性、JSON 往返。"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from engine.models import (
    NOT_OBSERVABLE,
    Answer,
    Citation,
    Outcome,
    RetrievedSource,
    RunError,
    RunErrorType,
    SearchMode,
    SearchTrace,
    SurfaceRun,
)


def make_run(**overrides) -> SurfaceRun:
    fields = dict(
        run_id="run-1",
        information_need_id="need-1",
        prompt_variant_id="variant-1",
        provider="test_provider",
        surface="test_surface",
        model="test-model-1",
        search_mode=SearchMode.DEFAULT,
        locale="zh-CN",
        outcome=Outcome.SUCCESS,
        answer=Answer(
            answer_text="推荐甲乙丙三家。",
            citations_available=True,
            citations=[Citation(url="https://example.com/a", domain="example.com", position=1)],
        ),
        search_trace=SearchTrace(
            search_triggered=True,
            retrieved_sources=[RetrievedSource(url="https://example.com/a", domain="example.com", rank=1)],
        ),
        raw_payload={"answer": "推荐甲乙丙三家。"},
        timestamp=datetime(2026, 8, 17, tzinfo=UTC),
        latency_ms=1200,
        cost_usd=0.004,
    )
    fields.update(overrides)
    return SurfaceRun(**fields)


def test_surface_run_is_immutable():
    run = make_run()
    with pytest.raises(ValidationError):
        run.outcome = Outcome.REFUSED


def test_success_with_error_rejected():
    with pytest.raises(ValidationError, match="SUCCESS"):
        make_run(error=RunError(error_type=RunErrorType.TRANSIENT, message="超时"))


def test_failure_without_error_rejected():
    with pytest.raises(ValidationError):
        make_run(outcome=Outcome.TRANSIENT_FAILURE, answer=None)


def test_failure_run_records_attempts():
    run = make_run(
        outcome=Outcome.TRANSIENT_FAILURE,
        answer=None,
        search_trace=SearchTrace(),
        error=RunError(error_type=RunErrorType.TRANSIENT, message="限流", http_status=429, attempts=3),
    )
    assert run.error.attempts == 3
    assert run.search_trace.retrieved_sources == NOT_OBSERVABLE


def test_empty_citations_is_valid_success():
    """引用集为空的正常返回也是 SUCCESS(真实负观测,6.3)。"""
    run = make_run(answer=Answer(answer_text="……", citations_available=True, citations=[]))
    assert run.outcome is Outcome.SUCCESS
    assert run.answer.citations == []


def test_json_round_trip():
    run = make_run()
    restored = SurfaceRun.model_validate_json(run.model_dump_json())
    assert restored == run
