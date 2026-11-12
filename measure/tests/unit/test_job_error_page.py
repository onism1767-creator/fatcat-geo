"""任务页的出错说明(v0.4.21):有代码的错误按「代码 + 参数」随界面语言组句;两种账户问题两句话;
意外异常照原文。零网络零数据库。"""

from fastapi.testclient import TestClient

from adapters.base import CapabilityRegistry, SurfaceAdapter, SurfaceMeta
from diagnosis.recommender import load_evidence_registry
from engine.models import BrandSpec, Prompt, PromptIntent, PromptOrigin, Target
from report.app import create_app
from report.jobs import DiagnosisJob, JobState
from tests.unit.test_report_page import LOW


class DummyAdapter(SurfaceAdapter):
    def meta(self) -> SurfaceMeta:
        return SurfaceMeta(display_name="S1", api_name="S1", provider="test_provider", surface="s1", models=["m1"],
                           default_model="m1", capabilities=CapabilityRegistry(supports_citations=True),
                           consumer_default_model="m1", main_model="m1", models_checked="test",
                           est_cost_per_call_usd={"m1": 0.005})

    def ask(self, request):
        raise AssertionError("测试不应触发真实采样")

    def healthcheck(self) -> bool:
        return True


def _failed_job(code: str | None, text: str | None = None) -> DiagnosisJob:
    job = DiagnosisJob(brand=BrandSpec(name="X", domains=["x.com"]), website="https://x.com",
                       category="C", locale="zh-CN", candidates=[], competitors_suggested=[])
    prompt = Prompt(text="q?", intent=PromptIntent.CATEGORY_DISCOVERY, origin=PromptOrigin.USER, locale="zh-CN")
    job.freeze(prompts=[prompt],
               competitors=[], targets=[Target(surface="s1", model="m1")], budget_usd=None, tier=LOW)
    job.state, job.phase = JobState.FAILED, "phase_failed"
    job.error_code = code
    job.error_params = {"ai": "S1", "model": "m1", "detail": "Your credit balance is too low"} if code else {}
    job.error = text
    return job


def _page(job: DiagnosisJob, lang: str) -> str:
    app = create_app(call_stats=lambda brand=None: [], adapters={"s1": DummyAdapter()}, dsn="postgresql://unused",
                     evidence=load_evidence_registry(), job_runner=lambda **kw: None)
    app.state.ctx.registry.add(job)
    return TestClient(app).get(f"/jobs/{job.job_id}?lang={lang}").text


def test_credit_and_auth_problems_get_their_own_sentence_in_both_languages():
    credit_zh = _page(_failed_job("ACCOUNT_CREDIT"), "zh")
    assert "S1(型号 m1)" in credit_zh and "额度不足" in credit_zh and "充值" in credit_zh
    assert "Your credit balance is too low" in credit_zh and "AccountProblem" not in credit_zh
    auth_zh = _page(_failed_job("ACCOUNT_AUTH"), "zh")
    assert "key 无效或没有使用权限" in auth_zh and "换好后" in auth_zh and "充值" not in auth_zh
    credit_en = _page(_failed_job("ACCOUNT_CREDIT"), "en")
    assert "out of credit" in credit_en and "Top up" in credit_en


def test_uncoded_failures_still_show_their_raw_text():
    page = _page(_failed_job(None, text="ValueError: something odd"), "zh")
    assert "ValueError: something odd" in page


def test_db_unreachable_has_its_own_code_and_instructions():
    page = _page(_failed_job("DB_UNREACHABLE"), "zh")
    assert "DB_UNREACHABLE" in page and "pgdev.ps1 status" in page and "PG_ZOMBIE" in page


def test_running_job_reports_quiet_minutes_and_stall_code():
    """v0.4.22:任务页显示「最近一次新结果在几分钟前」;超过 STALL_MINUTES 没有新样本就给 JOB_STALLED。"""
    from datetime import UTC, datetime, timedelta

    from report.jobs import POLICY, STALL_MINUTES

    # 阈值必须高过一次采样合法的最坏耗时(3 次尝试 × 超时 + 中间等待),否则健康但慢的腿会被提示重启(审核 2026-10-27 黄 17)
    worst_minutes = (POLICY.max_attempts * POLICY.timeout_s + (POLICY.max_attempts - 1) * POLICY.max_wait_s) / 60
    assert STALL_MINUTES > worst_minutes
    job = _failed_job(None)
    job.state, job.phase = JobState.RUNNING, "phase_sampling"
    job.started_at = datetime.now(UTC) - timedelta(minutes=30)
    job.last_result_at = datetime.now(UTC) - timedelta(minutes=2)
    fresh = _page(job, "zh")
    assert "最近一次新结果在 2." in fresh and "JOB_STALLED" not in fresh
    job.last_result_at = datetime.now(UTC) - timedelta(minutes=STALL_MINUTES + 5)
    stalled = _page(job, "zh")
    assert "JOB_STALLED" in stalled and "卡住" in stalled and "接着跑" in stalled
    job.last_result_at = None  # 一条都没回来过:从本次开始时刻算
    assert "JOB_STALLED" in _page(job, "en") and "stuck" in _page(job, "en")


def test_every_account_problem_says_what_to_do_in_both_languages():
    """五种账户问题各一句话、一个下一步(用户 2026-10-29 定:分不清是致命的);地区不支持与账户受限明说换 key 没用。"""
    expected = {
        "ACCOUNT_REGION": ("国家或地区", "换 key 没有用", "country or region", "A new key will not help"),
        "ACCOUNT_RESTRICTED": ("被平台限制了", "换 key 没有用", "restricted by the platform",
                               "A new key will not help"),
        "ACCOUNT_QUOTA": ("额度用完了", "等额度重置", "used up its quota", "Wait for the quota to reset"),
        "ACCOUNT_AUTH": ("也可能是账户被平台限制", "先看平台原话", "may be restricted", "Read what the platform said"),
    }
    for code, (zh_a, zh_b, en_a, en_b) in expected.items():
        zh, en = _page(_failed_job(code), "zh"), _page(_failed_job(code), "en")
        assert zh_a in zh and zh_b in zh, code
        assert en_a in en and en_b in en, code
