"""REPRESENTATION 诊断:AI 怎么描述你?(5.2)

指标解释按需求意图分层(17.2):
- 中立需求(discovery/comparison/scenario):先看提及率——未稳定进入回答,
  就谈不上"被如何描述";提及充分再看情感分布。
- brand_probe(点名探针):提及率天然≈100%,无判定价值;状态只由情感分布决定。

描述正确性判定需要品牌事实清单作参照,v1 未启用,如实标注。
"""

from __future__ import annotations

from typing import Mapping

from diagnosis.findings import (
    DiagnosisThresholds,
    Stage,
    StageFinding,
    StageStatus,
    fmt_estimate,
    interval_status,
)
from engine.models import NeedIntent, Sentiment
from engine.scheduler import NeedSurfaceMetric

_CLAIMS_NOTE = "描述正确性判定需品牌事实清单作参照,v1 未启用(路线图 v1.x)"


def _sentiment_summary(counts: Mapping[Sentiment, int]) -> tuple[int, float, str]:
    total = sum(counts.values())
    negative = counts.get(Sentiment.NEGATIVE, 0)
    share = negative / total if total else 0.0
    detail = "、".join(
        f"{sentiment.value} {count}" for sentiment, count in counts.items() if count
    ) or "无"
    return total, share, f"提及句情感分布:{detail}(负面占比 {share:.0%})"


def diagnose_representation(
    intent: NeedIntent,
    mention_metric: NeedSurfaceMetric | None,
    sentiment_counts: Mapping[Sentiment, int],
    thresholds: DiagnosisThresholds = DiagnosisThresholds(),
) -> StageFinding:
    if mention_metric is None or mention_metric.estimate is None:
        return StageFinding(
            stage=Stage.REPRESENTATION,
            status=StageStatus.NOT_OBSERVABLE,
            headline="无生成回答观测,表述阶段不可观测",
        )
    estimate = mention_metric.estimate
    accounting = mention_metric.accounting
    n_sentiment, negative_share, sentiment_line = _sentiment_summary(sentiment_counts)
    caliber = (
        f"口径:surface={mention_metric.surface} model={mention_metric.model} "
        f"N_valid={accounting.n_valid}/{accounting.n_total} "
        f"stop_reasons={[r.value for r in mention_metric.stop_reasons]} "
        f"意图={intent.value}"
    )

    if intent is NeedIntent.BRAND_PROBE:
        # 探针需求:提及率无判定价值,状态由情感决定
        if n_sentiment == 0:
            return StageFinding(
                stage=Stage.REPRESENTATION,
                status=StageStatus.NOT_OBSERVABLE,
                headline="探针需求下无可分析的提及句(异常,建议检查采样)",
                evidence_lines=[f"提及率:{fmt_estimate(estimate)}", caliber, _CLAIMS_NOTE],
            )
        if negative_share >= thresholds.negative_share_fail:
            status, headline = StageStatus.FAIL, "点名询问下 AI 对品牌的描述以负面为主"
        elif negative_share > thresholds.negative_share_weak:
            status, headline = StageStatus.WEAK, "点名询问下 AI 对品牌的描述含明显负面成分"
        else:
            status, headline = StageStatus.PASS, "点名询问下 AI 对品牌的描述以中性/正面为主"
        return StageFinding(
            stage=Stage.REPRESENTATION,
            status=status,
            headline=headline,
            evidence_lines=[
                sentiment_line,
                f"判定阈值:负面占比 >{thresholds.negative_share_weak:.0%} 为 WEAK,"
                f"≥{thresholds.negative_share_fail:.0%} 为 FAIL(探针需求提及率不参与判定)",
                caliber,
                _CLAIMS_NOTE,
            ],
            low_confidence=accounting.low_confidence,
        )

    # 中立需求:先提及率,后情感封顶
    status, notes = interval_status(
        estimate,
        low_confidence=accounting.low_confidence,
        fail_ci_high=thresholds.mention_fail_ci_high,
        pass_ci_low=thresholds.mention_pass_ci_low,
    )
    if status is StageStatus.PASS and negative_share > thresholds.negative_share_weak:
        notes.append(f"提及充分但负面占比 {negative_share:.0%} 超阈值,状态封顶 WEAK")
        status = StageStatus.WEAK
    if status is StageStatus.FAIL:
        headline = "品牌未稳定进入生成回答,暂无可分析的品牌描述"
        sentiment_part = ["情感分析:提及样本不足,暂不适用"] if n_sentiment == 0 else [sentiment_line]
    else:
        headline = {
            StageStatus.WEAK: "品牌在回答中不稳定地被提及",
            StageStatus.PASS: "品牌稳定进入回答且描述以中性/正面为主",
        }[status]
        sentiment_part = [sentiment_line]
    return StageFinding(
        stage=Stage.REPRESENTATION,
        status=status,
        headline=headline,
        evidence_lines=[
            f"提及率:{fmt_estimate(estimate)}",
            *sentiment_part,
            f"判定阈值:FAIL 上界≤{thresholds.mention_fail_ci_high:.0%},"
            f"PASS 下界≥{thresholds.mention_pass_ci_low:.0%},"
            f"负面占比 >{thresholds.negative_share_weak:.0%} 封顶 WEAK",
            caliber,
            _CLAIMS_NOTE,
            *notes,
        ],
        low_confidence=accounting.low_confidence,
    )
