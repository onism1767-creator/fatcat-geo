"""检索依赖诊断规则测试(第 9 章挂靠):四档判定、同仪器校验、双语渲染。

规则直接看比例(与 findings.rate_status 同家法,v0.4.26 S2),测试用手工构造的
比例验证每档的边界行为。
"""

import pytest

from diagnosis.messages import render_msg
from diagnosis.retrieval_dependency import (
    DependencyLevel,
    diagnose_retrieval_dependency,
)
from engine.aggregate import ClassSurfaceMetric
from engine.metrics import Rate, account_outcomes
from engine.models import Outcome, PromptIntent, StopReason


def metric(
    k: int | None = None,
    n: int = 10,
    *,
    surface: str = "s1",
    model: str = "m1",
) -> ClassSurfaceMetric:
    r = Rate(k=k, n=n) if k is not None else None
    return ClassSurfaceMetric(
        intent=PromptIntent.CATEGORY_DISCOVERY, provider="p", surface=surface, model=model,
        metric_label="brand_mention", k_hits=k or 0,
        rate=r,
        accounting=account_outcomes([Outcome.SUCCESS] * n),
        stop_reasons=[StopReason.COMPLETED],
        per_prompt=[r],
        prompt_coverage=None,
    )


def test_no_offline_leg_returns_none():
    assert diagnose_retrieval_dependency(metric(8), None) is None


def test_close_rates_are_low():
    # 开卷 80%、闭卷 70%:相差 10 个百分点,不足 20 → 分不出,LOW
    finding = diagnose_retrieval_dependency(metric(8), metric(7))
    assert finding.level is DependencyLevel.LOW


def test_gap_just_under_threshold_is_still_low():
    # 开卷 80%、闭卷 65%:相差 15 → 仍 LOW
    finding = diagnose_retrieval_dependency(metric(16, 20), metric(13, 20))
    assert finding.level is DependencyLevel.LOW


def test_offline_below_half_of_online_is_high():
    # 闭卷 20% < 开卷 80% × 0.5 = 40% → HIGH
    finding = diagnose_retrieval_dependency(metric(8), metric(2))
    assert finding.level is DependencyLevel.HIGH


def test_gap_at_least_threshold_but_above_half_is_medium():
    # 开卷 80%、闭卷 50%:相差 30 ≥ 20,且 50% ≥ 40% → MEDIUM
    finding = diagnose_retrieval_dependency(metric(8), metric(5))
    assert finding.level is DependencyLevel.MEDIUM


def test_absent_online_is_inconclusive():
    # 开卷比例 < 20%(缺席线):开卷都不提及,依赖度无从谈起
    finding = diagnose_retrieval_dependency(metric(1), metric(1))
    assert finding.level is DependencyLevel.INCONCLUSIVE
    assert any(line.code == "dependency.note.absent_online" for line in finding.evidence_lines)


def test_missing_rate_is_inconclusive():
    assert diagnose_retrieval_dependency(metric(), metric(2)).level is DependencyLevel.INCONCLUSIVE
    assert diagnose_retrieval_dependency(metric(8), metric()).level is DependencyLevel.INCONCLUSIVE


def test_cross_instrument_comparison_raises():
    with pytest.raises(ValueError, match="跨仪器"):
        diagnose_retrieval_dependency(metric(8, surface="s2"), metric(2))


def test_rates_always_shown_side_by_side_and_bilingual():
    """第 9 章:两个比例必须并排展示,不做四象限硬判;结论双语渲染完备。"""
    for finding in (
        diagnose_retrieval_dependency(metric(8), metric(2)),
        diagnose_retrieval_dependency(metric(8), metric(7)),
        diagnose_retrieval_dependency(metric(), metric()),
    ):
        for lang in ("zh", "en"):
            assert render_msg(finding.headline, lang).strip()
            for line in finding.evidence_lines:
                assert render_msg(line, lang).strip()
    paired = diagnose_retrieval_dependency(metric(8), metric(2))
    assert any(line.code == "dependency.pair" for line in paired.evidence_lines)
    assert any(line.code == "dependency.thresholds" for line in paired.evidence_lines)
    pair = next(line for line in paired.evidence_lines if line.code == "dependency.pair")
    assert "8/10 = 80%" in render_msg(pair, "zh")


def test_offline_higher_than_online_is_low_but_said_plainly():
    """闭卷 90% 比开卷 30% 还高:依赖度 LOW,但标题不能说「接近、未观测到差异」(审核 2026-10-27 黄 15)。"""
    finding = diagnose_retrieval_dependency(metric(3), metric(9))
    assert finding.level is DependencyLevel.LOW
    assert finding.headline.code == "dependency.headline.offline_higher"
    assert "闭卷" in render_msg(finding.headline, "zh") and "search-off" in render_msg(finding.headline, "en")
    assert "记住了品牌" in render_msg(finding.headline, "zh")  # 第 9 章:只说观测差异,不说「它认识你」(第二轮)
    assert "不联网" in render_msg(finding.headline, "zh", "plain")  # 人话版也有
    assert diagnose_retrieval_dependency(metric(8), metric(9)).headline.code == "dependency.headline.low"  # 差 10:接近


def test_online_absent_but_offline_high_is_stated_not_called_inconclusive():
    """联网 0/10、不联网 9/10:最显眼的反差,不能说「看不出来」(第二轮)。"""
    finding = diagnose_retrieval_dependency(metric(0), metric(9))
    assert finding.level is DependencyLevel.LOW and finding.headline.code == "dependency.headline.offline_higher"
    near_zero = diagnose_retrieval_dependency(metric(0), metric(1))  # 两边都很低:照旧说看不出来
    assert near_zero.level is DependencyLevel.INCONCLUSIVE
    rule = render_msg(finding.evidence_lines[2], "zh")
    assert "20 个百分点" in rule and "20%" not in rule.split("开卷低于")[0]
