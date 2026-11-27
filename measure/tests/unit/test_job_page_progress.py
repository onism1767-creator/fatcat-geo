"""任务页的核对网页进度(S8.5,用户定 2026-10-14):提问完之后不再显示「预计还需约 0 分钟」,
改为核对网页自己的进度条与「最多再等 N 分钟」(按预算给上限);核对也算活动,不会被当成「卡住了」。零网络零数据库。"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from diagnosis.recommender import load_evidence_registry
from engine.models import BrandSpec, Prompt, PromptIntent, PromptOrigin, Target
from report.app import create_app
from report.jobs import DiagnosisJob, JobState, LegProgress
from tests.unit.test_job_error_page import DummyAdapter
from tests.unit.test_report_page import LOW


def _running_job(now: datetime) -> DiagnosisJob:
    job = DiagnosisJob(brand=BrandSpec(name="X", domains=["x.com"]), website="https://x.com",
                       category="C", locale="zh-CN", candidates=[], competitors_suggested=[])
    prompt = Prompt(text="q?", intent=PromptIntent.CATEGORY_DISCOVERY, origin=PromptOrigin.USER, locale="zh-CN")
    job.freeze(prompts=[prompt], competitors=[], targets=[Target(surface="s1", model="m1")], budget_usd=None, tier=LOW)
    job.state, job.phase = JobState.RUNNING, "phase_verify"
    job.run_started_at = job.sampling_started_at = now - timedelta(minutes=5)
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


def _sampling_job(now: datetime, *, budget: float | None, estimate: float | None, leg: dict) -> DiagnosisJob:
    job = _running_job(now)
    job.phase, job.total_runs, job.completed_runs = "phase_sampling", 20, leg["done"]
    job.budget_usd, job.estimate_usd, job.spent_usd = budget, estimate, leg["spent_usd"]
    job.legs = [LegProgress(slots=20, unit_usd=0.10, budget_usd=budget, **leg)]
    return job


def test_the_job_page_reestimates_from_this_checks_own_spend_and_warns_only_when_it_matters():
    """边跑边重估(用户 2026-11-18 定):一个型号自己问满 5 次起,任务页写
    「照目前的花费,这一单预计共约 $N(开跑前估的是 $M)」;
    会在问完之前到费用上限、或比开跑前估的贵出五成才提醒;只提醒,照常跑。"""
    now = datetime.now(UTC)
    # 均价 $0.30(开跑前估 $0.10):预计 1.5 + 15 × 0.3 = $6.00,是预估 $2.00 的 3 倍;没设上限
    pricey = dict(done=5, paid_runs=5, spent_usd=1.5)
    page = _page(_sampling_job(now, budget=None, estimate=2.0, leg=pricey))
    assert "照目前的花费,这一单预计共约 $6.00(开跑前估的是 $2.00)。" in page
    assert "这一单比开跑前估的贵:预计共约 $6.00,是开跑前估的 3.0 倍。 没有设费用上限,会一直问完。" in page  # noqa: E501
    en = _page(_sampling_job(now, budget=None, estimate=2.0, leg=pricey), "en")
    assert "At the current spend this check will cost about $6.00 in all (estimated $2.00 before it started)." in en
    assert "This check is costing more than estimated: about $6.00 in all, 3.0 times the estimate." in en
    assert "No spending cap is set, so it will run to the end." in en

    # 设了上限 $3:还发得出 ⌈1.5 ÷ 0.3⌉ = 5 次 → 问到 10/20 = 50% 左右就到上限;提醒说上限,不再重复说「比预估贵」
    capped = _page(_sampling_job(now, budget=3.0, estimate=2.0, leg=pricey))
    assert ("照这个速度,问到 50% 左右就会到费用上限 $3.0,问不完。"
            "到上限会自动停,已问的都保留,停下后可以选择补齐。") in capped
    assert "这一单比开跑前估的贵" not in capped and "没有设费用上限" not in capped
    assert "at around 50%" in _page(_sampling_job(now, budget=3.0, estimate=2.0, leg=pricey), "en")

    # 和预估差不多(1.2 倍):只有那一句,不提醒
    calm = _page(_sampling_job(now, budget=None, estimate=2.0, leg=dict(done=5, paid_runs=5, spent_usd=0.6)))
    assert "照目前的花费,这一单预计共约 $2.40(开跑前估的是 $2.00)。" in calm and "这一单比开跑前估的贵" not in calm
    # 旧单没有开跑前的预估:只报数
    plain = _page(_sampling_job(now, budget=None, estimate=None, leg=pricey))
    assert "照目前的花费,这一单预计共约 $6.00。" in plain and "开跑前估的" not in plain
    # 还没问满 5 次、或者已经不在提问阶段:不显示
    early = _page(_sampling_job(now, budget=None, estimate=2.0, leg=dict(done=4, paid_runs=4, spent_usd=1.2)))
    assert "照目前的花费" not in early
    later = _sampling_job(now, budget=None, estimate=2.0, leg=pricey)
    later.phase = "phase_verify"
    assert "照目前的花费" not in _page(later)


def _app_with(job: DiagnosisJob) -> TestClient:
    app = create_app(call_stats=lambda brand=None: [], adapters={"s1": DummyAdapter()}, dsn="postgresql://unused",
                     evidence=load_evidence_registry(), job_runner=lambda **kw: None)
    app.state.ctx.registry.add(job)
    return TestClient(app)


def test_the_warning_carries_a_raise_the_cap_button_and_a_stop_button_that_work_midway():
    """提醒里的两个按钮(用户 2026-11-18 定):会撞上限时给「把上限提高到 $N」和「现在停下」;
    只是比预估贵时只给「现在停下」。
    提高上限当场重分给还在问的型号;点了停下,页面换成「正在停下」,各型号下一次提问前停。"""
    now = datetime.now(UTC)
    pricey = dict(done=5, paid_runs=5, spent_usd=1.5)  # 预计 $6.00
    job = _sampling_job(now, budget=3.0, estimate=2.0, leg=pricey)
    client = _app_with(job)
    page = client.get(f"/jobs/{job.job_id}?lang=zh").text
    assert "把上限提高到 $6.7" in page and "现在停下" in page  # 6.0 × 1.1 向上取到 0.1
    assert f'action="/jobs/{job.job_id}/raise_cap?lang=zh"' in page and 'name="budget" value="6.7"' in page
    assert "Raise the cap to $6.7" in client.get(f"/jobs/{job.job_id}?lang=en").text

    # 比现在低的上限被挡回(只能往上调),什么也没变
    bad = client.post(f"/jobs/{job.job_id}/raise_cap?lang=zh", data={"budget": "1"}, follow_redirects=False)
    assert bad.status_code == 303 and "FORM_BAD_BUDGET" in bad.headers["location"]
    assert job.budget_usd == 3.0 and job.legs[0].budget_usd == 3.0
    ok = client.post(f"/jobs/{job.job_id}/raise_cap?lang=zh", data={"budget": "6.7"}, follow_redirects=False)
    assert ok.status_code == 303 and job.budget_usd == 6.7
    assert job.legs[0].budget_usd == pytest.approx(6.7)  # 只有一条腿在问:新上限全给它
    after = client.get(f"/jobs/{job.job_id}?lang=zh").text
    assert "上限 $6.7" in after and "就会到费用上限" not in after  # 够问完了:提醒收起

    assert job.stop_requested is False
    assert client.post(f"/jobs/{job.job_id}/stop?lang=zh", follow_redirects=False).status_code == 303
    assert job.stop_requested is True
    stopping = client.get(f"/jobs/{job.job_id}?lang=zh").text
    assert "正在停下:手头正在问的这几次回来后就不再提问,已问的都保留" in stopping and "把上限提高到" not in stopping


def test_only_a_stop_button_when_there_is_no_cap_and_nothing_happens_outside_the_asking_phase():
    now = datetime.now(UTC)
    pricey = dict(done=5, paid_runs=5, spent_usd=1.5)
    job = _sampling_job(now, budget=None, estimate=2.0, leg=pricey)
    client = _app_with(job)
    page = client.get(f"/jobs/{job.job_id}?lang=zh").text
    assert "这一单比开跑前估的贵" in page and "现在停下" in page and "把上限提高到" not in page
    client.post(f"/jobs/{job.job_id}/raise_cap?lang=zh", data={"budget": "9"}, follow_redirects=False)
    assert job.budget_usd is None  # 这一单本来没设上限:提高上限什么也不做

    job.phase = "phase_verify"  # 已经问完、在核对网页:两个动作都不生效
    client.post(f"/jobs/{job.job_id}/stop?lang=zh", follow_redirects=False)
    assert job.stop_requested is False
    done = _sampling_job(now, budget=3.0, estimate=2.0, leg=pricey)
    done.state = JobState.DONE
    client2 = _app_with(done)
    client2.post(f"/jobs/{done.job_id}/stop?lang=zh", follow_redirects=False)
    client2.post(f"/jobs/{done.job_id}/raise_cap?lang=zh", data={"budget": "9"}, follow_redirects=False)
    assert done.stop_requested is False and done.budget_usd == 3.0
