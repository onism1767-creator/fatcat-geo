"""CITATION 诊断:AI 会选择你当参考来源吗?(5.2)

输出被引概率 + 区间 + 竞品差距。竞品对比遵循「区间重叠即无法区分」
原则(v0.4.2 附录):禁止在重叠区间上硬排序——与"无总分"同源。
"""

from __future__ import annotations

from diagnosis.findings import (
    DiagnosisThresholds,
    Stage,
    StageFinding,
    StageStatus,
    fmt_estimate,
    interval_status,
)
from engine.metrics import ProportionEstimate
from engine.scheduler import NeedSurfaceMetric


def _compare_line(focal: ProportionEstimate, name: str, competitor: ProportionEstimate) -> str:
    if focal.ci_high < competitor.ci_low:
        return f"显著落后于竞品 {name}(其被引 {fmt_estimate(competitor)})"
    if competitor.ci_high < focal.ci_low:
        return f"显著领先竞品 {name}(其被引 {fmt_estimate(competitor)})"
    return f"与竞品 {name} 区间重叠,无法区分(其被引 {fmt_estimate(competitor)})"


def diagnose_citation(
    metric: NeedSurfaceMetric | None,
    competitors: list[tuple[str, ProportionEstimate]] | None = None,
    thresholds: DiagnosisThresholds = DiagnosisThresholds(),
) -> StageFinding:
    if metric is None or metric.estimate is None:
        return StageFinding(
            stage=Stage.CITATION,
            status=StageStatus.NOT_OBSERVABLE,
            headline="无引用观测通道或无有效样本,引用阶段不可观测",
        )
    estimate = metric.estimate
    accounting = metric.accounting
    status, notes = interval_status(
        estimate,
        low_confidence=accounting.low_confidence,
        fail_ci_high=thresholds.citation_fail_ci_high,
        pass_ci_low=thresholds.citation_pass_ci_low,
    )
    evidence = [
        f"被引概率:{fmt_estimate(estimate)}",
        f"口径:surface={metric.surface} model={metric.model} "
        f"N_valid={accounting.n_valid}/{accounting.n_total} "
        f"stop_reasons={[r.value for r in metric.stop_reasons]} "
        f"拒答率={accounting.refusal_rate:.0%} 失败率={accounting.failure_rate:.0%}",
        f"判定阈值:FAIL 上界≤{thresholds.citation_fail_ci_high:.0%},"
        f"PASS 下界≥{thresholds.citation_pass_ci_low:.0%}",
    ]
    evidence.extend(_compare_line(estimate, name, comp) for name, comp in (competitors or []))
    evidence.extend(notes)
    headline = {
        StageStatus.FAIL: "品牌域名基本不进引用集",
        StageStatus.WEAK: "品牌域名进入引用集但不稳定",
        StageStatus.PASS: "品牌域名稳定进入引用集",
    }[status]
    return StageFinding(
        stage=Stage.CITATION,
        status=status,
        headline=headline,
        evidence_lines=evidence,
        low_confidence=accounting.low_confidence,
    )
