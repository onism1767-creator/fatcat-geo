"""metrics 测试(12.1):比例的边界与记账的分母规则(v0.4.26 S2:不再算区间)。"""

import pytest

from engine.metrics import LOW_CONFIDENCE_THRESHOLD, Rate, account_outcomes, merge_accounting, rate, rate_of
from engine.models import Outcome


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
        account_outcomes([Outcome.SUCCESS] * 3, n_account_error=1),
        account_outcomes([Outcome.SUCCESS, Outcome.TRANSIENT_FAILURE]),
    ])
    assert merged.n_total == 5 and merged.n_valid == 4 and merged.n_account_error == 1
    assert merged.failure_rate == pytest.approx(0.2) and merged.low_confidence is False
