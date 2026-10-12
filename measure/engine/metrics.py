"""L2 阶段指标:计数与比例(产品文档 15.2;v0.4.26 S2)。

不显示区间:同一道题的几次回答是一簇,72 条回答不是 72 个独立样本,把它们当独立的伯努利试验
算区间是虚假精度。写法一律「k / n = 比例」;以后要做区间,以题为抽样单位做 cluster bootstrap(另案)。

分母规则以 7.3 为唯一权威:比例指标分母 = outcome 为 SUCCESS 的运行数;
拒答与失败不进分母,但拒答率/失败率必须作为口径随指标输出;
无效样本占比超阈值 → LOW_CONFIDENCE,诊断规则须降权。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from engine.models import FrozenModel, Outcome

LOW_CONFIDENCE_THRESHOLD = 0.20  # 7.3:无效样本占比超此阈值 → LOW_CONFIDENCE(可配)


class Rate(FrozenModel):
    """一个比例指标:k 次命中 / n 次有效观测。

    这只是数值部分;完整口径(surface/model/题数/每题次数/stop_reason/时间窗/地区)
    由聚合层在其外层补齐(7.4),缺口径的数值不允许单独进报告。
    """

    k: int
    n: int

    @property
    def share(self) -> float:
        return self.k / self.n


def rate(k: int, n: int) -> Rate:
    if n <= 0:
        raise ValueError(f"n 必须为正,收到 {n}")
    if not 0 <= k <= n:
        raise ValueError(f"k 必须在 [0, n] 内,收到 k={k}, n={n}")
    return Rate(k=k, n=n)


def rate_of(flags: list[bool]) -> Rate | None:
    """一组是/否观测的比例;没有观测 → None(不进分母,不是 0)。"""
    return rate(sum(flags), len(flags)) if flags else None


class SampleAccounting(FrozenModel):
    """采样终态记账(7.3 口径):分母、拒答率、失败率与置信降级标记。"""

    n_total: int
    n_valid: int  # outcome=SUCCESS,比例指标的唯一合法分母
    n_refused: int
    n_transient_failure: int
    n_permanent_failure: int
    refusal_rate: float
    failure_rate: float
    invalid_rate: float  # (拒答+失败)/总数;LOW_CONFIDENCE 的判定依据(取保守口径)
    low_confidence: bool
    # v0.4.21:我方账户问题(认证 / 额度 / 欠费)导致没发出的观测。照常落库、随口径输出,
    # 但不是尝试:不进 n_total,不进失败率——那是我们的账户,不是 AI 的行为
    n_account_error: int = 0


def merge_accounting(
    parts: Iterable[SampleAccounting],
    low_confidence_threshold: float = LOW_CONFIDENCE_THRESHOLD,
) -> SampleAccounting:
    """合并多份记账,比率按合并后总数重算。"""
    n_total = n_valid = n_refused = n_transient = n_permanent = n_account = 0
    for part in parts:
        n_total += part.n_total
        n_valid += part.n_valid
        n_refused += part.n_refused
        n_transient += part.n_transient_failure
        n_permanent += part.n_permanent_failure
        n_account += part.n_account_error
    failure_rate = (n_transient + n_permanent) / n_total if n_total else 0.0
    refusal_rate = n_refused / n_total if n_total else 0.0
    invalid_rate = (n_total - n_valid) / n_total if n_total else 0.0
    return SampleAccounting(
        n_total=n_total,
        n_valid=n_valid,
        n_refused=n_refused,
        n_transient_failure=n_transient,
        n_permanent_failure=n_permanent,
        refusal_rate=refusal_rate,
        failure_rate=failure_rate,
        invalid_rate=invalid_rate,
        low_confidence=invalid_rate > low_confidence_threshold,
        n_account_error=n_account,
    )


def account_outcomes(
    outcomes: Iterable[Outcome],
    low_confidence_threshold: float = LOW_CONFIDENCE_THRESHOLD,
    *,
    n_account_error: int = 0,
) -> SampleAccounting:
    """outcomes 只含真正发出的尝试;账户问题另计(n_account_error),不进任何比率。"""
    counts = Counter(outcomes)
    n_total = sum(counts.values())
    n_valid = counts[Outcome.SUCCESS]
    n_refused = counts[Outcome.REFUSED]
    n_transient = counts[Outcome.TRANSIENT_FAILURE]
    n_permanent = counts[Outcome.PERMANENT_FAILURE]
    failure_rate = (n_transient + n_permanent) / n_total if n_total else 0.0
    refusal_rate = n_refused / n_total if n_total else 0.0
    invalid_rate = (n_total - n_valid) / n_total if n_total else 0.0
    return SampleAccounting(
        n_total=n_total,
        n_valid=n_valid,
        n_refused=n_refused,
        n_transient_failure=n_transient,
        n_permanent_failure=n_permanent,
        refusal_rate=refusal_rate,
        failure_rate=failure_rate,
        invalid_rate=invalid_rate,
        low_confidence=invalid_rate > low_confidence_threshold,
        n_account_error=n_account_error,
    )
