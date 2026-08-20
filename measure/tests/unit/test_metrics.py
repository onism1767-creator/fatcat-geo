"""metrics 属性测试(12.1):防 CI 计算错误静默产出错误区间。

属性清单来自架构书 12.1:区间 ⊆ [0,1]、覆盖点估计、k=0 与 k=n 边界不退化、
同比例下 n 增大区间单调收窄。另加 Beta 对称性作为公式抄错的哨兵。
名义覆盖率的蒙特卡洛校验不进 CI,随调度器交付为手动脚本(12.1)。
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from engine.metrics import (
    LOW_CONFIDENCE_THRESHOLD,
    account_outcomes,
    estimate_proportion,
    jeffreys_interval,
)
from engine.models import Outcome

k_and_n = st.integers(min_value=1, max_value=300).flatmap(
    lambda n: st.tuples(st.integers(min_value=0, max_value=n), st.just(n))
)


@given(k_and_n)
def test_interval_within_unit_and_covers_point_estimate(kn):
    k, n = kn
    low, high = jeffreys_interval(k, n)
    assert 0.0 <= low <= k / n <= high <= 1.0


@pytest.mark.parametrize("n", [1, 5, 20, 100])
def test_boundaries_do_not_degenerate(n):
    low, high = jeffreys_interval(0, n)  # 品牌从未被引:我们的常态,Wald 在此失效
    assert low == 0.0 and 0.0 < high < 1.0
    low, high = jeffreys_interval(n, n)
    assert 0.0 < low < 1.0 and high == 1.0


@given(
    st.tuples(st.integers(0, 10), st.integers(1, 10)).filter(lambda ab: ab[0] <= ab[1]),
    st.integers(1, 8),
    st.integers(2, 6),
)
def test_same_proportion_larger_n_narrower_interval(ab, m, factor):
    a, b = ab
    small = jeffreys_interval(a * m, b * m)
    large = jeffreys_interval(a * m * factor, b * m * factor)
    assert (large[1] - large[0]) <= (small[1] - small[0]) + 1e-9


@given(k_and_n)
def test_beta_symmetry(kn):
    """区间(k,n) 与 区间(n-k,n) 应互为镜像——公式抄错的第一道哨兵。"""
    k, n = kn
    low, high = jeffreys_interval(k, n)
    mirror_low, mirror_high = jeffreys_interval(n - k, n)
    assert low == pytest.approx(1.0 - mirror_high, abs=1e-9)
    assert high == pytest.approx(1.0 - mirror_low, abs=1e-9)


def test_invalid_inputs_rejected():
    with pytest.raises(ValueError):
        jeffreys_interval(0, 0)
    with pytest.raises(ValueError):
        jeffreys_interval(5, 3)
    with pytest.raises(ValueError):
        jeffreys_interval(-1, 3)


def test_estimate_proportion_packaging():
    est = estimate_proportion(2, 8)
    assert est.point == pytest.approx(0.25)
    assert est.method == "jeffreys-95"
    assert est.ci_low < est.point < est.ci_high
    assert est.width == pytest.approx(est.ci_high - est.ci_low)


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
