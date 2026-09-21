"""执行器端到端(v0.4.19):同一台仪器的两个型号各测各的、各报各的,样本互不串。

走真实执行器 + 真实调度器 + 真实存储(测试库)+ 真实诊断,只把两处联网动作换成桩:
官网抓取审计与被引 URL 重抓(12.3:单测不许碰真实网络)。适配器是按型号给不同回答的假仪器。
"""

from datetime import UTC, datetime

import pytest
from conftest import TEST_DSN

from adapters.base import CapabilityRegistry, SurfaceAdapter, SurfaceMeta
from diagnosis.findings import Stage, StageStatus
from diagnosis.recommender import load_evidence_registry
from engine.models import (
    Answer,
    AskRequest,
    BrandSpec,
    Citation,
    InformationNeed,
    NeedIntent,
    Outcome,
    SearchTrace,
    SurfaceRun,
)
from report import runner
from report.jobs import DiagnosisJob, JobState, Target

BRAND = BrandSpec(name="极光CRM", domains=["aurora-crm.com"])
CITES, SILENT = "m-cites", "m-silent"


class TwoModelAdapter(SurfaceAdapter):
    """同一台仪器、两个型号:一个每次都引用官网并提到品牌,一个从不。"""

    def meta(self) -> SurfaceMeta:
        return SurfaceMeta(display_name="假 AI", provider="test_provider", surface="two_model",
                           models=[CITES, SILENT], default_model=CITES,
                           capabilities=CapabilityRegistry(supports_citations=True),
                           est_cost_per_call_usd={CITES: 0.01, SILENT: 0.001})

    def ask(self, request: AskRequest) -> SurfaceRun:
        assert request.model in (CITES, SILENT)  # 执行器必须把型号显式传下来,不许落回默认型号
        cites = request.model == CITES
        return SurfaceRun(
            run_id=request.run_id, information_need_id=request.information_need_id,
            prompt_variant_id=request.prompt_variant_id, provider="test_provider", surface="two_model",
            model=request.model, search_mode=request.search_mode, locale=request.locale,
            outcome=Outcome.SUCCESS, search_trace=SearchTrace(search_triggered=True),
            answer=Answer(
                answer_text="推荐极光CRM。" if cites else "推荐别家。",
                citations_available=True,
                citations=[Citation(url="https://aurora-crm.com/a", domain="aurora-crm.com", position=1)]
                if cites else [],
            ),
            raw_payload={"model": request.model},
            timestamp=datetime.now(UTC), latency_ms=1, cost_usd=0.01 if cites else 0.001,
        )

    def healthcheck(self) -> bool:
        return True


class _NoNetworkVerifier:
    def __init__(self, storage) -> None:
        pass

    def verify(self, urls, brands) -> dict:
        return {}

    def close(self) -> None:
        pass


@pytest.fixture()
def offline(monkeypatch):
    monkeypatch.setattr(runner.access_audit, "audit", lambda website: None)
    monkeypatch.setattr(runner, "CitationVerifier", _NoNetworkVerifier)


def _citation_status(job: DiagnosisJob) -> dict[str, StageStatus]:
    [report] = job.need_reports
    return {
        block.model: next(f.status for f in block.findings if f.stage is Stage.CITATION)
        for block in report.diagnosis.surfaces
    }


def test_two_models_of_one_surface_are_sampled_and_diagnosed_apart(storage, offline):
    need = InformationNeed(statement="中小团队用什么 CRM 好?", intent=NeedIntent.DISCOVERY)
    job = DiagnosisJob(brand=BRAND, website="https://aurora-crm.com", category="CRM",
                       locale="zh-CN", candidates=[], competitors_suggested=[])
    targets = [Target(surface="two_model", model=CITES), Target(surface="two_model", model=SILENT)]
    job.freeze(needs=[need], competitors=[], targets=targets, tier_id="quick",
               variants_per_need=1, budget_usd=None)
    adapters = {"two_model": TwoModelAdapter()}
    evidence = load_evidence_registry()

    runner.execute_job(job=job, adapters=adapters, dsn=TEST_DSN, evidence=evidence)

    assert job.state is JobState.DONE, job.error
    assert job.total_plans == 2  # 1 个问题 × 2 个型号:每个型号一份独立计划
    [variant] = storage.variants_for_need(need.need_id)
    runs = storage.runs_for_variant(variant.variant_id, surface="two_model")
    per_model = {m: [r for r in runs if r.model == m] for m in (CITES, SILENT)}
    assert {m: len(rs) for m, rs in per_model.items()} == {CITES: 6, SILENT: 6}  # 快扫档:前 6 次一致即停
    # 各报各的:两个型号各有一个诊断块、各有一句话,结论不同
    assert _citation_status(job) == {CITES: StageStatus.PASS, SILENT: StageStatus.WEAK}
    assert {(v.surface, v.model) for v in job.surface_verdicts} == {("two_model", CITES), ("two_model", SILENT)}
    assert job.spent_usd == pytest.approx(6 * 0.01 + 6 * 0.001)

    # 续跑(重新生成报告):样本按「问法 × 仪器 × 型号 × 模式」复用——零新调用,且两个型号的
    # 样本不互相充当前缀(串了的话两边都会变成 6/12,结论双双变黄)
    runner.execute_job(job=job, adapters=adapters, dsn=TEST_DSN, evidence=evidence)
    assert job.state is JobState.DONE, job.error
    assert len(storage.runs_for_variant(variant.variant_id, surface="two_model")) == 12
    assert _citation_status(job) == {CITES: StageStatus.PASS, SILENT: StageStatus.WEAK}
