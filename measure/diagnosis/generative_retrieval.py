"""生成端检索通道诊断(v0.4.8 双通道架构的另一半)。

数据来自检索链路可观测的生成 surface(能力:supports_query_trace):
模型自拟检索词、递回的有序候选集——测的是「AI 实际的搜索习惯下,
你会不会被递上来」,与标准化裸检索(受控查询)是两台仪器、两个口径,
并列展示,永不合成。

三个观测:
1. 候选在场率(通道状态的判定依据,阈值与裸检索共用「检索在场」概念);
2. 检索词品牌入场率(上游环节:「你在不在模型的候选名单里」);
3. 同仪器漏斗对比:候选在场 vs 被引——同一批运行内部的零混淆对比,
   把缺席劈成「没递上来」(检索可见度)与「递上来了没被引」(内容竞争力)。

方差如实呈现:检索词是模型现场自拟的,区间宽就宽,报告不假装精确。
未触发检索的运行计入分母——不检索本身就是自然行为的一部分(口径行注明)。
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
    variant_msgs,
)
from engine.metrics import ProportionEstimate
from engine.scheduler import NeedSurfaceMetric

_HEADLINES = {
    StageStatus.FAIL: "genret.headline.fail",
    StageStatus.WEAK: "genret.headline.weak",
    StageStatus.PASS: "genret.headline.pass",
}


def _prefixed(prefix: str, estimate: ProportionEstimate) -> dict[str, float | int]:
    return {f"{prefix}_{key}": value for key, value in estimate_params(estimate).items()}


def diagnose_generative_retrieval(
    presence_metric: NeedSurfaceMetric | None,
    best_ranks: list[int],
    query_estimate: ProportionEstimate | None,
    cited_estimate: ProportionEstimate | None,
    triggered_estimate: ProportionEstimate | None,
    thresholds: DiagnosisThresholds = DiagnosisThresholds(),
) -> StageFinding:
    """产出该 AI 自己的 RETRIEVAL 阶段结论(v0.4.11 分家:与引用同仪器,
    直接作为该 surface 漏斗的检索环节参与定位)。无有效样本 → NOT_OBSERVABLE。"""
    if presence_metric is None or presence_metric.estimate is None:
        return StageFinding(
            stage=Stage.RETRIEVAL,
            status=StageStatus.NOT_OBSERVABLE,
            headline=msg("genret.headline.not_observable"),
        )
    estimate = presence_metric.estimate
    accounting = presence_metric.accounting
    status, notes = interval_status(
        estimate,
        low_confidence=accounting.low_confidence,
        fail_ci_high=thresholds.retrieval_fail_ci_high,
        pass_ci_low=thresholds.retrieval_pass_ci_low,
    )
    evidence: list[Msg] = [msg("genret.presence", **estimate_params(estimate))]
    evidence.append(
        msg("retrieval.rank_summary",
            median=float(median(best_ranks)), best=min(best_ranks), count=len(best_ranks))
        if best_ranks
        else msg("retrieval.rank_never")
    )
    if query_estimate is not None:
        evidence.append(msg("genret.query_entry", **estimate_params(query_estimate)))
    if cited_estimate is not None:
        evidence.append(msg("genret.funnel.pair",
                            **_prefixed("presence", estimate), **_prefixed("cited", cited_estimate)))
        # 同仪器漏斗:被引区间显著低于在场区间 → 资料递到了却常落选(内容竞争力)
        if cited_estimate.ci_high < estimate.ci_low:
            evidence.append(msg("genret.funnel.gap"))
        else:
            evidence.append(msg("genret.funnel.aligned"))
    if triggered_estimate is not None:
        evidence.append(msg("genret.triggered", **estimate_params(triggered_estimate)))
    evidence.append(caliber_msg(presence_metric))
    evidence.extend(variant_msgs(presence_metric))
    evidence.append(msg("retrieval.thresholds",
                        fail=thresholds.retrieval_fail_ci_high,
                        pass_=thresholds.retrieval_pass_ci_low))
    evidence.extend(notes)
    return StageFinding(
        stage=Stage.RETRIEVAL,
        status=status,
        headline=msg(_HEADLINES[status]),
        evidence_lines=evidence,
        low_confidence=accounting.low_confidence,
    )
