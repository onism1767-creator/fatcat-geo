"""metrics 测试(12.1):比例的边界与记账的分母规则(v0.4.26 S2:不再算区间;S3:空回答算无效)。"""

from datetime import UTC, datetime

import pytest

from engine.metrics import (
    LOW_CONFIDENCE_THRESHOLD,
    Rate,
    account_outcomes,
    account_runs,
    merge_accounting,
    rate,
    rate_of,
)
from engine.models import Answer, Outcome, RunError, RunErrorType, SearchMode, SurfaceRun


def test_rate_packaging_and_share():
    r = rate(2, 8)
    assert (r.k, r.n) == (2, 8) and r.share == pytest.approx(0.25)
    assert rate(0, 5).share == 0.0 and rate(5, 5).share == 1.0


def test_invalid_rates_rejected():
    with pytest.raises(ValueError):
        rate(0, 0)
    with pytest.raises(ValueError):
        rate(5, 3)
    with pytest.raises(ValueError):
        rate(-1, 3)


def test_rate_of_flags_and_empty_is_none():
    assert rate_of([True, False, True]) == Rate(k=2, n=3)
    assert rate_of([]) is None  # 没有观测 → None,不是 0


def test_account_outcomes_denominator_rule():
    """7.3:分母仅含 SUCCESS;拒答/失败不进分母但计入口径。"""
    outcomes = (
        [Outcome.SUCCESS] * 22
        + [Outcome.REFUSED]
        + [Outcome.TRANSIENT_FAILURE] * 2
    )
    acc = account_outcomes(outcomes)
    assert acc.n_total == 25 and acc.n_valid == 22
    assert acc.n_refused == 1 and acc.n_transient_failure == 2 and acc.n_permanent_failure == 0
    assert acc.refusal_rate == pytest.approx(0.04)
    assert acc.failure_rate == pytest.approx(0.08)
    assert acc.invalid_rate == pytest.approx(0.12)
    assert acc.low_confidence is False


def test_account_outcomes_low_confidence_flag():
    outcomes = [Outcome.SUCCESS] * 7 + [Outcome.TRANSIENT_FAILURE] * 3
    acc = account_outcomes(outcomes)
    assert acc.invalid_rate == pytest.approx(0.3)
    assert acc.invalid_rate > LOW_CONFIDENCE_THRESHOLD
    assert acc.low_confidence is True


def test_account_outcomes_empty_is_safe():
    acc = account_outcomes([])
    assert acc.n_total == 0 and acc.n_valid == 0
    assert acc.low_confidence is False


def test_merge_accounting_recomputes_ratios_on_totals():
    merged = merge_accounting([
        account_outcomes([Outcome.SUCCESS] * 3, n_account_error=1, n_empty=1),
        account_outcomes([Outcome.SUCCESS, Outcome.TRANSIENT_FAILURE]),
    ])
    assert merged.n_total == 5 and merged.n_valid == 3 and merged.n_empty == 1 and merged.n_account_error == 1
    assert merged.failure_rate == pytest.approx(0.2) and merged.invalid_rate == pytest.approx(0.4)
    assert merged.low_confidence is True  # 空回答也是无效样本,一样会把置信度拉低


def test_empty_answers_leave_the_denominator_but_stay_in_the_caliber():
    """产品文档 15.2:拒答、空回答、接口错误都是无效回答——不进分母,数量单独给。"""
    acc = account_outcomes([Outcome.SUCCESS] * 10 + [Outcome.REFUSED], n_empty=2)
    assert acc.n_total == 11 and acc.n_valid == 8 and acc.n_empty == 2 and acc.n_refused == 1
    assert acc.invalid_rate == pytest.approx(3 / 11) and acc.failure_rate == 0.0
    with pytest.raises(ValueError):
        account_outcomes([Outcome.SUCCESS], n_empty=2)  # 空回答不可能比成功还多


def _run(run_id: str, *, outcome=Outcome.SUCCESS, text: str | None = "回答", error_type=RunErrorType.TRANSIENT):
    return SurfaceRun(
        run_id=run_id, prompt_id="p", run_index=1, provider="test_provider", surface="test_surface", model="m1",
        search_mode=SearchMode.DEFAULT, locale="zh-CN", outcome=outcome,
        error=None if outcome is Outcome.SUCCESS else RunError(error_type=error_type, message="x"),
        answer=Answer(answer_text=text, citations_available=True) if text is not None else None,
        raw_payload={}, timestamp=datetime(2026, 10, 12, tzinfo=UTC), latency_ms=1, cost_usd=0.0,
    )


def test_account_runs_counts_empty_answers_from_the_runs_themselves():
    runs = [
        _run("ok"), _run("blank", text="  \n"), _run("none", text=None),
        _run("refused", outcome=Outcome.REFUSED, text=None, error_type=RunErrorType.REFUSAL),
        _run("failed", outcome=Outcome.TRANSIENT_FAILURE, text=None),
    ]
    acc = account_runs(runs, n_account_error=1)
    assert (acc.n_total, acc.n_valid, acc.n_empty, acc.n_refused, acc.n_transient_failure) == (5, 1, 2, 1, 1)
    assert acc.n_account_error == 1 and acc.invalid_rate == pytest.approx(0.8)
