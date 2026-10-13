"""L4 用户视图汇总(v0.4.13 条目 8;v0.4.14 用户语言层):把 L3 分诊结果压成
「官网抓取情况 / 每家 AI 一句话 / 哪个题型弱 / 先做什么」。只做分组与去重,不做任何
新判定——每个数字、每句结论都来自 L3 已产出的 Msg、Recommendation 与 SurfaceVerdict;
本模块不含 surface 名字符串,也不碰阈值。AI 的显示名由适配器 meta 声明、报告路由传入。

两视图分工:用户视图(本模块喂,渲染用 plain 人话目录)回答品牌方的问题;检测依据与
方法(模板里默认折叠,渲染用技术目录)保留 N、逐题计数、stop_reason、阈值、判定器、
核验四态、证据 ID 的全部细节。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

from diagnosis.diagnose import SurfaceDiagnosis
from diagnosis.findings import MentionCompare, Msg, Stage, StageFinding, msg
from diagnosis.recommender import Recommendation
from diagnosis.verdict import SurfaceVerdict
from engine.models import FrozenModel, PromptIntent
from report.jobs import ClassReport

_STAGE_ORDER = (Stage.ACCESS, Stage.RETRIEVAL, Stage.CITATION, Stage.REPRESENTATION)

TargetNamer = Callable[[str, str], str]  # (仪器, 型号) → 被测对象显示名,规则在 report/app.py


class BottleneckGroup(FrozenModel):
    stage: Stage | None  # None = 各可观测阶段均无瓶颈信号
    surfaces: list[str]  # 显示名


class ClassBrief(FrozenModel):
    intent: PromptIntent
    groups: list[BottleneckGroup]  # 漏斗顺序,末尾为无瓶颈组


class EngineBrief(FrozenModel):
    """每家 AI 三行:看到了什么 / 依据 / 需要注意(全部引用 L3 既有输出)。
    建议不在此重复——「建议先做的事」已跨题型与 AI 去重并标支持来源。"""

    surface: str
    display: str
    model: str
    provider: str
    primary: Stage | None
    phenomenon: Msg
    strongest_evidence: Msg | None
    limitations: list[Msg]
    third_party: Msg | None = None  # 第三方线一句(引用步的结论;S6.1),与官网线并列、不合并
    third_party_evidence: Msg | None = None  # 第三方线的数字(「N 次里 k 次」),与官网的数字并排、不相加(用户定)
    mention_compare: MentionCompare | None = None  # 竞品提及对比(S7):主视图每个题型下一块


class PriorityAction(FrozenModel):
    recommendation: Recommendation
    supporters: list[tuple[PromptIntent, str]]  # (题型, AI 显示名)

    @property
    def support_count(self) -> int:
        return len(self.supporters)


class VerdictLine(FrozenModel):
    """每家 AI 一句话:结论码 + 显示名代入;缺项以说明句列出。"""

    surface: str
    display: str
    message: Msg
    missing: list[Msg]
    third_party: Msg | None = None  # 第三方线一句(S6.1)


class ReportSummary(FrozenModel):
    access: StageFinding | None
    verdicts: list[VerdictLine]
    classes: list[ClassBrief]
    actions: list[PriorityAction]
    engines: dict[str, list[EngineBrief]]  # 题型值 → 各家三行


def _engine_brief(sd: SurfaceDiagnosis, display: str) -> EngineBrief:
    primary = sd.primary_failure
    finding = next((f for f in sd.findings if f.stage is primary), None)
    return EngineBrief(
        surface=sd.surface,
        display=display,
        model=sd.model,
        provider=sd.provider,
        primary=primary,
        phenomenon=finding.headline if finding is not None else msg("caution.none"),
        strongest_evidence=(
            finding.evidence_lines[0] if finding is not None and finding.evidence_lines else None
        ),
        limitations=[c for c in sd.cautions if c.code != "caution.none"],
        third_party=next((f.headline for f in sd.third_party if f.stage is Stage.CITATION), None),
        third_party_evidence=next(
            (f.evidence_lines[0] for f in sd.third_party if f.stage is Stage.CITATION and f.evidence_lines), None,
        ),
        mention_compare=sd.mention_compare,
    )


def _class_brief(report: ClassReport, name_of: TargetNamer) -> ClassBrief:
    by_stage: dict[Stage | None, list[str]] = defaultdict(list)
    for sd in report.diagnosis.surfaces:
        by_stage[sd.primary_failure].append(name_of(sd.surface, sd.model))
    groups = [
        BottleneckGroup(stage=stage, surfaces=by_stage[stage])
        for stage in (*_STAGE_ORDER, None)
        if by_stage.get(stage)
    ]
    return ClassBrief(intent=report.intent, groups=groups)


def summarize(
    reports: list[ClassReport],
    verdicts: list[SurfaceVerdict],
    name_of: TargetNamer,
) -> ReportSummary:
    """跨题型、跨 AI 去重建议:同一证据条目只出现一次,记下被哪些(题型 × AI)诊断支持;
    排序 = 证据等级强者在前,同级按支持数多者在前。name_of(仪器, 型号) 给被测对象的显示名。"""
    seen: dict[str, Recommendation] = {}
    supporters: dict[str, list[tuple[PromptIntent, str]]] = defaultdict(list)
    engines: dict[str, list[EngineBrief]] = {}
    for report in reports:
        engines[report.intent.value] = [
            _engine_brief(sd, name_of(sd.surface, sd.model))
            for sd in report.diagnosis.surfaces
        ]
        for sd in report.diagnosis.surfaces:
            for rec in [*sd.recommendations, *sd.third_party_recommendations]:
                seen.setdefault(rec.evidence_id, rec)
                supporters[rec.evidence_id].append((report.intent, name_of(sd.surface, sd.model)))
    actions = sorted(
        (PriorityAction(recommendation=rec, supporters=supporters[eid]) for eid, rec in seen.items()),
        key=lambda a: (a.recommendation.evidence_level, -a.support_count, a.recommendation.evidence_id),
    )
    return ReportSummary(
        access=reports[0].diagnosis.access if reports else None,
        verdicts=[
            VerdictLine(
                surface=v.surface,
                display=name_of(v.surface, v.model),
                message=msg(v.code, ai=name_of(v.surface, v.model)),
                missing=[msg(code) for code in v.missing],
                third_party=msg(v.third_party_code, ai=name_of(v.surface, v.model)) if v.third_party_code else None,
            )
            for v in verdicts
        ],
        classes=[_class_brief(r, name_of) for r in reports],
        actions=actions,
        engines=engines,
    )
