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
from diagnosis.citation_diag import VerificationSummary, diagnose_citation
from diagnosis.diagnose import access_finding, diagnose_need, diagnose_surface
from diagnosis.findings import Stage
from diagnosis.generative_retrieval import diagnose_generative_retrieval
from diagnosis.recommender import load_evidence_registry
from diagnosis.representation_diag import diagnose_representation
from diagnosis.retrieval_dependency import diagnose_retrieval_dependency
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
        return SurfaceMeta(display_name="S1", provider="test_provider", surface="s1", models=["m1"],
                           default_model="m1",
                           capabilities=CapabilityRegistry(supports_citations=True),
                           est_cost_per_call_usd=0.005)

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
        variant_estimates=[estimate_proportion(k, n)],
    )


def finished_job() -> DiagnosisJob:
    brand = BrandSpec(name="极光CRM", domains=["aurora-crm.com"])
    need = InformationNeed(statement="中小团队用什么 CRM 好?", intent=NeedIntent.DISCOVERY)
    access = access_finding(None)
    # v0.4.11 分家:两家 surface 各自分诊——s1 借裸检索视角,s2 自带生成端链路
    bare_block = diagnose_surface(
        provider="test_provider", surface="s1", model="m1",
        access=access,
        retrieval=diagnose_retrieval(make_metric(0, 30), []),
        citation=diagnose_citation(
            make_metric(0, 30),
            [("竞品甲", estimate_proportion(20, 30))],
            verification=VerificationSummary(
                attempted=2, verified_match=0, content_mismatch=1,
                fetch_blocked=1, unparsed=0, unreachable=0,
            ),
        ),
        representation=diagnose_representation(
            NeedIntent.DISCOVERY, make_metric(20, 30), {Sentiment.POSITIVE: 5}
        ),
        intent=NeedIntent.DISCOVERY, registry=REGISTRY,
        # 开卷 20/30 vs 闭卷 2/30:区间分离且闭卷上界不足开卷下界一半 → HIGH
        retrieval_dependency=diagnose_retrieval_dependency(
            make_metric(20, 30), make_metric(2, 30)
        ),
    )
    generative_block = diagnose_surface(
        provider="test_provider", surface="s2", model="m2",
        access=access,
        # 生成端链路作为该家自己的 RETRIEVAL:在场 0/30 → FAIL
        retrieval=diagnose_generative_retrieval(
            make_metric(0, 30), [],
            estimate_proportion(1, 30), estimate_proportion(0, 30), estimate_proportion(25, 30),
        ),
        citation=diagnose_citation(make_metric(0, 30), [], verification=None),
        representation=diagnose_representation(NeedIntent.DISCOVERY, make_metric(20, 30), {}),
        intent=NeedIntent.DISCOVERY, registry=REGISTRY,
    )
    diagnosis = diagnose_need(need, access=access, surfaces=[bare_block, generative_block])
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
    assert "95% 可信区间(Jeffreys 先验)" in html  # v0.4.13:可信区间,不冒充频率学派置信区间
    assert "操作阈值(v1 初始值,未经校准)" in html  # 阈值口径自报,且标明未校准
    assert "最强瓶颈信号在" in html and "失败最可能" not in html  # 定位措辞去因果
    assert job.fingerprint in html  # 任务快照指纹


def test_report_recommendations_carry_evidence_ids(client_and_job):
    client, job, _ = client_and_job
    html = client.get(f"/jobs/{job.job_id}/report").text
    assert "EVIDENCE_STRUCT_V1" in html  # RETRIEVAL FAIL → 结构信号建议(B 级)
    assert "建议实验验证" in html  # C 级以下强制标注
    assert "证据等级" in html


