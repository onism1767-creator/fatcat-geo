"""单需求诊断编排:四阶段结论 → 失败阶段定位 → 建议(铁律 9 的执行处)。

定位规则(5.2 漏斗顺序):
- ACCESS FAIL → 定位 ACCESS,下游照常观测但建议只针对 ACCESS;
- 否则按 RETRIEVAL → CITATION → REPRESENTATION 找第一个 FAIL;
- 无 FAIL 则找第一个 WEAK;全 PASS → 不定位(无需药方);
- NOT_OBSERVABLE 阶段跳过定位并在警示中注明。

上游 FAIL 禁下游药方:recommend() 只会收到 primary_failure 这一个阶段。
定位在 ACCESS/RETRIEVAL 时,按第 1 章样例显式输出"不建议全页改写"警示。
"""

from __future__ import annotations

from diagnosis.access_audit import AccessAuditResult
from diagnosis.access_audit import CheckStatus as AccessCheckStatus
from diagnosis.findings import Stage, StageFinding, StageStatus
from diagnosis.recommender import EvidenceEntry, Recommendation, recommend
from engine.models import FrozenModel, InformationNeed, NeedIntent

_FUNNEL = (Stage.RETRIEVAL, Stage.CITATION, Stage.REPRESENTATION)


class NeedDiagnosis(FrozenModel):
    need_id: str
    statement: str
    intent: NeedIntent
    findings: list[StageFinding]  # 按 ACCESS/RETRIEVAL/CITATION/REPRESENTATION 顺序
    primary_failure: Stage | None
    cautions: list[str]
    recommendations: list[Recommendation]


def access_finding(audit: AccessAuditResult | None) -> StageFinding:
    """把 ACCESS 审计结果翻译为阶段结论。UNKNOWN 记 NOT_OBSERVABLE 并注明。"""
    if audit is None:
        return StageFinding(
            stage=Stage.ACCESS,
            status=StageStatus.NOT_OBSERVABLE,
            headline="未执行 ACCESS 审计",
        )
    status = {
        AccessCheckStatus.PASS: StageStatus.PASS,
        AccessCheckStatus.FAIL: StageStatus.FAIL,
        AccessCheckStatus.UNKNOWN: StageStatus.NOT_OBSERVABLE,
    }[audit.overall]
    gating_lines = [
        f"[{check.status}] {check.check} — {check.detail}"
        for check in audit.checks
        if check.gating
    ]
    if audit.overall is AccessCheckStatus.UNKNOWN:
        gating_lines.append("存在无法判定的检查项,资格门记为不可观测,需站长侧核验")
    headline = {
        StageStatus.PASS: "站点对 AI 爬虫可达,资格门通过",
        StageStatus.FAIL: "站点存在准入阻断,AI 爬虫无法正常获取内容",
        StageStatus.NOT_OBSERVABLE: "资格门存在无法判定项",
    }[status]
    return StageFinding(
        stage=Stage.ACCESS, status=status, headline=headline, evidence_lines=gating_lines
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


def diagnose_need(
    need: InformationNeed,
    *,
    access: StageFinding,
    retrieval: StageFinding,
    citation: StageFinding,
    representation: StageFinding,
    registry: list[EvidenceEntry],
) -> NeedDiagnosis:
    findings = {
        Stage.ACCESS: access,
        Stage.RETRIEVAL: retrieval,
        Stage.CITATION: citation,
        Stage.REPRESENTATION: representation,
    }
    cautions: list[str] = []
    skip: frozenset[Stage] = frozenset()
    # 通道分歧防线:被引必先被检索。引用阶段 PASS 时,裸检索通道的缺席
    # 只能说明观测通道与生成端实际检索(查询改写/多语言 fan-out)不等同
    # (风险 #5:观测通道不当真值),不得据此定位检索失败。
    if (
        findings[Stage.RETRIEVAL].status in (StageStatus.FAIL, StageStatus.WEAK)
        and findings[Stage.CITATION].status is StageStatus.PASS
    ):
        skip = frozenset({Stage.RETRIEVAL})
        cautions.append(
            "检索观测通道显示品牌缺席,但引用阶段 PASS——被引必先被检索,两通道结论矛盾。"
            "这说明裸检索观测通道与生成端实际执行的检索(含查询改写与多语言扩展)不等同,"
            "检索结论按观测通道分歧处理,不据此定位失败(架构书风险 #5:观测通道不当真值)"
        )
    primary = localize_failure(findings, skip=skip)
    unobservable = [s.value for s in findings if findings[s].status is StageStatus.NOT_OBSERVABLE]
    if unobservable:
        cautions.append(f"以下阶段不可观测,未参与定位:{', '.join(unobservable)}")
    if primary in (Stage.ACCESS, Stage.RETRIEVAL):
        cautions.append(
            "失败定位在准入/检索层:当前证据不足以表明问题出在引用或生成阶段,"
            "因此【不建议】进行全页内容改写(依据见架构书 5.3)"
        )
    if primary is None:
        cautions.append("未定位到失败阶段:各可观测阶段均无 FAIL/WEAK,无需药方")

    recommendations = (
        recommend(registry, stage=primary, intent=need.intent) if primary is not None else []
    )
    return NeedDiagnosis(
        need_id=need.need_id,
        statement=need.statement,
        intent=need.intent,
        findings=[access, retrieval, citation, representation],
        primary_failure=primary,
        cautions=cautions,
        recommendations=recommendations,
    )
