"""RETRIEVAL 诊断:有资格进检索候选吗?(5.2)

输入是需求层的"检索在场"指标(命中口径 = 品牌域名出现在候选集,
由检索排名判定器供数)与在场时的最优位次列表。
无检索观测通道 → NOT_OBSERVABLE,如实说,不用引用反推(铁律 7)。
输出为结论码 + 参数(v1.1),组句在渲染层。
"""

from __future__ import annotations

from statistics import median

from diagnosis.findings import (
    DiagnosisThresholds,
    Msg,
    Stage,
    StageFinding,
    StageStatus,
    caliber_msg,
    estimate_params,
    interval_status,
    msg,
)
from engine.scheduler import NeedSurfaceMetric

_HEADLINES = {
    StageStatus.FAIL: "retrieval.headline.fail",
    StageStatus.WEAK: "retrieval.headline.weak",
    StageStatus.PASS: "retrieval.headline.pass",
}


def diagnose_retrieval(
    metric: NeedSurfaceMetric | None,
    best_ranks: list[int],
    thresholds: DiagnosisThresholds = DiagnosisThresholds(),
) -> StageFinding:
    if metric is None or metric.estimate is None:
        return StageFinding(
            stage=Stage.RETRIEVAL,
            status=StageStatus.NOT_OBSERVABLE,
            headline=msg("retrieval.headline.not_observable"),
            evidence_lines=[msg("retrieval.not_observable_note")],
        )
    estimate = metric.estimate
    accounting = metric.accounting
    status, notes = interval_status(
        estimate,
        low_confidence=accounting.low_confidence,
        fail_ci_high=thresholds.retrieval_fail_ci_high,
        pass_ci_low=thresholds.retrieval_pass_ci_low,
    )
    rank_line: Msg = (
        msg("retrieval.rank_summary",
            median=float(median(best_ranks)), best=min(best_ranks), count=len(best_ranks))
        if best_ranks
        else msg("retrieval.rank_never")
    )
    evidence = [
        msg("retrieval.presence", **estimate_params(estimate)),
        rank_line,
        caliber_msg(metric),
        msg("retrieval.thresholds",
            fail=thresholds.retrieval_fail_ci_high, pass_=thresholds.retrieval_pass_ci_low),
        *notes,
    ]
    return StageFinding(
        stage=Stage.RETRIEVAL,
        status=status,
        headline=msg(_HEADLINES[status]),
        evidence_lines=evidence,
        low_confidence=accounting.low_confidence,
    )
