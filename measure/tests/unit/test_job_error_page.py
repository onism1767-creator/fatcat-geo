"""任务页的出错说明(v0.4.21):有代码的错误按「代码 + 参数」随界面语言组句;两种账户问题两句话;
意外异常照原文。零网络零数据库。"""

from fastapi.testclient import TestClient

from adapters.base import CapabilityRegistry, SurfaceAdapter, SurfaceMeta
from diagnosis.recommender import load_evidence_registry
from engine.models import BrandSpec, InformationNeed, NeedIntent
from report.app import create_app
from report.jobs import DiagnosisJob, JobState, Target


class DummyAdapter(SurfaceAdapter):
    def meta(self) -> SurfaceMeta:
        return SurfaceMeta(display_name="S1", provider="test_provider", surface="s1", models=["m1"],
                           default_model="m1", capabilities=CapabilityRegistry(supports_citations=True),
                           est_cost_per_call_usd={"m1": 0.005})

    def ask(self, request):
        raise AssertionError("测试不应触发真实采样")

    def healthcheck(self) -> bool:
        return True


def _failed_job(code: str | None, text: str | None = None) -> DiagnosisJob:
    job = DiagnosisJob(brand=BrandSpec(name="X", domains=["x.com"]), website="https://x.com",
                       category="C", locale="zh-CN", candidates=[], competitors_suggested=[])
    job.freeze(needs=[InformationNeed(statement="q?", intent=NeedIntent.DISCOVERY)], competitors=[],
               targets=[Target(surface="s1", model="m1")], tier_id="quick", variants_per_need=1, budget_usd=None)
    job.state, job.phase = JobState.FAILED, "phase_failed"
    job.error_code = code
    job.error_params = {"ai": "S1", "model": "m1", "detail": "Your credit balance is too low"} if code else {}
    job.error = text
    return job


def _page(job: DiagnosisJob, lang: str) -> str:
    app = create_app(call_stats=lambda: [], adapters={"s1": DummyAdapter()}, dsn="postgresql://unused",
                     evidence=load_evidence_registry(), job_runner=lambda **kw: None)
    app.state.registry.add(job)
    return TestClient(app).get(f"/jobs/{job.job_id}?lang={lang}").text


def test_credit_and_auth_problems_get_their_own_sentence_in_both_languages():
    credit_zh = _page(_failed_job("job_error.account_credit"), "zh")
    assert "S1(型号 m1)" in credit_zh and "额度不足" in credit_zh and "充值" in credit_zh
    assert "Your credit balance is too low" in credit_zh and "AccountProblem" not in credit_zh
    auth_zh = _page(_failed_job("job_error.account_auth"), "zh")
    assert "key 无效或没有使用权限" in auth_zh and "换好后" in auth_zh and "充值" not in auth_zh
    credit_en = _page(_failed_job("job_error.account_credit"), "en")
    assert "out of credit" in credit_en and "Top up" in credit_en


def test_uncoded_failures_still_show_their_raw_text():
    page = _page(_failed_job(None, text="ValueError: something odd"), "zh")
    assert "ValueError: something odd" in page
