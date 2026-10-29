"""L4 用户视图汇总(v0.4.13 条目 8;v0.4.14 用户语言层;v0.4.26 S8.7 改版):把 L3 分诊结果压成
「官网抓取情况 / 各 AI 一句话(每个型号的百分比)/ 四步 → 题型 → AI(型号数数)→ 型号 / 末尾一段汇总 / 先做什么」。

只做分组、计数与去重,不做任何新判定——每个数字、每句结论都来自 L3 已产出的 StageFinding(含 rate)、Msg、Recommendation;
本模块不含 surface 名字符串;汇总段落分「经常 / 偶尔 / 基本不」只借 L3 的 DiagnosisThresholds,不另定线。
AI 的显示名(「接口 · 型号」与接口名)由适配器 meta 声明、报告路由传入。成句在 report/narrative.py。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable

from diagnosis.diagnose import SurfaceDiagnosis
from diagnosis.findings import (
    DiagnosisThresholds,
    MentionCompare,
    Msg,
    SentimentTally,
    Stage,
    StageFinding,
    StageStatus,
    msg,
)
from diagnosis.recommender import Recommendation
from engine.metrics import Rate
from engine.models import FrozenModel, PromptIntent
from report.jobs import ClassReport

TargetNamer = Callable[[str, str], str]  # (仪器, 型号) → 「接口 · 型号」,规则在 report/web.py
ApiNamer = Callable[[str], str]  # 仪器 → 接口名

_STEPS = (Stage.RETRIEVAL, Stage.CITATION, Stage.REPRESENTATION)
_RATE_CODES = {Stage.RETRIEVAL: "genret.presence", Stage.CITATION: "citation.rate",
               Stage.REPRESENTATION: "repr.mention_rate"}
_THIRD_RATE_CODES = {Stage.RETRIEVAL: "thirdparty.retrieved.rate", Stage.CITATION: "thirdparty.cited.rate"}


class PriorityAction(FrozenModel):
    recommendation: Recommendation
    supporters: list[tuple[PromptIntent, str]]  # (题型, AI 显示名)

    @property
    def support_count(self) -> int:
        return len(self.supporters)


class StepCell(FrozenModel):
    """某一步里一个型号的一格:灯 + 一句人话(有比例时「本次 N 次里 k 次(p%)…」,看不到时该步的结论句)。"""

    status: StageStatus
    text: Msg


class ModelCells(FrozenModel):
    surface: str
    model: str
    own: StepCell  # 官网线
    third: StepCell | None = None  # 第三方线(第四步没有)


class ApiBlock(FrozenModel):
    """一家 AI 在某题型某一步:先一行型号数数,再每个型号一行。"""

    surface: str
    api: str
    status_counts: dict[str, int]  # 官网线那一格的灯按状态计数(型号数数,不算平均)
    models: list[ModelCells]


class IntentStep(FrozenModel):
    intent: PromptIntent
    n_prompts: int
    apis: list[ApiBlock]
    compares: list[tuple[str, MentionCompare]] = []  # 第四步:竞品提及对比(显示名, 对比)
    sentiments: list[tuple[str, SentimentTally]] = []  # 第四步:AI 怎么评价你
    has_quotes: bool = False


class StepView(FrozenModel):
    stage: Stage
    intents: list[IntentStep]


class ApiLine(FrozenModel):
    """各 AI 一句话(写法 1,用户定 2026-10-14):用户不提你时每个型号提到你的比例(四类题合计);
    用户直接问起时能不能介绍你。"""

    surface: str
    api: str
    unprompted: list[tuple[str, Rate]]  # (型号, 不点名的四类题合计 k/n);没测 → 空
    # (型号, 品牌事实题里提及比例达到「经常」线;None = 失败 / 拒答太多、说不准);没测 → 空
    prompted: list[tuple[str, bool | None]]


class BestCell(FrozenModel):
    display: str
    intent: PromptIntent
    rate: Rate


class WrapUp(FrozenModel):
    """末尾一段汇总的原料:只是把上面的数字挑出来,不做新判断。"""

    n_apis: int
    n_models: int
    n_prompts: int
    prompted_ok: int | None  # 直接问起时能介绍你的型号数;None = 没测或全都说不准
    prompted_total: int  # 说得准的型号数
    prompted_unsure: int  # 失败 / 拒答太多、说不准的型号数
    unprompted: dict[str, int] | None  # often / sometimes / rarely 各几个型号;None = 没测
    best_unprompted: tuple[str, Rate] | None
    best_own_citation: BestCell | None
    best_third_citation: BestCell | None
    access: StageStatus | None


class ReportSummary(FrozenModel):
    access: StageFinding | None
    api_lines: list[ApiLine]
    steps: list[StepView]  # 第二 / 三 / 四步
    wrap_up: WrapUp | None
    actions: list[PriorityAction]


def _finding(sd: SurfaceDiagnosis, stage: Stage) -> StageFinding:
    return next(f for f in sd.findings if f.stage is stage)


def _third(sd: SurfaceDiagnosis, stage: Stage) -> StageFinding | None:
    return next((f for f in sd.third_party if f.stage is stage), None)


def _cell(finding: StageFinding, code: str, *, headline_only: bool = False) -> StepCell:
    if finding.rate is not None and not headline_only:
        r = finding.rate
        return StepCell(status=finding.status, text=msg(code, k=r.k, n=r.n, share=r.share))
    return StepCell(status=finding.status, text=finding.headline)


def _step_view(stage: Stage, reports: list[ClassReport], name_of: TargetNamer, api_name_of: ApiNamer) -> StepView:
    intents: list[IntentStep] = []
    for report in reports:
        by_surface: dict[str, list[SurfaceDiagnosis]] = defaultdict(list)
        for sd in report.diagnosis.surfaces:
            by_surface[sd.surface].append(sd)
        apis: list[ApiBlock] = []
        for surface, sds in by_surface.items():
            models: list[ModelCells] = []
            for sd in sds:
                # 品牌事实题的第四步:提及天然≈100%,灯由评价决定,格子里给结论句而不是提及比例
                headline_only = stage is Stage.REPRESENTATION and report.intent is PromptIntent.BRAND_FACT
                own = _cell(_finding(sd, stage), _RATE_CODES[stage], headline_only=headline_only)
                third = None
                if stage in _THIRD_RATE_CODES:
                    tf = _third(sd, stage)
                    third = _cell(tf, _THIRD_RATE_CODES[stage]) if tf is not None else None
                models.append(ModelCells(surface=sd.surface, model=sd.model, own=own, third=third))
            apis.append(ApiBlock(surface=surface, api=api_name_of(surface),
                                 status_counts=dict(Counter(m.own.status.value for m in models)), models=models))
        step = IntentStep(intent=report.intent, n_prompts=len(report.prompt_texts), apis=apis)
        if stage is Stage.REPRESENTATION:
            step = step.model_copy(update={
                "compares": [(name_of(sd.surface, sd.model), sd.mention_compare)
                             for sd in report.diagnosis.surfaces if sd.mention_compare is not None],
                "sentiments": [(name_of(sd.surface, sd.model), sd.sentiment)
                               for sd in report.diagnosis.surfaces if sd.sentiment is not None],
                "has_quotes": bool(report.quotes),
            })
        intents.append(step)
    return StepView(stage=stage, intents=intents)


def _api_lines(reports: list[ClassReport], api_name_of: ApiNamer) -> list[ApiLine]:
    pass_from = DiagnosisThresholds().mention_pass_from
    pooled: dict[tuple[str, str], list[int]] = {}  # (仪器, 型号) → [k, n],不点名的四类题合计(计数相加,不是平均)
    prompted: dict[tuple[str, str], bool | None] = {}
    order: list[tuple[str, str]] = []
    for report in reports:
        for sd in report.diagnosis.surfaces:
            key = (sd.surface, sd.model)
            if key not in order:
                order.append(key)
            f = _finding(sd, Stage.REPRESENTATION)
            if f.rate is None:
                continue
            if report.intent is PromptIntent.BRAND_FACT:
                prompted[key] = None if f.low_confidence else f.rate.share >= pass_from  # 说不准不下结论(第二轮)
            else:
                acc = pooled.setdefault(key, [0, 0])
                acc[0] += f.rate.k
                acc[1] += f.rate.n
    lines: list[ApiLine] = []
    for surface in dict.fromkeys(s for s, _ in order):
        models = [m for s, m in order if s == surface]
        lines.append(ApiLine(
            surface=surface, api=api_name_of(surface),
            unprompted=[(m, Rate(k=pooled[(surface, m)][0], n=pooled[(surface, m)][1]))
                        for m in models if (surface, m) in pooled],
            prompted=[(m, prompted[(surface, m)]) for m in models if (surface, m) in prompted],
        ))
    return lines


def _wrap_up(reports: list[ClassReport], lines: list[ApiLine], name_of: TargetNamer) -> WrapUp | None:
    if not reports:
        return None
    th = DiagnosisThresholds()
    models = {(sd.surface, sd.model) for r in reports for sd in r.diagnosis.surfaces}
    prompted = [ok for line in lines for _, ok in line.prompted]
    unprompted = [(f"{line.api} · {m}", r) for line in lines for m, r in line.unprompted]
    buckets: dict[str, int] | None = None
    best: tuple[str, Rate] | None = None
    if unprompted:
        buckets = {"often": 0, "sometimes": 0, "rarely": 0}
        for _, r in unprompted:
            often, sometimes = r.share >= th.mention_pass_from, r.share >= th.mention_fail_below
            key = "often" if often else "sometimes" if sometimes else "rarely"
            buckets[key] += 1
        best = max(unprompted, key=lambda item: (item[1].share, item[1].k))

    def best_of(pick: Callable[[SurfaceDiagnosis], StageFinding | None]) -> BestCell | None:
        cells = [
            BestCell(display=name_of(sd.surface, sd.model), intent=r.intent, rate=f.rate)
            for r in reports for sd in r.diagnosis.surfaces
            if (f := pick(sd)) is not None and f.rate is not None
        ]
        return max(cells, key=lambda c: (c.rate.share, c.rate.k)) if cells else None

    return WrapUp(
        n_apis=len({s for s, _ in models}), n_models=len(models),
        n_prompts=sum(len(r.prompt_texts) for r in reports),
        prompted_ok=sum(1 for ok in prompted if ok) if any(ok is not None for ok in prompted) else None,
        prompted_total=sum(1 for ok in prompted if ok is not None),
        prompted_unsure=sum(1 for ok in prompted if ok is None),
        unprompted=buckets, best_unprompted=best,
        best_own_citation=best_of(lambda sd: _finding(sd, Stage.CITATION)),
        best_third_citation=best_of(lambda sd: _third(sd, Stage.CITATION)),
        access=reports[0].diagnosis.access.status,
    )


def summarize(reports: list[ClassReport], name_of: TargetNamer, api_name_of: ApiNamer) -> ReportSummary:
    """跨题型、跨 AI 去重建议:同一证据条目只出现一次,记下被哪些(题型 × AI)诊断支持;
    排序 = 证据等级强者在前,同级按支持数多者在前。"""
    seen: dict[str, Recommendation] = {}
    supporters: dict[str, list[tuple[PromptIntent, str]]] = defaultdict(list)
    for report in reports:
        for sd in report.diagnosis.surfaces:
            for rec in [*sd.recommendations, *sd.third_party_recommendations]:
                seen.setdefault(rec.evidence_id, rec)
                supporters[rec.evidence_id].append((report.intent, name_of(sd.surface, sd.model)))
    actions = sorted(
        (PriorityAction(recommendation=rec, supporters=supporters[eid]) for eid, rec in seen.items()),
        key=lambda a: (a.recommendation.evidence_level, -a.support_count, a.recommendation.evidence_id),
    )
    lines = _api_lines(reports, api_name_of)
    return ReportSummary(
        access=reports[0].diagnosis.access if reports else None,
        api_lines=lines,
        steps=[_step_view(stage, reports, name_of, api_name_of) for stage in _STEPS],
        wrap_up=_wrap_up(reports, lines, name_of),
        actions=actions,
    )
