"""RETRIEVAL 诊断:有资格进检索候选吗?(5.2)

输入是需求层的"检索在场"指标(命中口径 = 品牌域名出现在候选集,
由检索排名判定器供数)与在场时的最优位次列表。
无检索观测通道 → NOT_OBSERVABLE,如实说,不用引用反推(铁律 7)。
"""

from __future__ import annotations

from statistics import median

from diagnosis.findings import (
    DiagnosisThresholds,
    Stage,
    StageFinding,
    StageStatus,
    fmt_estimate,
    interval_status,
)
from engine.scheduler import NeedSurfaceMetric


def diagnose_retrieval(
    metric: NeedSurfaceMetric | None,
    best_ranks: list[int],
    thresholds: DiagnosisThresholds = DiagnosisThresholds(),
) -> StageFinding:
    if metric is None or metric.estimate is None:
        return StageFinding(
            stage=Stage.RETRIEVAL,
            status=StageStatus.NOT_OBSERVABLE,
            headline="无检索观测通道或无有效样本,检索阶段不可观测",
            evidence_lines=["禁止用引用集反推检索排名(铁律 7),故此阶段如实记不可观测"],
        )
    estimate = metric.estimate
    accounting = metric.accounting
    status, notes = interval_status(
        estimate,
        low_confidence=accounting.low_confidence,
        fail_ci_high=thresholds.retrieval_fail_ci_high,
        pass_ci_low=thresholds.retrieval_pass_ci_low,
    )
    if best_ranks:
        rank_line = (
            f"在场样本中的最优位次:中位数第 {median(best_ranks):.0f} 位"
            f"(最好第 {min(best_ranks)} 位,共 {len(best_ranks)} 次在场)"
        )
    else:
        rank_line = "品牌域名从未进入检索候选集(k=0)"
    evidence = [
        f"检索候选在场率:{fmt_estimate(estimate)}",
        rank_line,
        f"口径:surface={metric.surface} model={metric.model} "
        f"N_valid={accounting.n_valid}/{accounting.n_total} "
        f"stop_reasons={[r.value for r in metric.stop_reasons]} "
        f"拒答率={accounting.refusal_rate:.0%} 失败率={accounting.failure_rate:.0%}",
        f"判定阈值:FAIL 上界≤{thresholds.retrieval_fail_ci_high:.0%},"
        f"PASS 下界≥{thresholds.retrieval_pass_ci_low:.0%}",
        *notes,
    ]
    headline = {
        StageStatus.FAIL: "品牌网站基本不进检索候选——缺席从检索层开始",
        StageStatus.WEAK: "品牌网站在检索候选中不稳定在场",
        StageStatus.PASS: "品牌网站稳定进入检索候选",
    }[status]
    return StageFinding(
        stage=Stage.RETRIEVAL,
        status=status,
        headline=headline,
        evidence_lines=evidence,
        low_confidence=accounting.low_confidence,
    )
