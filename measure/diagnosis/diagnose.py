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
from diagnosis.findings import (
    ChannelFinding,
    Msg,
    RetrievalChannel,
    Stage,
    StageFinding,
    StageStatus,
    msg,
)
from diagnosis.recommender import EvidenceEntry, Recommendation, recommend
from diagnosis.retrieval_dependency import DependencyFinding
from engine.models import FrozenModel, InformationNeed, NeedIntent

_FUNNEL = (Stage.RETRIEVAL, Stage.CITATION, Stage.REPRESENTATION)


class NeedDiagnosis(FrozenModel):
    need_id: str
    statement: str
    intent: NeedIntent
    findings: list[StageFinding]  # 按 ACCESS/RETRIEVAL/CITATION/REPRESENTATION 顺序
    primary_failure: Stage | None
    cautions: list[Msg]
    recommendations: list[Recommendation]
    retrieval_dependency: DependencyFinding | None = None  # 第 9 章挂靠观测,不参与定位
    # 双通道架构(v0.4.8):生成端通道存在时填两条(标准化 + 生成端)并列展示;
    # 只有裸检索时为空,RETRIEVAL 阶段按单通道渲染(与旧版一致)
    retrieval_channels: list[ChannelFinding] = []


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


def diagnose_need(
    need: InformationNeed,
    *,
    access: StageFinding,
    retrieval: StageFinding,
    citation: StageFinding,
    representation: StageFinding,
    registry: list[EvidenceEntry],
    retrieval_dependency: DependencyFinding | None = None,
    retrieval_generative: ChannelFinding | None = None,
) -> NeedDiagnosis:
    cautions: list[Msg] = []
    # ── RETRIEVAL 双通道合成(v0.4.8)────────────────────────────────
    # 生成端通道可观测时,阶段状态与定位依据取生成端(与引用同仪器,
    # 「被引必先被检索」在同仪器内严格成立);标准化裸检索降为并列参考。
    # 两通道各自完整结论保留在 retrieval_channels,报告分视角展示。
    channels: list[ChannelFinding] = []
    if (
        retrieval_generative is not None
        and retrieval_generative.status is not StageStatus.NOT_OBSERVABLE
    ):
        channels = [
            ChannelFinding(
                channel=RetrievalChannel.STANDARDIZED,
                status=retrieval.status,
                headline=retrieval.headline,
                evidence_lines=retrieval.evidence_lines,
                low_confidence=retrieval.low_confidence,
            ),
            retrieval_generative,
        ]
        if retrieval.status is not StageStatus.NOT_OBSERVABLE and (
            retrieval.status is not retrieval_generative.status
        ):
            cautions.append(msg(
                "caution.channel_divergence",
                std=retrieval.status.value,
                gen=retrieval_generative.status.value,
            ))
        retrieval = StageFinding(
            stage=Stage.RETRIEVAL,
            status=retrieval_generative.status,
            headline=retrieval_generative.headline,
            evidence_lines=[msg("retrieval.channel_basis")],
            low_confidence=retrieval_generative.low_confidence,
        )

    findings = {
        Stage.ACCESS: access,
        Stage.RETRIEVAL: retrieval,
        Stage.CITATION: citation,
        Stage.REPRESENTATION: representation,
    }
    skip: frozenset[Stage] = frozenset()
    # 通道分歧防线:被引必先被检索。引用阶段 PASS 时,裸检索通道的缺席
    # 只能说明观测通道与生成端实际检索(查询改写/多语言 fan-out)不等同
    # (风险 #5:观测通道不当真值),不得据此定位检索失败。
    if (
        findings[Stage.RETRIEVAL].status in (StageStatus.FAIL, StageStatus.WEAK)
        and findings[Stage.CITATION].status is StageStatus.PASS
    ):
        skip = frozenset({Stage.RETRIEVAL})
        cautions.append(msg("caution.divergence"))
    primary = localize_failure(findings, skip=skip)
    unobservable = [s.value for s in findings if findings[s].status is StageStatus.NOT_OBSERVABLE]
    if unobservable:
        cautions.append(msg("caution.unobservable", stages=", ".join(unobservable)))
    if primary in (Stage.ACCESS, Stage.RETRIEVAL):
        cautions.append(msg("caution.no_rewrite"))
    if primary is None:
        cautions.append(msg("caution.none"))

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
        retrieval_dependency=retrieval_dependency,
        retrieval_channels=channels,
    )
