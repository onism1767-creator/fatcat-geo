"""生成端检索通道与双通道合成测试(v0.4.8 架构)。

- 通道结论:状态取候选在场区间(与裸检索共用「检索在场」阈值);
  漏斗对比只认区间端点;方差经由区间如实呈现。
- 合成规则:生成端可观测时定位依据取生成端(与引用同仪器),
  裸检索降为并列参考;两通道状态分歧出警示;各自完整结论保留。
"""


from diagnosis.diagnose import diagnose_need
from diagnosis.findings import RetrievalChannel, Stage, StageFinding, StageStatus, msg
from diagnosis.generative_retrieval import diagnose_generative_retrieval
from diagnosis.messages import render_msg
from diagnosis.recommender import load_evidence_registry
from engine.metrics import ProportionEstimate, account_outcomes
from engine.models import InformationNeed, NeedIntent, Outcome, StopReason
from engine.scheduler import NeedSurfaceMetric

REGISTRY = load_evidence_registry()


def est(ci_low: float, ci_high: float, n: int = 20) -> ProportionEstimate:
    return ProportionEstimate(
        k=round(n * (ci_low + ci_high) / 2), n=n,
        point=(ci_low + ci_high) / 2, ci_low=ci_low, ci_high=ci_high,
    )


def presence_metric(estimate: ProportionEstimate | None) -> NeedSurfaceMetric:
    return NeedSurfaceMetric(
        information_need_id="need-1", provider="p", surface="s1", model="m1",
        metric_label="retrieval_rank_domain_match", k_hits=0,
        estimate=estimate,
        accounting=account_outcomes([Outcome.SUCCESS] * 20),
        stop_reasons=[StopReason.CI_CONVERGED],
        per_variant=[],
    )


# ── 通道结论 ──────────────────────────────────────────────────────────


def test_no_channel_when_no_metric():
    assert diagnose_generative_retrieval(None, [], None, None, None) is None


def test_not_observable_when_no_estimate():
    finding = diagnose_generative_retrieval(presence_metric(None), [], None, None, None)
    assert finding.status is StageStatus.NOT_OBSERVABLE
    assert finding.channel is RetrievalChannel.GENERATIVE


def test_status_from_presence_interval():
    assert diagnose_generative_retrieval(
        presence_metric(est(0.5, 0.9)), [2], None, None, None
    ).status is StageStatus.PASS
    assert diagnose_generative_retrieval(
        presence_metric(est(0.02, 0.15)), [], None, None, None
    ).status is StageStatus.FAIL
    assert diagnose_generative_retrieval(
        presence_metric(est(0.15, 0.5)), [3], None, None, None
    ).status is StageStatus.WEAK


def test_funnel_gap_when_cited_significantly_below_presence():
    finding = diagnose_generative_retrieval(
        presence_metric(est(0.6, 0.9)), [1], est(0.1, 0.3), est(0.05, 0.25), est(0.8, 0.99)
    )
    codes = [line.code for line in finding.evidence_lines]
    assert "genret.funnel.pair" in codes
    assert "genret.funnel.gap" in codes  # 递上来了却常落选 → 内容竞争力
    assert "genret.query_entry" in codes
    assert "genret.triggered" in codes


def test_funnel_aligned_when_intervals_overlap():
    finding = diagnose_generative_retrieval(
        presence_metric(est(0.4, 0.8)), [1], None, est(0.35, 0.7), None
    )
    codes = [line.code for line in finding.evidence_lines]
    assert "genret.funnel.aligned" in codes and "genret.funnel.gap" not in codes


def test_channel_messages_render_bilingual():
    finding = diagnose_generative_retrieval(
        presence_metric(est(0.6, 0.9)), [1, 3], est(0.1, 0.3), est(0.05, 0.25), est(0.8, 0.99)
    )
    for lang in ("zh", "en"):
        assert render_msg(finding.headline, lang).strip()
        for line in finding.evidence_lines:
            assert render_msg(line, lang).strip()


# ── 双通道合成(diagnose_need)─────────────────────────────────────────


def stage(stage_kind: Stage, status: StageStatus, code: str) -> StageFinding:
    return StageFinding(stage=stage_kind, status=status, headline=msg(code))


def need() -> InformationNeed:
    return InformationNeed(statement="中小团队用什么 CRM 好?", intent=NeedIntent.DISCOVERY)


def diagnose(bare_status: StageStatus, generative, citation_status=StageStatus.FAIL):
    return diagnose_need(
        need(),
        access=stage(Stage.ACCESS, StageStatus.PASS, "access.headline.pass"),
        retrieval=stage(Stage.RETRIEVAL, bare_status, "retrieval.headline.fail"),
        citation=stage(Stage.CITATION, citation_status, "citation.headline.fail"),
        representation=stage(Stage.REPRESENTATION, StageStatus.WEAK, "repr.headline.neutral_weak"),
        registry=REGISTRY,
        retrieval_generative=generative,
    )


def test_without_generative_channel_behavior_unchanged():
    diagnosis = diagnose(StageStatus.FAIL, None)
    assert diagnosis.retrieval_channels == []
    assert diagnosis.primary_failure is Stage.RETRIEVAL  # 裸检索单通道照旧定位


def test_generative_channel_takes_over_stage_status_and_localization():
    generative = diagnose_generative_retrieval(presence_metric(est(0.5, 0.9)), [1], None, None, None)
    diagnosis = diagnose(StageStatus.FAIL, generative)

    # 两通道各自完整结论并列保留
    assert [c.channel for c in diagnosis.retrieval_channels] == [
        RetrievalChannel.STANDARDIZED, RetrievalChannel.GENERATIVE
    ]
    assert diagnosis.retrieval_channels[0].status is StageStatus.FAIL
    # 合成阶段状态取生成端(同仪器优先),裸检索 FAIL 不再定位到 RETRIEVAL
    retrieval_finding = diagnosis.findings[1]
    assert retrieval_finding.status is StageStatus.PASS
    assert retrieval_finding.evidence_lines[0].code == "retrieval.channel_basis"
    assert diagnosis.primary_failure is Stage.CITATION
    # 状态分歧出警示
    assert any(c.code == "caution.channel_divergence" for c in diagnosis.cautions)


def test_no_divergence_caution_when_channels_agree():
    generative = diagnose_generative_retrieval(presence_metric(est(0.02, 0.15)), [], None, None, None)
    diagnosis = diagnose(StageStatus.FAIL, generative)
    assert diagnosis.findings[1].status is StageStatus.FAIL
    assert not any(c.code == "caution.channel_divergence" for c in diagnosis.cautions)
    assert diagnosis.primary_failure is Stage.RETRIEVAL


def test_unobservable_generative_channel_falls_back_to_bare():
    generative = diagnose_generative_retrieval(presence_metric(None), [], None, None, None)
    diagnosis = diagnose(StageStatus.FAIL, generative)
    assert diagnosis.retrieval_channels == []  # 生成端不可观测 → 不拆通道
    assert diagnosis.primary_failure is Stage.RETRIEVAL
