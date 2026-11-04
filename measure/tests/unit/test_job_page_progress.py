"""任务页的核对网页进度(S8.5,用户定 2026-10-14):提问完之后不再显示「预计还需约 0 分钟」,
改为核对网页自己的进度条与「最多再等 N 分钟」(按预算给上限);核对也算活动,不会被当成「卡住了」。零网络零数据库。"""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from diagnosis.recommender import load_evidence_registry
from engine.models import BrandSpec, Prompt, PromptIntent, PromptOrigin, Target
from report.app import create_app
from report.jobs import DiagnosisJob, JobState
from tests.unit.test_job_error_page import DummyAdapter
from tests.unit.test_report_page import LOW


def _running_job(now: datetime) -> DiagnosisJob:
    job = DiagnosisJob(brand=BrandSpec(name="X", domains=["x.com"]), website="https://x.com",
                       category="C", locale="zh-CN", candidates=[], competitors_suggested=[])
    prompt = Prompt(text="q?", intent=PromptIntent.CATEGORY_DISCOVERY, origin=PromptOrigin.USER, locale="zh-CN")
    job.freeze(prompts=[prompt], competitors=[], targets=[Target(surface="s1", model="m1")], budget_usd=None, tier=LOW)
    job.state, job.phase = JobState.RUNNING, "phase_verify"
    job.started_at = now - timedelta(minutes=5)
    job.total_runs = 3  # 执行器开跑时才算总数;这里直接给
    job.completed_runs = job.total_runs  # 提问全部完成
    return job


def _page(job: DiagnosisJob, lang: str = "zh") -> str:
    app = create_app(call_stats=lambda brand=None: [], adapters={"s1": DummyAdapter()}, dsn="postgresql://unused",
                     evidence=load_evidence_registry(), job_runner=lambda **kw: None)
    app.state.ctx.registry.add(job)
    return TestClient(app).get(f"/jobs/{job.job_id}?lang={lang}").text


def test_verify_phase_shows_its_own_progress_and_a_bounded_wait():
    now = datetime.now(UTC)
    job = _running_job(now)
    job.verify_started_at = now - timedelta(minutes=2)
    job.verify_total, job.verify_done, job.verify_stage = 10, 4, "fetch"
    job.last_result_at = now - timedelta(minutes=1)  # 核对网页在报进度:不算卡住
    page = _page(job)
    assert "核对网页" in page and "已开 4/10 个" in page and "最多再等 13.0 分钟" in page  # 15 − 2
    assert "总进度 84%" in page and "提问 100%(已问 3/3)" in page  # 核对段 80 + 12 × 0.4;提问段自己 100%
    assert "预计还需约" not in page  # 不再显示按提问速度算的「还需 0 分钟」
    assert "正在核对 AI 引用的网页" in page  # 阶段名照旧
    assert 'style="width: 40%"' in page  # 核对进度条
    assert "卡住" not in page
    job.verify_stage = "render"
    assert "正在用浏览器补抓抽不出正文的网页" in _page(job)
    job.verify_stage = "done"
    assert "网页核对完成,正在整理结果" in _page(job)
    en = _page(job, "en")
    assert "Checking pages" in en and "4/10 opened" in en


def test_overall_progress_walks_through_the_stages():
    """总进度(用户定 2026-10-14):按阶段分段,提问段按已问次数、核对段按已开网页数,完成 100。"""
    now = datetime.now(UTC)
    job = _running_job(now)
    job.phase, job.completed_runs = "phase_access", 0
    assert job.overall_pct == 3 and job.sampling_pct == 0
    job.phase, job.completed_runs = "phase_sampling", 1
    assert job.overall_pct == 8 + 24 and "总进度 32%" in _page(job)
    job.phase, job.completed_runs = "phase_verify", 3
    job.verify_total, job.verify_done = 10, 5
    assert job.overall_pct == 86
    job.phase, job.judge_total, job.judge_done = "phase_judging", 4, 2  # 高档的 AI 裁判复核段(S9.3)
    assert job.overall_pct == 94 and "AI 裁判复核" in _page(job) and "已判 2/4 条回答" in _page(job)
    job.phase = "phase_scoring"
    assert job.overall_pct == 97
    job.state, job.phase = JobState.DONE, "phase_done"
    assert job.overall_pct == 100 and job.sampling_pct == 100


def test_between_sampling_and_verification_the_page_says_so():
    now = datetime.now(UTC)
    job = _running_job(now)  # 提问完了、核对还没开始
    page = _page(job)
    assert "提问已全部完成" in page and "下一步核对 AI 引用的网页" in page and "预计还需约" not in page
    assert job.verify_remaining_minutes(now, 900) is None
    job.verify_started_at = now - timedelta(minutes=20)  # 超过预算:剩余 0,不为负
    assert job.verify_remaining_minutes(now, 900) == 0.0


def test_a_failed_job_does_not_keep_counting_down_the_page_check():
    """审核发现(2026-10-14):核对中途失败的任务,任务页还挂着「最多再等 N 分钟」倒计时;改成只有在跑才显示。"""
    now = datetime.now(UTC)
    job = _running_job(now)
    job.verify_started_at = now - timedelta(minutes=2)
    job.verify_total, job.verify_done, job.verify_stage = 10, 4, "fetch"
    job.state, job.phase = JobState.FAILED, "phase_failed"
    page = _page(job)
    assert "最多再等" not in page and "已开 4/10 个" not in page
    assert "预计还需约" not in page and "下一步核对" not in page
