"""CITATION 诊断:AI 会选择你当参考来源吗?(5.2)

输出被引概率 + 区间 + 竞品差距。竞品对比遵循「区间重叠即无法区分」
原则(v0.4.2 附录):禁止在重叠区间上硬排序——与"无总分"同源。
输出为结论码 + 参数(v1.1),组句在渲染层。
"""

from __future__ import annotations

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
from engine.metrics import ProportionEstimate
from engine.models import FrozenModel
from engine.scheduler import NeedSurfaceMetric

_HEADLINES = {
    StageStatus.FAIL: "citation.headline.fail",
    StageStatus.WEAK: "citation.headline.weak",
    StageStatus.PASS: "citation.headline.pass",
}


def _compare_line(focal: ProportionEstimate, name: str, competitor: ProportionEstimate) -> Msg:
    if focal.ci_high < competitor.ci_low:
        code = "citation.compare.behind"
    elif competitor.ci_high < focal.ci_low:
        code = "citation.compare.ahead"
    else:
        code = "citation.compare.overlap"
    return msg(code, name=name, **estimate_params(competitor))


class VerificationSummary(FrozenModel):
    """三档证据「已核验」层的需求级汇总(独立重抓被引 URL 的结果)。"""

    estimate: ProportionEstimate | None  # 已核验被引概率(分母与被引指标一致)
    checked_urls: int
    unreachable: int
    soft_misses: int  # 可达但正文未见品牌(疑似软 404/错标)


def diagnose_citation(
    metric: NeedSurfaceMetric | None,
    competitors: list[tuple[str, ProportionEstimate]] | None = None,
    thresholds: DiagnosisThresholds = DiagnosisThresholds(),
    verification: VerificationSummary | None = None,
) -> StageFinding:
    if metric is None or metric.estimate is None:
        return StageFinding(
            stage=Stage.CITATION,
            status=StageStatus.NOT_OBSERVABLE,
            headline=msg("citation.headline.not_observable"),
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
        msg("citation.rate", **estimate_params(estimate)),
        caliber_msg(metric),
        msg("citation.thresholds",
            fail=thresholds.citation_fail_ci_high, pass_=thresholds.citation_pass_ci_low),
    ]
    if verification is not None:
        evidence.append(msg("citation.tier_note"))
        if verification.estimate is not None:
            evidence.append(msg("citation.verified_rate", **estimate_params(verification.estimate)))
        evidence.append(msg(
            "citation.verify_summary",
            checked=verification.checked_urls,
            unreachable=verification.unreachable,
            soft=verification.soft_misses,
        ))
    evidence.extend(_compare_line(estimate, name, comp) for name, comp in (competitors or []))
    evidence.extend(notes)
    headline_code = _HEADLINES[status]
    if status is StageStatus.WEAK and metric.k_hits == 0:
        headline_code = "citation.headline.weak_zero"
    return StageFinding(
        stage=Stage.CITATION,
        status=status,
        headline=msg(headline_code),
        evidence_lines=evidence,
        low_confidence=accounting.low_confidence,
    )
