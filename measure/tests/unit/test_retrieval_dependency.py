"""检索依赖诊断规则测试(第 9 章挂靠):四档判定、同仪器校验、双语渲染。

规则只认区间端点(与 findings.interval_status 同家法),测试用手工构造的
区间验证每档的边界行为,不依赖 Jeffreys 数值。
"""

import pytest

from diagnosis.messages import render_msg
from diagnosis.retrieval_dependency import (
    DependencyLevel,
    diagnose_retrieval_dependency,
)
from engine.metrics import ProportionEstimate, account_outcomes
from engine.models import Outcome, StopReason
from engine.scheduler import NeedSurfaceMetric


def metric(
    ci_low: float | None = None,
    ci_high: float | None = None,
    *,
    surface: str = "s1",
    model: str = "m1",
) -> NeedSurfaceMetric:
    estimate = None
    if ci_low is not None:
        estimate = ProportionEstimate(
            k=5, n=10, point=(ci_low + ci_high) / 2, ci_low=ci_low, ci_high=ci_high
        )
    return NeedSurfaceMetric(
        information_need_id="need-1", provider="p", surface=surface, model=model,
        metric_label="brand_mention", k_hits=5,
        estimate=estimate,
        accounting=account_outcomes([Outcome.SUCCESS] * 10),
        stop_reasons=[StopReason.CI_CONVERGED],
        per_variant=[],
        variant_estimates=[estimate],
    )


def test_no_offline_leg_returns_none():
    assert diagnose_retrieval_dependency(metric(0.6, 0.9), None) is None


def test_interval_overlap_is_low():
    finding = diagnose_retrieval_dependency(metric(0.6, 0.9), metric(0.55, 0.8))
    assert finding.level is DependencyLevel.LOW


def test_touching_intervals_count_as_overlap():
    # 闭卷上界恰等于开卷下界:仍视为重叠(无法区分),不判分离
    finding = diagnose_retrieval_dependency(metric(0.6, 0.9), metric(0.3, 0.6))
    assert finding.level is DependencyLevel.LOW


def test_separated_beyond_half_is_high():
    # 闭卷上界 0.25 < 开卷下界 0.6 × 0.5 = 0.3 → HIGH
    finding = diagnose_retrieval_dependency(metric(0.6, 0.9), metric(0.05, 0.25))
    assert finding.level is DependencyLevel.HIGH


def test_separated_within_half_is_medium():
    # 分离(0.5 < 0.6)但未过半界(0.5 ≥ 0.3)→ MEDIUM
    finding = diagnose_retrieval_dependency(metric(0.6, 0.9), metric(0.35, 0.5))
    assert finding.level is DependencyLevel.MEDIUM


def test_absent_online_is_inconclusive():
    # 开卷区间上界 ≤ 0.20(缺席阈值):开卷都不提及,依赖度无从谈起
    finding = diagnose_retrieval_dependency(metric(0.02, 0.18), metric(0.02, 0.18))
    assert finding.level is DependencyLevel.INCONCLUSIVE
    assert any(line.code == "dependency.note.absent_online" for line in finding.evidence_lines)


def test_missing_estimate_is_inconclusive():
    assert (
        diagnose_retrieval_dependency(metric(), metric(0.1, 0.3)).level
        is DependencyLevel.INCONCLUSIVE
    )
    assert (
        diagnose_retrieval_dependency(metric(0.6, 0.9), metric()).level
        is DependencyLevel.INCONCLUSIVE
    )


def test_cross_instrument_comparison_raises():
    with pytest.raises(ValueError, match="跨仪器"):
        diagnose_retrieval_dependency(metric(0.6, 0.9, surface="s2"), metric(0.05, 0.25))


def test_intervals_always_shown_side_by_side_and_bilingual():
    """第 9 章:两区间必须并排展示,不做四象限硬判;结论双语渲染完备。"""
    for finding in (
        diagnose_retrieval_dependency(metric(0.6, 0.9), metric(0.05, 0.25)),
        diagnose_retrieval_dependency(metric(0.6, 0.9), metric(0.55, 0.8)),
        diagnose_retrieval_dependency(metric(), metric()),
    ):
        for lang in ("zh", "en"):
            assert render_msg(finding.headline, lang).strip()
            for line in finding.evidence_lines:
                assert render_msg(line, lang).strip()
    paired = diagnose_retrieval_dependency(metric(0.6, 0.9), metric(0.05, 0.25))
    assert any(line.code == "dependency.pair" for line in paired.evidence_lines)
    assert any(line.code == "dependency.thresholds" for line in paired.evidence_lines)
