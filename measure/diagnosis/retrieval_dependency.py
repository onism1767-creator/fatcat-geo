"""检索依赖诊断(第 9 章挂靠):开卷 vs 闭卷提及比例对比。

回答的问题:品牌在 AI 回答中的呈现,主要依赖实时检索,还是模型参数化知识?
注意口径:闭卷能答 ≠ 「AI 记住了品牌」(第 9 章原文),仅说明可见度不依赖
临场检索,结论码里如实注明。

规则(与 findings.rate_status 同家法:直接看比例,v0.4.26 S2 用户定 T1):
- 无闭卷腿(功能未启用 / 无支持 surface)→ 返回 None,报告不出此段;
- 任一腿无有效样本 → INCONCLUSIVE;
- 开卷提及本身处于缺席水平(比例 < mention_fail_below)→ INCONCLUSIVE,
  开卷都不提及,「依赖什么」无从谈起;
- 开卷与闭卷相差不足 rate_gap → LOW(检索不是可见度主要来源);
- 闭卷比例 < 开卷比例 × dependency_high_ratio → HIGH;
- 其余 → MEDIUM。

两个比例必须并排展示,不做四象限硬判(第 9 章);依赖度不是漏斗阶段,
不参与失败定位。开卷与闭卷两腿必须来自同一 surface/model(跨仪器禁比,
调用方保证,本模块校验)。
"""

from __future__ import annotations

from enum import StrEnum

from diagnosis.findings import DiagnosisThresholds, caliber_msg
from diagnosis.messages import Msg, msg
from engine.aggregate import ClassSurfaceMetric
from engine.models import FrozenModel


class DependencyLevel(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INCONCLUSIVE = "INCONCLUSIVE"


class DependencyFinding(FrozenModel):
    level: DependencyLevel
    headline: Msg
    evidence_lines: list[Msg] = []


def diagnose_retrieval_dependency(
    online: ClassSurfaceMetric | None,
    offline: ClassSurfaceMetric | None,
    thresholds: DiagnosisThresholds | None = None,
) -> DependencyFinding | None:
    if offline is None:
        return None
    thresholds = thresholds or DiagnosisThresholds()
    if online is not None and (online.surface, online.model) != (offline.surface, offline.model):
        raise ValueError(
            f"开卷/闭卷必须同 surface/model(跨仪器禁比):"
            f"{(online.surface, online.model)} vs {(offline.surface, offline.model)}"
        )

    meaning = msg("dependency.note.meaning")
    rule_line = msg(
        "dependency.thresholds",
        pts=round(thresholds.rate_gap * 100),  # 差距按「个百分点」说(与别处统一)
        ratio=thresholds.dependency_high_ratio,
        absent=thresholds.mention_fail_below,
    )
    calibers = [
        msg("dependency.leg_caliber", leg=msg(f"dependency.leg.{leg}"), caliber=caliber_msg(metric))
        for leg, metric in (("online", online), ("offline", offline))
        if metric is not None
    ]

    on = online.rate if online is not None else None
    off = offline.rate
    if on is None or off is None:
        return DependencyFinding(
            level=DependencyLevel.INCONCLUSIVE,
            headline=msg("dependency.headline.inconclusive"),
            evidence_lines=[msg("dependency.note.missing_leg"), meaning, rule_line, *calibers],
        )

    pair = msg(
        "dependency.pair",
        on_k=on.k, on_n=on.n, on_share=on.share,
        off_k=off.k, off_n=off.n, off_share=off.share,
    )
    diff = round(on.share - off.share, 9)  # 先四舍五入,免得 0.19999… 把正好差 20 个百分点判成分不出(同 compare_rates)
    if on.share < thresholds.mention_fail_below and -diff >= thresholds.rate_gap:
        # 联网几乎不提、不联网却常提:这是最显眼的一种反差,不能说成「看不出来」(第二轮)
        return DependencyFinding(
            level=DependencyLevel.LOW, headline=msg("dependency.headline.offline_higher"),
            evidence_lines=[pair, meaning, rule_line, *calibers],
        )
    if on.share < thresholds.mention_fail_below:
        return DependencyFinding(
            level=DependencyLevel.INCONCLUSIVE,
            headline=msg("dependency.headline.inconclusive"),
            evidence_lines=[msg("dependency.note.absent_online"), pair, meaning, rule_line, *calibers],
        )
    if abs(diff) < thresholds.rate_gap:
        level, headline = DependencyLevel.LOW, msg("dependency.headline.low")
    elif diff < 0:  # 闭卷比开卷还高:不是「接近、未观测到差异」,要照实说(审核 2026-10-27 黄 15)
        level, headline = DependencyLevel.LOW, msg("dependency.headline.offline_higher")
    elif off.share < on.share * thresholds.dependency_high_ratio:
        level, headline = DependencyLevel.HIGH, msg("dependency.headline.high")
    else:
        level, headline = DependencyLevel.MEDIUM, msg("dependency.headline.medium")
    return DependencyFinding(
        level=level, headline=headline, evidence_lines=[pair, meaning, rule_line, *calibers]
    )
