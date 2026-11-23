"""撞上费用上限(用户 2026-11-03 定,小米真跑时查到):上限按预估比例分到各型号;已花费每存一条回答就更新;
一条腿一停任务页就提示;跑完因上限停了就弹窗问要不要补齐,建议的新上限算好。页面流零网络。"""

import threading

import pytest

from engine.models import SearchMode, StopReason
from engine.scheduler import StopDetail
from report import runner
from report.estimate import split_budget
from report.jobs import JobState, LegProgress
from tests.unit.test_report_page import finished_job, visible_text
from tests.unit.test_response_page import _client, _run


def _stop(n_attempts: int, reason: StopReason = StopReason.BUDGET_EXHAUSTED) -> StopDetail:
    return StopDetail(surface="s1", model="m1", search_mode=SearchMode.DEFAULT, stop_reason=reason,
                      n_attempts=n_attempts, n_valid=n_attempts, failure_counts={}, last_failure_message=None)


def test_split_budget_follows_what_is_still_to_spend():
    assert split_budget(10.0, {"a": 3.0, "b": 1.0}) == {"a": 7.5, "b": 2.5}  # 开跑:按预估比例
    # 续跑:b 问完了(还要花 0)只保住已花的 1.5,提高的额度全给 a
    assert split_budget(10.0, {"a": 3.0, "b": 0.0}, {"a": 2.0, "b": 1.5}) == {"a": 8.5, "b": 1.5}
    assert split_budget(3.0, {"a": 0.0, "b": 0.0}, {"a": 2.0, "b": 1.5}) == {"a": 2.0, "b": 1.5}  # 没有还要花的
    assert split_budget(None, {"a": 1.0}) == {"a": None}


def test_job_page_asks_whether_to_fill_the_rest_when_the_cap_stopped_a_model():
    """跑完才问(用户定):弹窗写哪家、问了几次、错误码;建议上限 = 已花 0.5 + 没问的 2 次 × 「贵的时候」的单价
    (没实测就是声明价 $0.005;Q7 乙,和确认页同一个数、不再另留余量),向上取到 0.6;
    现有上限已经比这高(腿停在它那份、整单没花到上限)就填现有上限——上限只能往上调,填低了会被挡回来(小米真跑)。"""
    job = finished_job()
    job.state, job.stop_details, job.spent_usd = JobState.PARTIAL, [_stop(1)], 0.5
    job.budget_usd = 0.4
    client = _client(job)
    html = client.get(f"/jobs/{job.job_id}?lang=zh").text
    text = visible_text(html)
    assert "<dialog open" in html and "BUDGET_EXHAUSTED" in html
    assert "S1 · m1 到了费用上限,问了 1/3 次。" in text and "要补齐吗" in text and "就看现在的报告" in text
    assert 'name="budget"' in html and 'value="0.6"' in html
    assert "reached the spending cap after 1/3 asks" in client.get(f"/jobs/{job.job_id}?lang=en").text
    job.budget_usd = 1.0
    assert 'value="1.0"' in client.get(f"/jobs/{job.job_id}?lang=zh").text


def test_the_suggested_cap_uses_the_when_pricey_unit_price():
    """用户 2026-11-03 定 Q7 乙:弹窗建议值和确认页「贵的时候约 $N」同一个数——有实测时按最贵一成的用量 × 牌价,
    不是平常的均价再加一成。已花 0.5 + 没问的 2 次 × $0.064 = 0.628 → 向上取到 0.7(按平常单价 $0.027 只到 0.6)。"""
    from fastapi.testclient import TestClient

    from report.app import create_app
    from tests.unit.test_estimate_prices import CHEAP, HIGH, SHAPE, PricedAdapter, stat
    from tests.unit.test_report_page import REGISTRY

    stats = [stat(CHEAP, "zh-CN", 20, usage=SHAPE, usage_high=HIGH)]
    app = create_app(call_stats=lambda brand=None: stats, adapters={"priced": PricedAdapter()},
                     dsn="postgresql://unused", evidence=REGISTRY, job_runner=lambda **kw: None)
    job = finished_job()
    job.state, job.spent_usd, job.budget_usd = JobState.PARTIAL, 0.5, 0.4
    job.stop_details = [StopDetail(surface="priced", model=CHEAP, search_mode=SearchMode.DEFAULT,
                                   stop_reason=StopReason.BUDGET_EXHAUSTED, n_attempts=1, n_valid=1,
                                   failure_counts={}, last_failure_message=None)]
    app.state.ctx.registry.add(job)
    html = TestClient(app).get(f"/jobs/{job.job_id}?lang=zh").text
    assert "<dialog open" in html and 'value="0.7"' in html


def test_other_stops_get_no_dialog_and_a_running_job_gets_a_notice_instead():
    job = finished_job()
    job.state, job.stop_details = JobState.PARTIAL, [_stop(2, StopReason.FAILURE_RATE_EXCEEDED)]
    html = _client(job).get(f"/jobs/{job.job_id}?lang=zh").text
    assert "<dialog" not in html and "提前停的型号" in html
    job.state, job.stop_details = JobState.RUNNING, [_stop(1)]
    html = _client(job).get(f"/jobs/{job.job_id}?lang=zh").text
    assert "<dialog" not in html and "到了费用上限" in html and "其余型号还在继续" in html


def test_spent_grows_with_every_answer_stored():
    """原来一家问完才整笔加,页面看着像只花了一分钱(小米真跑:$0.0108 挂了 10 分钟)。"""
    job = finished_job()
    job.spent_usd, job.completed_runs = 0.0, 0

    class Store:
        def insert_surface_run(self, run) -> None:
            pass

    leg = LegProgress(slots=3, done=0, spent_usd=0.0, paid_runs=0, unit_usd=0.01, budget_usd=None)
    sink = runner._ProgressSink(Store(), job, threading.Lock(), leg)  # type: ignore[arg-type]
    for cost in (0.02, 0.03):
        sink.insert_surface_run(_run("p", run_id="x", index=1).model_copy(update={"cost_usd": cost}))
    assert job.spent_usd == pytest.approx(0.05) and job.completed_runs == 2
    # 这条腿自己的进度与花费也逐条跟着走(边跑边重估用,用户 2026-11-18 定)
    assert (leg.done, leg.paid_runs) == (2, 2) and leg.spent_usd == pytest.approx(0.05)
    # 没花钱的那次(失败行 $0)占进度、不算「真花了钱的次数」:这一单的均价 = 已花 ÷ 真花钱的次数,
    # 原来 $0 的失败也加 1,均价被拉低(第四轮审核;开场从库里数时本来就只数花了钱的)
    sink.insert_surface_run(_run("p", run_id="y", index=2).model_copy(update={"cost_usd": 0.0}))
    assert (leg.done, leg.paid_runs) == (3, 2) and leg.spent_usd == pytest.approx(0.05)
