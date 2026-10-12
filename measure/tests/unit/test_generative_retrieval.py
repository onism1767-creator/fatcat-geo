"""生成端检索诊断与按 surface 分诊测试(v0.4.11 分家架构)。

- 生成端结论:状态取候选在场区间,
  直接作为该 AI 漏斗的 RETRIEVAL 阶段参与定位;漏斗对比只认区间端点。
- 分家定位:每家 surface 独立走漏斗;v0.4.4 通道分歧防线保留
  (引用 PASS 时检索 FAIL 不定位,只作警示)。
"""

from diagnosis.diagnose import diagnose_need, diagnose_surface
from diagnosis.findings import Stage, StageStatus
from diagnosis.generative_retrieval import diagnose_generative_retrieval
from diagnosis.messages import render_msg
from diagnosis.recommender import load_evidence_registry
from engine.aggregate import NeedSurfaceMetric
from engine.metrics import ProportionEstimate, account_outcomes
from engine.models import InformationNeed, NeedIntent, Outcome, StopReason

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
        variant_estimates=[estimate] if estimate is not None else [],
    )


# ── 生成端 RETRIEVAL 结论 ─────────────────────────────────────────────


def test_not_observable_when_no_metric_or_estimate():
    for metric in (None, presence_metric(None)):
        finding = diagnose_generative_retrieval(metric, [], None, None, None)
        assert finding.stage is Stage.RETRIEVAL
        assert finding.status is StageStatus.NOT_OBSERVABLE


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


def test_generative_messages_render_bilingual():
    finding = diagnose_generative_retrieval(
        presence_metric(est(0.6, 0.9)), [1, 3], est(0.1, 0.3), est(0.05, 0.25), est(0.8, 0.99)
    )
    for lang in ("zh", "en"):
        assert render_msg(finding.headline, lang).strip()
        for line in finding.evidence_lines:
            assert render_msg(line, lang).strip()


# ── 分家分诊:生成端作为该 AI 的 RETRIEVAL 阶段参与定位 ────────────────


def surface_diagnosis(retrieval_finding, citation_status=StageStatus.FAIL):
    from diagnosis.findings import StageFinding, msg

    return diagnose_surface(
        provider="p", surface="s1", model="m1",
        access=StageFinding(stage=Stage.ACCESS, status=StageStatus.PASS,
                            headline=msg("access.headline.pass")),
        retrieval=retrieval_finding,
        citation=StageFinding(stage=Stage.CITATION, status=citation_status,
                              headline=msg("citation.headline.fail")),
        representation=StageFinding(stage=Stage.REPRESENTATION, status=StageStatus.PASS,
                                    headline=msg("repr.headline.neutral_pass")),
        intent=NeedIntent.DISCOVERY, registry=REGISTRY,
    )


def test_generative_pass_localizes_downstream():
    generative = diagnose_generative_retrieval(presence_metric(est(0.5, 0.9)), [1], None, None, None)
    diagnosis = surface_diagnosis(generative, citation_status=StageStatus.FAIL)
    assert diagnosis.findings[0].status is StageStatus.PASS  # RETRIEVAL = 生成端结论
    assert diagnosis.primary_failure is Stage.CITATION  # 递上来了没被引 → 病灶在引用


def test_generative_fail_localizes_retrieval():
    generative = diagnose_generative_retrieval(presence_metric(est(0.02, 0.15)), [], None, None, None)
    diagnosis = surface_diagnosis(generative, citation_status=StageStatus.FAIL)
    assert diagnosis.primary_failure is Stage.RETRIEVAL


def test_divergence_guard_survives_per_surface():
    """v0.4.4 防线在分家后逐 surface 执行:引用 PASS 时检索 FAIL 只作警示。"""
    generative = diagnose_generative_retrieval(presence_metric(est(0.02, 0.15)), [], None, None, None)
    diagnosis = surface_diagnosis(generative, citation_status=StageStatus.PASS)
    assert diagnosis.primary_failure is None
    assert any(c.code == "caution.divergence" for c in diagnosis.cautions)


def test_need_diagnosis_assembles_surface_blocks():
    from diagnosis.findings import StageFinding, msg

    need = InformationNeed(statement="中小团队用什么 CRM 好?", intent=NeedIntent.DISCOVERY)
    generative = diagnose_generative_retrieval(presence_metric(est(0.5, 0.9)), [1], None, None, None)
    block = surface_diagnosis(generative)
    diagnosis = diagnose_need(
        need,
        access=StageFinding(stage=Stage.ACCESS, status=StageStatus.PASS,
                            headline=msg("access.headline.pass")),
        surfaces=[block],
    )
    assert diagnosis.surfaces == [block]
    assert [f.stage for f in diagnosis.surfaces[0].findings] == [
        Stage.RETRIEVAL, Stage.CITATION, Stage.REPRESENTATION
    ]
    assert (block.provider, block.surface, block.model) == ("p", "s1", "m1")
