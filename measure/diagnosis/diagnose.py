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

上游 FAIL 禁下游药方:recommend() 每条线只收这条线的 primary_failure(官网线、第三方线各一个);
另外官网审计的提示项(如网站地图没找到)对应的 ACCESS 条目不管卡在哪一步都出——它在最上游,不违反这条。
"""

from __future__ import annotations

from diagnosis.access_audit import AccessAuditResult
from diagnosis.access_audit import CheckStatus as AccessCheckStatus
from diagnosis.findings import MentionCompare, Msg, SentimentTally, Stage, StageFinding, StageStatus, msg
from diagnosis.recommender import THIRD_PARTY_CITED_ELSEWHERE, EvidenceEntry, Recommendation, recommend
from diagnosis.retrieval_dependency import DependencyFinding
from diagnosis.third_party_diag import localize_third_party
from engine.aggregate import CitedElsewhere
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
    # 第三方线(S6.1,v0.4.24 D2 / D3):搜到 → 引用写到你的第三方网页;各自亮灯、各自定位、各自建议,不并入官网线
    third_party: list[StageFinding] = []
    third_party_failure: Stage | None = None
    third_party_cautions: list[Msg] = []
    third_party_recommendations: list[Recommendation] = []
    mention_compare: MentionCompare | None = None  # 竞品提及对比(S7):主视图一块;没有竞品 → None
    sentiment: SentimentTally | None = None  # 提到你的句子按好坏计数(S8.1):主视图一行 + 饼图;一句没有 → None
    # AI 引用了、但没写到你的第三方网页(第三方线证据区的名单;实验卡 C1 只在它非空时出);一个都没有 → None
    third_party_cited_elsewhere: CitedElsewhere | None = None


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
        stage=Stage.ACCESS, status=status, headline=msg(headline_code), evidence_lines=notes,
        failed_checks=[c.check for c in audit.checks if c.gating and c.status is AccessCheckStatus.FAIL],
        hint_checks=[c.check for c in audit.checks if not c.gating and c.status is AccessCheckStatus.FAIL],
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
    third_party: list[StageFinding] | None = None,
    mention_compare: MentionCompare | None = None,
    sentiment: SentimentTally | None = None,
    third_party_cited_elsewhere: CitedElsewhere | None = None,
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

    # 本次成立的数据条件:有的条目要证据区真有东西才出(C1 要名单非空;用户 2026-10-28 定)
    data = frozenset({THIRD_PARTY_CITED_ELSEWHERE}) if third_party_cited_elsewhere is not None else frozenset()
    recommendations = (  # ACCESS 的建议只针对挂了的检查项(审核 2026-10-27 黄 14);没记检查项就不过滤
        recommend(registry, stage=primary, intent=intent,
                  access_checks=frozenset(access.failed_checks) if access.failed_checks else None,
                  data_available=data)
        if primary is not None else []
    )
    # 提示项没过(网站地图没找到):对应的实验卡不管卡在哪一步都出——它在最上游,不违反「上游 FAIL 禁下游药方」
    if access.hint_checks:
        have = {r.evidence_id for r in recommendations}
        recommendations += [r for r in recommend(registry, stage=Stage.ACCESS, intent=intent,
                                                 access_checks=frozenset(access.hint_checks), data_available=data)
                            if r.evidence_id not in have]
    # 第三方线单独定位:同一套「第一个 FAIL,否则第一个 WEAK」,同样有通道分歧防线;建议只从第三方线的条目里取
    tp_findings = list(third_party or [])
    tp_cautions: list[Msg] = []
    tp_skip: frozenset[Stage] = frozenset()
    tp_by_stage = {f.stage: f for f in tp_findings}
    if (
        Stage.RETRIEVAL in tp_by_stage and Stage.CITATION in tp_by_stage
        and tp_by_stage[Stage.RETRIEVAL].status in (StageStatus.FAIL, StageStatus.WEAK)
        and tp_by_stage[Stage.CITATION].status is StageStatus.PASS
    ):
        tp_skip = frozenset({Stage.RETRIEVAL})
        tp_cautions.append(msg("caution.thirdparty.divergence"))
    tp_primary = localize_third_party(tp_findings, skip=tp_skip)
    return SurfaceDiagnosis(
        provider=provider,
        surface=surface,
        model=model,
        findings=[retrieval, citation, representation],
        primary_failure=primary,
        cautions=cautions,
        recommendations=recommendations,
        retrieval_dependency=retrieval_dependency,
        third_party=tp_findings,
        third_party_failure=tp_primary,
        third_party_cautions=tp_cautions,
        third_party_recommendations=(
            recommend(registry, stage=tp_primary, intent=intent, channel="third_party", data_available=data)
            if tp_primary is not None else []
        ),
        mention_compare=mention_compare,
        sentiment=sentiment,
        third_party_cited_elsewhere=third_party_cited_elsewhere,
    )


def diagnose_class(
    intent: PromptIntent,
    *,
    access: StageFinding,
    surfaces: list[SurfaceDiagnosis],
) -> ClassDiagnosis:
    return ClassDiagnosis(intent=intent, access=access, surfaces=surfaces)
