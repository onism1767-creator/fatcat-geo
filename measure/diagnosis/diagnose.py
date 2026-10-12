"""单题型诊断编排:按 AI 分家(v0.4.11)——每台仪器一套漏斗、一次定位、一份药方(v0.4.26 S2 起按题型聚合)。

分家理由(实测实证):各家 AI 的搜索习惯完全不同(有的成批定向品牌点名
查询,有的少量宽品类查询——实据见交接文档冒烟记录),同样的缺席病灶分布
不同,混在一个漏斗里 = 把两种病写成一张方子。研究单位本就是
Provider × Surface(铁律),数据层从第一天分记,本次把报告层补齐。

结构:ClassDiagnosis = 共享的 ACCESS 结论(全站一次,门槛对所有 AI 生效)
+ 每家 surface 一个 SurfaceDiagnosis(RETRIEVAL/CITATION/REPRESENTATION
三阶段 + 各自定位 + 各自建议 + 各自开卷/闭卷对照)。

定位规则(5.2 漏斗顺序,逐家独立执行):
- ACCESS FAIL → 定位 ACCESS,下游照常观测但建议只针对 ACCESS;
- 否则按 RETRIEVAL → CITATION → REPRESENTATION 找第一个 FAIL;
- 无 FAIL 则找第一个 WEAK;全 PASS → 不定位(无需药方);
- NOT_OBSERVABLE 阶段跳过定位并在警示中注明;
- 通道分歧防线(v0.4.4):引用 PASS 时检索 FAIL/WEAK 不定位,只作警示。

上游 FAIL 禁下游药方:recommend() 只会收到 primary_failure 这一个阶段。
"""

from __future__ import annotations

from diagnosis.access_audit import AccessAuditResult
from diagnosis.access_audit import CheckStatus as AccessCheckStatus
from diagnosis.findings import Msg, Stage, StageFinding, StageStatus, msg
from diagnosis.recommender import EvidenceEntry, Recommendation, recommend
from diagnosis.retrieval_dependency import DependencyFinding
from engine.models import FrozenModel, PromptIntent

_FUNNEL = (Stage.RETRIEVAL, Stage.CITATION, Stage.REPRESENTATION)


class SurfaceDiagnosis(FrozenModel):
    """一家 surface(一台 AI 仪器)在该题型上的完整分诊。
    provider/surface/model 为运行时口径数据,非源码硬编码(13.4 说明)。"""

    provider: str
    surface: str
    model: str
    findings: list[StageFinding]  # RETRIEVAL/CITATION/REPRESENTATION 顺序(ACCESS 共享在需求层)
    primary_failure: Stage | None
    cautions: list[Msg]
    recommendations: list[Recommendation]
    retrieval_dependency: DependencyFinding | None = None  # 第 9 章挂靠,不参与定位


class ClassDiagnosis(FrozenModel):
    """一个题型(产品文档 15.2 的五类之一)上全部被测对象的分诊。"""

    intent: PromptIntent
    access: StageFinding  # 全站一次,门槛对所有 AI 生效
    surfaces: list[SurfaceDiagnosis]


def access_finding(audit: AccessAuditResult | None) -> StageFinding:
    """把 ACCESS 审计结果翻译为阶段结论。UNKNOWN 记 NOT_OBSERVABLE 并注明。
    (检查明细由报告层直接渲染 audit 对象,此处不重复。)"""
    if audit is None:
        return StageFinding(
            stage=Stage.ACCESS,
            status=StageStatus.NOT_OBSERVABLE,
            headline=msg("access.headline.missing"),
        )
    status = {
        AccessCheckStatus.PASS: StageStatus.PASS,
        AccessCheckStatus.FAIL: StageStatus.FAIL,
        AccessCheckStatus.UNKNOWN: StageStatus.NOT_OBSERVABLE,
    }[audit.overall]
    headline_code = {
        StageStatus.PASS: "access.headline.pass",
        StageStatus.FAIL: "access.headline.fail",
        StageStatus.NOT_OBSERVABLE: "access.headline.unknown",
    }[status]
    notes = [msg("access.unknown_note")] if audit.overall is AccessCheckStatus.UNKNOWN else []
    return StageFinding(
        stage=Stage.ACCESS, status=status, headline=msg(headline_code), evidence_lines=notes
    )


def localize_failure(
    findings: dict[Stage, StageFinding], *, skip: frozenset[Stage] = frozenset()
) -> Stage | None:
    if findings[Stage.ACCESS].status is StageStatus.FAIL:
        return Stage.ACCESS
    for stage in _FUNNEL:
        if stage not in skip and findings[stage].status is StageStatus.FAIL:
            return stage
    for stage in _FUNNEL:
        if stage not in skip and findings[stage].status is StageStatus.WEAK:
            return stage
    return None


def diagnose_surface(
    *,
    provider: str,
    surface: str,
    model: str,
    access: StageFinding,
    retrieval: StageFinding,
    citation: StageFinding,
    representation: StageFinding,
    intent: PromptIntent,
    registry: list[EvidenceEntry],
    retrieval_dependency: DependencyFinding | None = None,
) -> SurfaceDiagnosis:
    findings = {
        Stage.ACCESS: access,
        Stage.RETRIEVAL: retrieval,
        Stage.CITATION: citation,
        Stage.REPRESENTATION: representation,
    }
    cautions: list[Msg] = []
    skip: frozenset[Stage] = frozenset()
    # 通道分歧防线:被引必先被检索。引用 PASS 时检索通道的缺席只作警示,
    # 不得据此定位失败(风险 #5:观测通道不当真值;同仪器生成端通道理论上
    # 不会出现此矛盾,出现即更该警示)。
    if (
        findings[Stage.RETRIEVAL].status in (StageStatus.FAIL, StageStatus.WEAK)
        and findings[Stage.CITATION].status is StageStatus.PASS
    ):
        skip = frozenset({Stage.RETRIEVAL})
        cautions.append(msg("caution.divergence"))
    primary = localize_failure(findings, skip=skip)
    unobservable = [s for s in findings if findings[s].status is StageStatus.NOT_OBSERVABLE]
    if unobservable:
        cautions.append(msg(
            "caution.unobservable", stages=[msg(f"stage.{s.value}") for s in unobservable]
        ))
    if primary in (Stage.ACCESS, Stage.RETRIEVAL):
        cautions.append(msg("caution.no_rewrite"))
    if primary is None:
        cautions.append(msg("caution.none"))

    recommendations = (
        recommend(registry, stage=primary, intent=intent) if primary is not None else []
    )
    return SurfaceDiagnosis(
        provider=provider,
        surface=surface,
        model=model,
        findings=[retrieval, citation, representation],
        primary_failure=primary,
        cautions=cautions,
        recommendations=recommendations,
        retrieval_dependency=retrieval_dependency,
    )


def diagnose_class(
    intent: PromptIntent,
    *,
    access: StageFinding,
    surfaces: list[SurfaceDiagnosis],
) -> ClassDiagnosis:
    return ClassDiagnosis(intent=intent, access=access, surfaces=surfaces)
