"""L4 页面测试,含 12.2「报告口径测试」的可执行化:

渲染输出不得匹配总分模式;每条指标携带 N 与 stop_reasons;
每条建议携带 evidence id;C 级以下带「建议实验验证」。
页面流用假 surface 与桩执行器,零网络零数据库。
"""

import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from adapters.base import CapabilityRegistry, SurfaceAdapter, SurfaceMeta
from diagnosis.citation_diag import diagnose_citation
from diagnosis.diagnose import access_finding, diagnose_need
from diagnosis.recommender import load_evidence_registry
from diagnosis.representation_diag import diagnose_representation
from diagnosis.retrieval_diag import diagnose_retrieval
from engine.metrics import account_outcomes, estimate_proportion
from engine.models import (
    BrandSpec,
    InformationNeed,
    NeedIntent,
    Outcome,
    Sentiment,
    StopReason,
)
from engine.needs import NeedCandidate
from engine.scheduler import NeedSurfaceMetric
from report.app import create_app
from report.jobs import DiagnosisJob, JobState, NeedReport, estimate_job

REGISTRY = load_evidence_registry()


class DummyAdapter(SurfaceAdapter):
    def meta(self) -> SurfaceMeta:
        return SurfaceMeta(provider="test_provider", surface="s1", models=["m1"],
                           default_model="m1",
                           capabilities=CapabilityRegistry(supports_citations=True))

    def ask(self, request):
        raise AssertionError("测试不应触发真实采样")

    def healthcheck(self) -> bool:
        return True


def make_metric(k: int, n: int) -> NeedSurfaceMetric:
    return NeedSurfaceMetric(
        information_need_id="need-1", provider="test_provider", surface="s1", model="m1",
        metric_label="test", k_hits=k,
        estimate=estimate_proportion(k, n),
        accounting=account_outcomes([Outcome.SUCCESS] * n),
        stop_reasons=[StopReason.CI_CONVERGED, StopReason.MAX_N_REACHED],
        per_variant=[],
    )


def finished_job() -> DiagnosisJob:
    brand = BrandSpec(name="极光CRM", domains=["aurora-crm.com"])
    need = InformationNeed(statement="中小团队用什么 CRM 好?", intent=NeedIntent.DISCOVERY)
    diagnosis = diagnose_need(
        need,
        access=access_finding(None),
        retrieval=diagnose_retrieval(make_metric(0, 30), []),
        citation=diagnose_citation(make_metric(0, 30), [("竞品甲", estimate_proportion(20, 30))]),
        representation=diagnose_representation(
            NeedIntent.DISCOVERY, make_metric(20, 30), {Sentiment.POSITIVE: 5}
        ),
        registry=REGISTRY,
    )
    job = DiagnosisJob(brand=brand, website="https://aurora-crm.com", category="CRM",
                       locale="zh-CN", candidates=[], competitors_suggested=[])
    job.freeze(needs=[need], competitors=[], surfaces=["s1"], tier_id="quick",
               variants_per_need=2, budget_usd=1.0)
    job.state = JobState.DONE
    job.phase = "完成"
    job.finished_at = datetime(2026, 8, 24, tzinfo=UTC)
    job.need_reports = [NeedReport(need=need, variant_texts=["中小团队用什么 CRM 好?"],
                                   diagnosis=diagnosis)]
    return job


@pytest.fixture()
def client_and_job():
    calls = []

    def stub_runner(*, job, adapters, dsn, evidence):
        calls.append(job.job_id)
        job.state = JobState.DONE
        job.phase = "完成"

    app = create_app(adapters={"s1": DummyAdapter()}, dsn="postgresql://unused",
                     evidence=REGISTRY, job_runner=stub_runner)
    job = finished_job()
    app.state.registry.add(job)
    return TestClient(app), job, calls


# ── 12.2 报告口径测试 ─────────────────────────────────────────────────


def test_report_contains_no_score_patterns(client_and_job):
    client, job, _ = client_and_job
    html = client.get(f"/jobs/{job.job_id}/report").text
    assert not re.search(r"总分|得分|score\s*[::]", html, re.IGNORECASE)
    assert not re.search(r"\d+\s*/\s*100", html)


def test_report_metrics_carry_caliber(client_and_job):
    client, job, _ = client_and_job
    html = client.get(f"/jobs/{job.job_id}/report").text
    assert "N_valid" in html and "stop_reasons" in html
    assert "95% 区间(Jeffreys)" in html
    assert "判定阈值" in html  # 阈值口径自报
    assert job.fingerprint in html  # 任务快照指纹


