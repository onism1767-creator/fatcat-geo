"""生成端检索链路诊断:检索阶段唯一的观测来源(v0.4.26 S1 起)。

数据来自检索链路可观测的生成 surface(能力:supports_query_trace):
模型自拟检索词、递回的有序候选集——测的是「AI 实际的搜索习惯下,
你会不会被递上来」。不暴露检索链路的仪器,检索阶段如实记不可观测。

三个观测:
1. 候选在场比例(阶段状态的判定依据);
2. 检索词品牌入场比例(上游环节:「你在不在模型的候选名单里」);
3. 同仪器漏斗对比:候选在场 vs 被引——同一批运行内部的零混淆对比,
   把缺席劈成「没递上来」(检索可见度)与「递上来了没被引」(内容竞争力)。

方差如实呈现:检索词是模型现场自拟的,逐题并列,报告不假装精确。
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
    compare_rates,
    msg,
    prompt_msgs,
    rate_params,
    rate_status,
)
from engine.aggregate import ClassSurfaceMetric
from engine.metrics import Rate

_HEADLINES = {
    StageStatus.FAIL: "genret.headline.fail",
    StageStatus.WEAK: "genret.headline.weak",
    StageStatus.PASS: "genret.headline.pass",
}


def _prefixed(prefix: str, r: Rate) -> dict[str, float | int]:
    return {f"{prefix}_{key}": value for key, value in rate_params(r).items()}


def diagnose_generative_retrieval(
    presence_metric: ClassSurfaceMetric | None,
    best_ranks: list[int],
    query_rate: Rate | None,
    cited_rate: Rate | None,
    triggered_rate: Rate | None,
    thresholds: DiagnosisThresholds = DiagnosisThresholds(),
) -> StageFinding:
    """产出该 AI 自己的 RETRIEVAL 阶段结论(v0.4.11 分家:与引用同仪器,
    直接作为该 surface 漏斗的检索环节参与定位)。无有效样本 → NOT_OBSERVABLE。"""
    if presence_metric is None or presence_metric.rate is None:
        return StageFinding(
            stage=Stage.RETRIEVAL,
            status=StageStatus.NOT_OBSERVABLE,
            headline=msg("genret.headline.not_observable"),
        )
    r = presence_metric.rate
    accounting = presence_metric.accounting
    status, notes = rate_status(
        r,
        low_confidence=accounting.low_confidence,
        fail_below=thresholds.retrieval_fail_below,
        pass_from=thresholds.retrieval_pass_from,
    )
    evidence: list[Msg] = [msg("genret.presence", **rate_params(r))]
    evidence.append(
        msg("retrieval.rank_summary",
            median=float(median(best_ranks)), best=min(best_ranks), count=len(best_ranks))
        if best_ranks
        else msg("retrieval.rank_never")
    )
    if query_rate is not None:
        evidence.append(msg("genret.query_entry", **rate_params(query_rate)))
    if cited_rate is not None:
        evidence.append(msg("genret.funnel.pair", **_prefixed("presence", r), **_prefixed("cited", cited_rate)))
        # 同仪器漏斗:被引比例明显低于在场比例 → 资料递到了却常落选(内容竞争力)
        if compare_rates(r, cited_rate, thresholds.rate_gap) > 0:
            evidence.append(msg("genret.funnel.gap", gap=thresholds.rate_gap))
        else:
            evidence.append(msg("genret.funnel.aligned", gap=thresholds.rate_gap))
    if triggered_rate is not None:
        evidence.append(msg("genret.triggered", **rate_params(triggered_rate)))
    evidence.append(caliber_msg(presence_metric))
    evidence.extend(prompt_msgs(presence_metric))
    evidence.append(msg("retrieval.thresholds",
                        fail=thresholds.retrieval_fail_below,
                        pass_=thresholds.retrieval_pass_from))
    evidence.extend(notes)
    return StageFinding(
        stage=Stage.RETRIEVAL,
        status=status,
        headline=msg(_HEADLINES[status]),
        evidence_lines=evidence,
        low_confidence=accounting.low_confidence,
        rate=r,
    )
