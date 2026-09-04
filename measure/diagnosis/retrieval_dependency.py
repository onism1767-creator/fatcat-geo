"""检索依赖诊断(第 9 章挂靠):开卷 vs 闭卷提及率对比。

回答的问题:品牌在 AI 回答中的呈现,主要依赖实时检索,还是模型参数化知识?
注意口径:闭卷能答 ≠ 「AI 记住了品牌」(第 9 章原文),仅说明可见度不依赖
临场检索,结论码里如实注明。

规则(与 findings.interval_status 同家法:只认区间端点,不认点估计):
- 无闭卷腿(功能未启用 / 无支持 surface)→ 返回 None,报告不出此段;
- 任一腿无有效样本 → INCONCLUSIVE;
- 开卷提及本身处于缺席水平(区间上界 ≤ mention_fail_ci_high)→ INCONCLUSIVE,
  开卷都不提及,「依赖什么」无从谈起;
- 两区间重叠 → LOW(闭卷与开卷无法区分,检索不是可见度主要来源);
- 区间分离且闭卷上界 < 开卷下界 × dependency_high_ratio → HIGH;
- 其余分离情形 → MEDIUM。

两条区间必须并排展示,不做四象限硬判(第 9 章);依赖度不是漏斗阶段,
不参与失败定位。开卷与闭卷两腿必须来自同一 surface/model(跨仪器禁比,
调用方保证,本模块校验)。
"""

from __future__ import annotations

from enum import StrEnum

from diagnosis.findings import DiagnosisThresholds, caliber_msg
from diagnosis.messages import Msg, msg
from engine.models import FrozenModel
from engine.scheduler import NeedSurfaceMetric


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
    online: NeedSurfaceMetric | None,
    offline: NeedSurfaceMetric | None,
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
        ratio=thresholds.dependency_high_ratio,
        absent=thresholds.mention_fail_ci_high,
    )
    calibers = [
        msg("dependency.leg_caliber", leg=msg(f"dependency.leg.{leg}"), caliber=caliber_msg(metric))
        for leg, metric in (("online", online), ("offline", offline))
        if metric is not None
    ]

    on = online.estimate if online is not None else None
    off = offline.estimate
    if on is None or off is None:
        return DependencyFinding(
            level=DependencyLevel.INCONCLUSIVE,
            headline=msg("dependency.headline.inconclusive"),
            evidence_lines=[msg("dependency.note.missing_leg"), meaning, rule_line, *calibers],
        )

    pair = msg(
        "dependency.pair",
        on_point=on.point, on_low=on.ci_low, on_high=on.ci_high, on_n=on.n,
        off_point=off.point, off_low=off.ci_low, off_high=off.ci_high, off_n=off.n,
    )
    if on.ci_high <= thresholds.mention_fail_ci_high:
        return DependencyFinding(
            level=DependencyLevel.INCONCLUSIVE,
            headline=msg("dependency.headline.inconclusive"),
            evidence_lines=[msg("dependency.note.absent_online"), pair, meaning, rule_line, *calibers],
        )
    if off.ci_high >= on.ci_low:
        level, headline = DependencyLevel.LOW, msg("dependency.headline.low")
    elif off.ci_high < on.ci_low * thresholds.dependency_high_ratio:
        level, headline = DependencyLevel.HIGH, msg("dependency.headline.high")
    else:
        level, headline = DependencyLevel.MEDIUM, msg("dependency.headline.medium")
    return DependencyFinding(
        level=level, headline=headline, evidence_lines=[pair, meaning, rule_line, *calibers]
    )
