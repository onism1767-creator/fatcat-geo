"""撞上费用上限(用户 2026-11-03 定,小米真跑时查到):上限按预估比例分到各型号;已花费每存一条回答就更新;
一条腿一停任务页就提示;跑完因上限停了就弹窗问要不要补齐,建议的新上限算好。页面流零网络。"""

import threading

import pytest

from engine.models import SearchMode, StopReason
from engine.scheduler import StopDetail
from report import runner
from report.estimate import split_budget
from report.jobs import JobState
from tests.unit.test_report_page import finished_job, visible_text
from tests.unit.test_response_page import _client, _run


def _stop(n_attempts: int, reason: StopReason = StopReason.BUDGET_EXHAUSTED) -> StopDetail:
    return StopDetail(surface="s1", model="m1", search_mode=SearchMode.DEFAULT, stop_reason=reason,
                      n_attempts=n_attempts, n_valid=n_attempts, failure_counts={}, last_failure_message=None)


def test_split_budget_follows_the_estimate_and_falls_back_to_even():
    assert split_budget(10.0, {"a": 3.0, "b": 1.0}) == {"a": 7.5, "b": 2.5}
    assert split_budget(10.0, {"a": 0.0, "b": 0.0}) == {"a": 5.0, "b": 5.0}
    assert split_budget(None, {"a": 1.0}) == {"a": None}


def test_job_page_asks_whether_to_fill_the_rest_when_the_cap_stopped_a_model():
    """跑完才问(用户定):弹窗写哪家、问了几次、错误码;建议上限 = 已花 0.5 + 没问的 2 次 × $0.005 × 1.1,向上取到 0.6。"""
    job = finished_job()
    job.state, job.stop_details, job.spent_usd = JobState.PARTIAL, [_stop(1)], 0.5
    client = _client(job)
    html = client.get(f"/jobs/{job.job_id}?lang=zh").text
    text = visible_text(html)
    assert "<dialog open" in html and "BUDGET_EXHAUSTED" in html
    assert "S1 · m1 到了费用上限,问了 1/3 次。" in text and "要补齐吗" in text and "就看现在的报告" in text
    assert 'name="budget"' in html and 'value="0.6"' in html
    assert "reached the spending cap after 1/3 asks" in client.get(f"/jobs/{job.job_id}?lang=en").text


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

    sink = runner._ProgressSink(Store(), job, threading.Lock())  # type: ignore[arg-type]
    for cost in (0.02, 0.03):
        sink.insert_surface_run(_run("p", run_id="x", index=1).model_copy(update={"cost_usd": cost}))
    assert job.spent_usd == pytest.approx(0.05) and job.completed_runs == 2
