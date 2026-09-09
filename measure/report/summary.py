"""L4 决策视图汇总(v0.4.13 条目 8):把 L3 分诊结果压成「哪里有问题 / 哪几家 AI /
先做什么」。只做分组与去重,不做任何新判定——每个数字、每句结论都来自 L3 已产出的
Msg 与 Recommendation,本模块不含 surface 名字符串,也不碰阈值。

两视图分工:决策视图(本模块喂)回答三个问题;审计视图(模板里默认折叠)保留 N、
可信区间、stop_reason、阈值、判定器、核验四态、证据 ID 的全部细节。
"""

from __future__ import annotations

from collections import defaultdict

from diagnosis.diagnose import SurfaceDiagnosis
from diagnosis.findings import Msg, Stage, StageFinding, msg
from diagnosis.recommender import Recommendation
from engine.models import FrozenModel, NeedIntent
from report.jobs import NeedReport

_STAGE_ORDER = (Stage.ACCESS, Stage.RETRIEVAL, Stage.CITATION, Stage.REPRESENTATION)


class BottleneckGroup(FrozenModel):
    stage: Stage | None  # None = 各可观测阶段均无瓶颈信号
    surfaces: list[str]


class NeedBrief(FrozenModel):
    need_id: str
    statement: str
    intent: NeedIntent
    groups: list[BottleneckGroup]  # 漏斗顺序,末尾为无瓶颈组


class EngineBrief(FrozenModel):
    """每家 AI 四行:现象 / 最强证据 / 局限 / 推荐实验(全部引用 L3 既有输出)。"""

    surface: str
    model: str
    provider: str
    primary: Stage | None
    phenomenon: Msg
    strongest_evidence: Msg | None
    limitations: list[Msg]
    experiments: list[str]  # 证据条目 ID


class PriorityAction(FrozenModel):
    recommendation: Recommendation
    supporters: list[tuple[str, str]]  # (需求原句, surface)

    @property
    def support_count(self) -> int:
        return len(self.supporters)


class ReportSummary(FrozenModel):
    access: StageFinding | None
    needs: list[NeedBrief]
    actions: list[PriorityAction]
    engines: dict[str, list[EngineBrief]]  # need_id → 各家四行


def _engine_brief(sd: SurfaceDiagnosis) -> EngineBrief:
    primary = sd.primary_failure
    finding = next((f for f in sd.findings if f.stage is primary), None)
    return EngineBrief(
        surface=sd.surface,
        model=sd.model,
        provider=sd.provider,
        primary=primary,
        phenomenon=finding.headline if finding is not None else msg("caution.none"),
        strongest_evidence=(
            finding.evidence_lines[0] if finding is not None and finding.evidence_lines else None
        ),
        limitations=[c for c in sd.cautions if c.code != "caution.none"],
        experiments=[r.evidence_id for r in sd.recommendations],
    )


def _need_brief(report: NeedReport) -> NeedBrief:
    by_stage: dict[Stage | None, list[str]] = defaultdict(list)
    for sd in report.diagnosis.surfaces:
        by_stage[sd.primary_failure].append(sd.surface)
    groups = [
        BottleneckGroup(stage=stage, surfaces=by_stage[stage])
        for stage in (*_STAGE_ORDER, None)
        if by_stage.get(stage)
    ]
    return NeedBrief(
        need_id=report.need.need_id,
        statement=report.need.statement,
        intent=report.need.intent,
        groups=groups,
    )


def summarize(reports: list[NeedReport]) -> ReportSummary:
    """跨需求、跨 AI 去重建议:同一证据条目只出现一次,记下被哪些(需求 × AI)诊断支持;
    排序 = 证据等级强者在前,同级按支持数多者在前。"""
    seen: dict[str, Recommendation] = {}
    supporters: dict[str, list[tuple[str, str]]] = defaultdict(list)
    engines: dict[str, list[EngineBrief]] = {}
    for report in reports:
        engines[report.need.need_id] = [_engine_brief(sd) for sd in report.diagnosis.surfaces]
        for sd in report.diagnosis.surfaces:
            for rec in sd.recommendations:
                seen.setdefault(rec.evidence_id, rec)
                supporters[rec.evidence_id].append((report.need.statement, sd.surface))
    actions = sorted(
        (PriorityAction(recommendation=rec, supporters=supporters[eid]) for eid, rec in seen.items()),
        key=lambda a: (a.recommendation.evidence_level, -a.support_count, a.recommendation.evidence_id),
    )
    return ReportSummary(
        access=reports[0].diagnosis.access if reports else None,
        needs=[_need_brief(r) for r in reports],
        actions=actions,
        engines=engines,
    )
