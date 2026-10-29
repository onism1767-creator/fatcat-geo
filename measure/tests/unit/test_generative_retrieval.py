"""生成端检索诊断与按 surface 分诊测试(v0.4.11 分家架构;v0.4.26 S2 看比例)。

- 生成端结论:状态取候选在场比例,直接作为该 AI 漏斗的 RETRIEVAL 阶段参与定位;
  漏斗对比只认「相差 ≥ rate_gap」。
- 分家定位:每家 surface 独立走漏斗;v0.4.4 通道分歧防线保留
  (引用 PASS 时检索 FAIL 不定位,只作警示)。
"""

from diagnosis.diagnose import diagnose_class, diagnose_surface
from diagnosis.findings import Stage, StageStatus
from diagnosis.generative_retrieval import diagnose_generative_retrieval
from diagnosis.messages import render_msg
from diagnosis.recommender import load_evidence_registry
from engine.aggregate import ClassSurfaceMetric
from engine.metrics import Rate, account_outcomes
from engine.models import Outcome, PromptIntent, StopReason

REGISTRY = load_evidence_registry()


def r(k: int, n: int = 20) -> Rate:
    return Rate(k=k, n=n)


def presence_metric(rate: Rate | None) -> ClassSurfaceMetric:
    return ClassSurfaceMetric(
        intent=PromptIntent.CATEGORY_DISCOVERY, provider="p", surface="s1", model="m1",
        metric_label="retrieval_rank_domain_match", k_hits=rate.k if rate else 0,
        rate=rate,
        accounting=account_outcomes([Outcome.SUCCESS] * 20),
        stop_reasons=[StopReason.COMPLETED],
        per_prompt=[rate] if rate is not None else [],
        prompt_coverage=None,
    )


# ── 生成端 RETRIEVAL 结论 ─────────────────────────────────────────────


def test_not_observable_when_no_metric_or_rate():
    for metric in (None, presence_metric(None)):
        finding = diagnose_generative_retrieval(metric, [], None, None, None)
        assert finding.stage is Stage.RETRIEVAL
        assert finding.status is StageStatus.NOT_OBSERVABLE


def test_status_from_presence_rate():
    assert diagnose_generative_retrieval(presence_metric(r(14)), [2], None, None, None).status is StageStatus.PASS
    assert diagnose_generative_retrieval(presence_metric(r(2)), [], None, None, None).status is StageStatus.FAIL
    assert diagnose_generative_retrieval(presence_metric(r(6)), [3], None, None, None).status is StageStatus.WEAK


def test_funnel_gap_when_cited_well_below_presence():
    finding = diagnose_generative_retrieval(presence_metric(r(15)), [1], r(4), r(3), r(18))
    codes = [line.code for line in finding.evidence_lines]
    assert "genret.funnel.pair" in codes
    assert "genret.funnel.gap" in codes  # 递上来了却常落选 → 内容竞争力
    assert "genret.query_entry" in codes
    assert "genret.triggered" in codes


def test_funnel_aligned_when_rates_are_close():
    finding = diagnose_generative_retrieval(presence_metric(r(12)), [1], None, r(10), None)
    codes = [line.code for line in finding.evidence_lines]
    assert "genret.funnel.aligned" in codes and "genret.funnel.gap" not in codes


def test_generative_messages_render_bilingual():
    finding = diagnose_generative_retrieval(presence_metric(r(15)), [1, 3], r(4), r(3), r(18))
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
        intent=PromptIntent.CATEGORY_DISCOVERY, registry=REGISTRY,
    )


def test_generative_pass_localizes_downstream():
    generative = diagnose_generative_retrieval(presence_metric(r(14)), [1], None, None, None)
    diagnosis = surface_diagnosis(generative, citation_status=StageStatus.FAIL)
    assert diagnosis.findings[0].status is StageStatus.PASS  # RETRIEVAL = 生成端结论
    assert diagnosis.primary_failure is Stage.CITATION  # 递上来了没被引 → 病灶在引用


def test_generative_fail_localizes_retrieval():
    generative = diagnose_generative_retrieval(presence_metric(r(2)), [], None, None, None)
    diagnosis = surface_diagnosis(generative, citation_status=StageStatus.FAIL)
    assert diagnosis.primary_failure is Stage.RETRIEVAL


def test_divergence_guard_survives_per_surface():
    """v0.4.4 防线在分家后逐 surface 执行:引用 PASS 时检索 FAIL 只作警示。"""
    generative = diagnose_generative_retrieval(presence_metric(r(2)), [], None, None, None)
    diagnosis = surface_diagnosis(generative, citation_status=StageStatus.PASS)
    assert diagnosis.primary_failure is None
    assert any(c.code == "caution.divergence" for c in diagnosis.cautions)


def test_class_diagnosis_assembles_surface_blocks():
    from diagnosis.findings import StageFinding, msg

    generative = diagnose_generative_retrieval(presence_metric(r(14)), [1], None, None, None)
    block = surface_diagnosis(generative)
    diagnosis = diagnose_class(
        PromptIntent.CATEGORY_DISCOVERY,
        access=StageFinding(stage=Stage.ACCESS, status=StageStatus.PASS,
                            headline=msg("access.headline.pass")),
        surfaces=[block],
    )
    assert diagnosis.surfaces == [block] and diagnosis.intent is PromptIntent.CATEGORY_DISCOVERY
    assert [f.stage for f in diagnosis.surfaces[0].findings] == [
        Stage.RETRIEVAL, Stage.CITATION, Stage.REPRESENTATION
    ]
    assert (block.provider, block.surface, block.model) == ("p", "s1", "m1")


def test_funnel_cited_higher_is_not_called_close():
    """在场 3、被引 18(同一批):不能说「相差不足」,也不能按漏斗解读(第二轮)。"""
    finding = diagnose_generative_retrieval(presence_metric(r(3)), [1], None, r(18), None)
    codes = [line.code for line in finding.evidence_lines]
    assert "genret.funnel.cited_higher" in codes and "genret.funnel.aligned" not in codes
    line = next(line for line in finding.evidence_lines if line.code == "genret.funnel.cited_higher")
    assert "个百分点" in render_msg(line, "zh") and "points" in render_msg(line, "en")