def test_report_decision_view_dedups_actions_and_groups_bottlenecks(client_and_job):
    """v0.4.13 条目 8:决策视图在前——三句话摘要、去重后的优先动作(标支持数)、各 AI 四行;
    审计细节折叠在后。finished_job 两家 surface 同定位 RETRIEVAL → 同一证据条目只出一次、支持数 2。"""
    client, job, _ = client_and_job
    html = client.get(f"/jobs/{job.job_id}/report").text
    assert "① 决策摘要" in html and "④ 各 AI 四行" in html and "⑥ 审计与方法学附录" in html
    assert html.index("① 决策摘要") < html.index("⑥ 审计与方法学附录")
    assert "被 2 个诊断支持" in html
    assert "<b>RETRIEVAL</b>: s1, s2" in html  # 瓶颈分组:阶段 → 哪几家 AI
    assert html.count('class="rec"') == 3  # 去重:两家 × 三条 → 三条动作


def test_summarize_orders_by_evidence_level_then_support():
    from report.summary import summarize

    job = finished_job()
    summary = summarize(job.need_reports)
    levels = [a.recommendation.evidence_level for a in summary.actions]
    assert levels == sorted(levels)
    assert all(a.support_count == 2 for a in summary.actions)
    (brief_s1, brief_s2) = summary.engines[job.needs[0].need_id]
    assert brief_s1.primary is brief_s2.primary is Stage.RETRIEVAL
    assert brief_s1.strongest_evidence is not None and brief_s1.experiments


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
    # 按 surface 分价(v0.4.6):便宜通道与贵通道混选,成本逐个计价求和
    est = estimate_job(3, 2, [0.005, 0.17], "quick")
    assert est.calls == 90  # 3 需求 × 2 变体 × 2 surface × 7.5 期望采样
    assert est.cost_usd == pytest.approx(3 * 2 * 7.5 * (0.005 + 0.17))
    # 检索依赖对照:闭卷腿按自身单价另计(每腿采样策略与开卷相同)
    est_dep = estimate_job(3, 2, [0.005, 0.17], "quick", dependency_costs=[0.04])
    assert est_dep.calls == 135  # 多一条闭卷腿
    assert est_dep.cost_usd == pytest.approx(3 * 2 * 7.5 * (0.005 + 0.17 + 0.04))


def test_job_snapshot_round_trip():
    """快照往返(v0.4.9):冻结配置全保留;报告不入快照,续跑/重算时重建。"""
    job = finished_job()
    restored = DiagnosisJob.from_snapshot(job.to_snapshot())
    assert restored.job_id == job.job_id
    assert restored.fingerprint == job.fingerprint  # 同快照同指纹,续跑不换基线
    assert restored.state is JobState.DONE and restored.phase == "phase_done"
    assert [n.need_id for n in restored.needs] == [n.need_id for n in job.needs]
    assert restored.surfaces == job.surfaces and restored.tier_id == job.tier_id
    assert restored.dependency_enabled == job.dependency_enabled
    assert restored.need_reports == []


def test_resume_endpoint_relaunches_interrupted_job(client_and_job):
    client, job, calls = client_and_job
    job.state = JobState.INTERRUPTED
    resp = client.post(f"/jobs/{job.job_id}/resume", follow_redirects=False)
    assert resp.status_code == 303
    assert calls == [job.job_id]  # 执行器被再次触发(可重入,只补缺口)


def test_resume_endpoint_rejects_completed_job_with_report(client_and_job):
    client, job, calls = client_and_job  # 夹具任务:DONE 且报告在手 → 无缺可补
    resp = client.post(f"/jobs/{job.job_id}/resume", follow_redirects=False)
    assert resp.status_code == 303
    assert calls == []


def test_fingerprint_sensitive_to_dependency_flag():
    def build(dependency_enabled: bool) -> str:
        job = DiagnosisJob(brand=BrandSpec(name="X", domains=["x.com"]), website="https://x.com",
                           category="C", locale="zh-CN", candidates=[],
                           competitors_suggested=[])
        job.freeze(needs=[InformationNeed(need_id="fixed", statement="问法甲",
                                          intent=NeedIntent.DISCOVERY,
                                          created_at=datetime(2026, 8, 24, tzinfo=UTC))],
                   competitors=[], surfaces=["s1"], tier_id="quick",
                   variants_per_need=2, budget_usd=None,
                   dependency_enabled=dependency_enabled)
        return job.fingerprint

    assert build(False) != build(True)  # 闭卷对照开关是口径的一部分,必须进指纹


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
