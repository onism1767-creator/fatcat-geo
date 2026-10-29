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
    MentionCompare,
    MentionShare,
    Msg,
    SentimentTally,
    Stage,
    StageFinding,
    StageStatus,
    accounting_params,
    compare_rates,
    msg,
    prompt_msgs,
    rate_params,
    rate_status,
)
from engine.aggregate import ClassSurfaceMetric
from engine.metrics import Rate
from engine.models import PromptIntent, Sentiment

_RELATION = {1: "less", -1: "more", 0: "close"}  # compare_rates(你, 竞品):你高 → 竞品比你少


def compare_mentions(
    brand_name: str,
    mention_metric: ClassSurfaceMetric | None,
    competitors: list[tuple[str, Rate]],
    gap: float = DiagnosisThresholds().rate_gap,
) -> MentionCompare | None:
    """竞品提及对比(S7):没有竞品、或你的提及比例看不到 → None(主视图不出这一块)。"""
    if mention_metric is None or mention_metric.rate is None or not competitors:
        return None
    r = mention_metric.rate
    return MentionCompare(
        brand=MentionShare(name=brand_name, rate=r),
        competitors=[MentionShare(name=name, rate=c, relation=_RELATION[compare_rates(r, c, gap)])
                     for name, c in competitors],
        gap=gap,
    )


def sentiment_tally(counts: Mapping[Sentiment, int]) -> SentimentTally | None:
    """主视图「AI 怎么评价你」的原料(S8.1):按句计数;一句没有 → None(不画图、不出这一行)。"""
    tally = SentimentTally(
        positive=counts.get(Sentiment.POSITIVE, 0), neutral=counts.get(Sentiment.NEUTRAL, 0),
        negative=counts.get(Sentiment.NEGATIVE, 0), mixed=counts.get(Sentiment.MIXED, 0),
    )
    return tally if tally.total else None


def _compare_lines(r: Rate, competitors: list[tuple[str, Rate]], gap: float) -> list[Msg]:
    """证据行:回答里提到各竞品的次数,与提到你的比(差 ≥ gap 才说多 / 少;S7)。"""
    return [msg(f"mention.compare.{_RELATION[compare_rates(r, c, gap)]}", name=name, pts=round(gap * 100),
                **rate_params(c))
            for name, c in competitors]


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
    return msg(
        "repr.caliber",
        surface=metric.surface,
        model=metric.model,
        n_prompts=len(metric.per_prompt),
        stop_reasons=", ".join(r.value for r in metric.stop_reasons),
        intent=intent.value,
        **accounting_params(metric.accounting),
    )


def diagnose_representation(
    intent: PromptIntent,
    mention_metric: ClassSurfaceMetric | None,
    sentiment_counts: Mapping[Sentiment, int],
    thresholds: DiagnosisThresholds = DiagnosisThresholds(),
    competitors: list[tuple[str, Rate]] | None = None,
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
    compare = _compare_lines(r, competitors or [], thresholds.rate_gap)  # 和竞品比谁被提得多(S7)

    if intent is PromptIntent.BRAND_FACT:
        # 点名焦点品牌的题:提及率无判定价值,状态由情感决定
        if n_sentiment == 0:
            return StageFinding(
                stage=Stage.REPRESENTATION,
                status=StageStatus.NOT_OBSERVABLE,
                headline=msg("repr.headline.probe_none"),
                rate=r,
                evidence_lines=[
                    msg("repr.mention_rate", **rate_params(r)),
                    *compare,
                    caliber,
                    *prompt_msgs(mention_metric),
                    msg("repr.claims_note"),
                ],
            )
        if negative_share >= thresholds.negative_share_fail:
            status = StageStatus.FAIL
        elif negative_share > thresholds.negative_share_weak:
            status = StageStatus.WEAK
        else:
            status = StageStatus.PASS
        notes: list[Msg] = []
        headline = {
            StageStatus.FAIL: "repr.headline.probe_fail",
            StageStatus.WEAK: "repr.headline.probe_weak",
            StageStatus.PASS: "repr.headline.probe_pass",
        }[status]
        if accounting.low_confidence:
            # 失败 / 拒答太多:不许下确定性结论,一律降 WEAK(7.3;审核 2026-10-27 黄 13),标题也换成「说不准」——
            # 不能借用「有明显负面内容」那句:全是好评、只是失败多时,那句话和数据正好相反(第二轮红 2)
            status, headline = StageStatus.WEAK, "repr.headline.probe_unsure"
            notes = [msg("common.low_confidence_downgrade")]
        return StageFinding(
            stage=Stage.REPRESENTATION,
            status=status,
            headline=msg(headline),
            rate=r,
            evidence_lines=[
                sentiment_line,
                *notes,
                *compare,
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
        rate=r,
        evidence_lines=[
            msg("repr.mention_rate", **rate_params(r)),
            *compare,
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
