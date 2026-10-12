"""REPRESENTATION 诊断:AI 怎么描述你?(5.2)

指标解释按题型分层(产品文档 15.2):
- 不点名的四类题:先看提及比例——未稳定进入回答,就谈不上"被如何描述";提及充分再看情感分布。
- 品牌事实(点名焦点品牌):提及率天然≈100%,无判定价值;状态只由情感分布决定。

描述正确性判定需要品牌事实清单作参照,v1 未启用,如实标注。
输出为结论码 + 参数(v1.1),组句在渲染层。
"""

from __future__ import annotations

from collections.abc import Mapping

from diagnosis.findings import (
    DiagnosisThresholds,
    Msg,
    Stage,
    StageFinding,
    StageStatus,
    msg,
    prompt_msgs,
    rate_params,
    rate_status,
)
from engine.aggregate import ClassSurfaceMetric
from engine.models import PromptIntent, Sentiment


def _sentiment_summary(counts: Mapping[Sentiment, int]) -> tuple[int, float, Msg]:
    total = sum(counts.values())
    negative = counts.get(Sentiment.NEGATIVE, 0)
    share = negative / total if total else 0.0
    line = msg(
        "repr.sentiment_dist",
        positive=counts.get(Sentiment.POSITIVE, 0),
        neutral=counts.get(Sentiment.NEUTRAL, 0),
        negative=negative,
        mixed=counts.get(Sentiment.MIXED, 0),
        share=share,
    )
    return total, share, line


def _caliber(metric: ClassSurfaceMetric, intent: PromptIntent) -> Msg:
    accounting = metric.accounting
    return msg(
        "repr.caliber",
        surface=metric.surface,
        model=metric.model,
        n_prompts=len(metric.per_prompt),
        n_valid=accounting.n_valid,
        n_total=accounting.n_total,
        stop_reasons=", ".join(r.value for r in metric.stop_reasons),
        intent=intent.value,
    )


def diagnose_representation(
    intent: PromptIntent,
    mention_metric: ClassSurfaceMetric | None,
    sentiment_counts: Mapping[Sentiment, int],
    thresholds: DiagnosisThresholds = DiagnosisThresholds(),
) -> StageFinding:
    if mention_metric is None or mention_metric.rate is None:
        return StageFinding(
            stage=Stage.REPRESENTATION,
            status=StageStatus.NOT_OBSERVABLE,
            headline=msg("repr.headline.not_observable"),
        )
    r = mention_metric.rate
    accounting = mention_metric.accounting
    n_sentiment, negative_share, sentiment_line = _sentiment_summary(sentiment_counts)
    caliber = _caliber(mention_metric, intent)

    if intent is PromptIntent.BRAND_FACT:
        # 点名焦点品牌的题:提及率无判定价值,状态由情感决定
        if n_sentiment == 0:
            return StageFinding(
                stage=Stage.REPRESENTATION,
                status=StageStatus.NOT_OBSERVABLE,
                headline=msg("repr.headline.probe_none"),
                evidence_lines=[
                    msg("repr.mention_rate", **rate_params(r)),
                    caliber,
                    *prompt_msgs(mention_metric),
                    msg("repr.claims_note"),
                ],
            )
        if negative_share >= thresholds.negative_share_fail:
            status, headline = StageStatus.FAIL, "repr.headline.probe_fail"
        elif negative_share > thresholds.negative_share_weak:
            status, headline = StageStatus.WEAK, "repr.headline.probe_weak"
        else:
            status, headline = StageStatus.PASS, "repr.headline.probe_pass"
        return StageFinding(
            stage=Stage.REPRESENTATION,
            status=status,
            headline=msg(headline),
            evidence_lines=[
                sentiment_line,
                msg("repr.thresholds.probe",
                    weak=thresholds.negative_share_weak, fail=thresholds.negative_share_fail),
                caliber,
                *prompt_msgs(mention_metric),
                msg("repr.claims_note"),
            ],
            low_confidence=accounting.low_confidence,
        )

    # 不点名的题:先提及比例,后情感封顶
    status, notes = rate_status(
        r,
        low_confidence=accounting.low_confidence,
        fail_below=thresholds.mention_fail_below,
        pass_from=thresholds.mention_pass_from,
    )
    if status is StageStatus.PASS and negative_share > thresholds.negative_share_weak:
        notes.append(msg("repr.negative_cap", share=negative_share))
        status = StageStatus.WEAK
    if status is StageStatus.FAIL:
        headline = "repr.headline.neutral_fail"
        sentiment_part = (
            [msg("repr.sentiment_insufficient")] if n_sentiment == 0 else [sentiment_line]
        )
    else:
        headline = {
            StageStatus.WEAK: "repr.headline.neutral_weak",
            StageStatus.PASS: "repr.headline.neutral_pass",
        }[status]
        sentiment_part = [sentiment_line]
    return StageFinding(
        stage=Stage.REPRESENTATION,
        status=status,
        headline=msg(headline),
        evidence_lines=[
            msg("repr.mention_rate", **rate_params(r)),
            *sentiment_part,
            msg("repr.thresholds.neutral",
                fail=thresholds.mention_fail_below, pass_=thresholds.mention_pass_from,
                neg=thresholds.negative_share_weak),
            caliber,
            *prompt_msgs(mention_metric),
            msg("repr.claims_note"),
            *notes,
        ],
        low_confidence=accounting.low_confidence,
    )