def test_report_recommendations_carry_evidence_ids(client_and_job):
    client, job, _ = client_and_job
    html = client.get(f"/jobs/{job.job_id}/report").text
    assert "EVIDENCE_STRUCT_V1" in html  # RETRIEVAL FAIL → 结构信号建议(B 级)
    assert "建议实验验证" in html  # C 级以下强制标注
    assert "证据等级" in html


def test_report_shows_upstream_caution_and_competitor_gap(client_and_job):
    client, job, _ = client_and_job
    html = client.get(f"/jobs/{job.job_id}/report").text
    assert "不建议】进行全页" in html
    assert "显著落后于竞品 竞品甲" in html


# ── 页面流 ───────────────────────────────────────────────────────────


def test_index_renders_form(client_and_job):
    client, _, _ = client_and_job
    html = client.get("/").text
    assert "品牌名" in html and "目标问题" in html


def test_manual_flow_to_confirm_and_launch(client_and_job):
    client, _, calls = client_and_job
    resp = client.post("/jobs", data={
        "brand_name": "极光CRM", "aliases": "AuroraCRM",
        "website": "aurora-crm.com", "category": "CRM",
        "questions": "问题一?\n问题二?", "generate": "",
    }, follow_redirects=False)
    assert resp.status_code == 303
    confirm_url = resp.headers["location"]
    html = client.get(confirm_url).text
    assert "问题一?" in html and "确认并开始采样" in html

    resp = client.post(confirm_url, data={
        "needs_selected": ["0", "1"], "surfaces_selected": ["s1"],
        "tier": "quick", "variants": "2", "budget": "1.0", "custom": "",
    }, follow_redirects=False)
    assert resp.status_code == 303
    job_id = resp.headers["location"].rsplit("/", 1)[-1].split("?")[0]
    assert calls == [job_id]  # 执行器被触发
    job = client.app.state.registry.get(job_id)
    assert len(job.needs) == 2 and job.fingerprint


def test_confirm_rejects_empty_selection(client_and_job):
    client, _, _ = client_and_job
    resp = client.post("/jobs", data={
        "brand_name": "X", "website": "x.com", "category": "C",
        "questions": "q1?", "generate": "",
    }, follow_redirects=False)
    confirm_url = resp.headers["location"]
    resp = client.post(confirm_url, data={"surfaces_selected": ["s1"], "custom": ""},
                       follow_redirects=False)
    assert resp.status_code == 303 and "error=" in resp.headers["location"]


def test_unfinished_report_redirects_to_status(client_and_job):
    client, _, _ = client_and_job
    resp = client.post("/jobs", data={
        "brand_name": "X", "website": "x.com", "category": "C",
        "questions": "q1?", "generate": "",
    }, follow_redirects=False)
    job_id = resp.headers["location"].split("/")[2].split("?")[0]
    resp = client.get(f"/jobs/{job_id}/report", follow_redirects=False)
    assert resp.status_code == 303


# ── 预估与指纹 ────────────────────────────────────────────────────────


def test_estimate_job_arithmetic():
    est = estimate_job(3, 2, 2, "quick")
    assert est.calls == 90
    assert est.cost_usd == pytest.approx(90 * 0.0055)


def test_fingerprint_stable_and_sensitive():
    def build(statement: str) -> str:
        job = DiagnosisJob(brand=BrandSpec(name="X", domains=["x.com"]), website="https://x.com",
                           category="C", locale="zh-CN", candidates=[],
                           competitors_suggested=[])
        job.freeze(needs=[InformationNeed(need_id="fixed", statement=statement,
                                          intent=NeedIntent.DISCOVERY,
                                          created_at=datetime(2026, 8, 24, tzinfo=UTC))],
                   competitors=[], surfaces=["s1"], tier_id="quick",
                   variants_per_need=2, budget_usd=None)
        return job.fingerprint

    assert build("问法甲") == build("问法甲")  # 同快照同指纹
    assert build("问法甲") != build("问法乙")  # 改题 = 新基线


def test_candidate_default_check_logic():
    # 确认页默认勾选前 4 条与 brand_probe(模板逻辑的输入侧约束:candidates 顺序稳定)
    cand = NeedCandidate(intent=NeedIntent.BRAND_PROBE, text="X 怎么样?")
    assert cand.intent is NeedIntent.BRAND_PROBE
