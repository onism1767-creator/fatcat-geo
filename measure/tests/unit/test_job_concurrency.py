"""同一单的并发请求(第二轮审核:路由改普通函数后跑在线程池里):连点两下只起一个执行器、只开一张再测单;
出题期间确认页显示等待、出题失败不撤已在跑的单;「重新生成报告」一律只重算不花钱。零网络零数据库。"""

from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from report.app import create_app
from report.jobs import JobState
from tests.unit.test_category_page import FORM, _client, _job_of
from tests.unit.test_report_page import REGISTRY, DummyAdapter, finished_job


def _app_with_runner(calls: list):
    def runner(*, job, replay, **_):
        calls.append((job.job_id, replay))

    app = create_app(call_stats=lambda brand=None: [], adapters={"s1": DummyAdapter()}, dsn="postgresql://unused",
                     evidence=REGISTRY, job_runner=runner, calibration=None, category_check=lambda job: None)
    return app


def test_two_simultaneous_confirms_start_one_runner(monkeypatch):
    """两个请求几乎同时到:把「查状态」和「改状态」之间的窗口放大到 0.3 秒,第二个请求也必须看到已经开跑。"""
    import report.routes as routes

    real = routes.confirm_choice

    def slow_choice(*args, **kwargs):
        time.sleep(0.3)
        return real(*args, **kwargs)

    monkeypatch.setattr(routes, "confirm_choice", slow_choice)
    calls: list = []
    app = _app_with_runner(calls)
    client = TestClient(app)
    created = client.post("/jobs?lang=zh", data=FORM, follow_redirects=False)
    job = _job_of(client, created.headers["location"])
    results: list[int] = []

    def confirm() -> None:
        with TestClient(app) as c:
            r = c.post(f"/jobs/{job.job_id}/confirm?lang=zh", data={
                "prompts_selected": ["0"], "targets_selected": ["s1::m1"], "tier": "low", "runs": "3"},
                follow_redirects=False)
            results.append(r.status_code)

    threads = [threading.Thread(target=confirm) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    time.sleep(0.2)  # 执行器桩跑在后台线程里
    assert results == [303, 303] and [c for c in calls if c[0] == job.job_id] == [(job.job_id, False)]


def test_regenerate_button_never_asks_the_ai_even_for_a_partial_job():
    """部分完成的单点「重新生成报告」(带 regenerate=1)只重算;点「接着跑」(不带)才补问(第二轮)。"""
    calls: list = []
    app = _app_with_runner(calls)
    client = TestClient(app)
    job = finished_job()
    job.state = JobState.PARTIAL
    app.state.ctx.registry.add(job)
    assert client.post(f"/jobs/{job.job_id}/resume?lang=zh", data={"regenerate": "1"},
                       follow_redirects=False).status_code == 303
    time.sleep(0.2)
    assert calls[-1] == (job.job_id, True)
    job.state = JobState.PARTIAL
    client.post(f"/jobs/{job.job_id}/resume?lang=zh", follow_redirects=False)
    time.sleep(0.2)
    assert calls[-1] == (job.job_id, False)
    job.state = JobState.PARTIAL  # 执行器桩不改状态:放回「部分完成」再看报告页
    report = client.get(f"/jobs/{job.job_id}/report?lang=zh").text
    assert 'name="regenerate" value="1"' in report  # 报告页的按钮带只重算标记


def test_confirm_page_waits_while_drafting_and_a_failed_draft_never_withdraws_a_running_job():
    client, _ = _client(None)
    created = client.post("/jobs?lang=zh", data=FORM, follow_redirects=False)
    job = _job_of(client, created.headers["location"])
    job.drafting = True
    page = client.get(f"/jobs/{job.job_id}/confirm?lang=zh")
    assert page.status_code == 200 and "AI 正在出题" in page.text and 'http-equiv="refresh"' in page.text
    assert client.post(f"/jobs/{job.job_id}/confirm?lang=zh", data={"targets_selected": ["s1::m1"]},
                       follow_redirects=False).headers["location"].endswith("/confirm?lang=zh")
    en = client.get(f"/jobs/{job.job_id}/confirm?lang=en").text
    assert "Drafting questions" in en
    # 出题失败的撤单只撤草稿:已经开跑的单不能撤
    from report.routes import _abandon_draft

    class Req:
        def __init__(self, app):
            self.app = app
            self.query_params = {}
            self.cookies = {}
            self.headers = {}

    job.state = JobState.RUNNING
    _abandon_draft(Req(client.app), job, "/")
    assert client.app.state.ctx.registry.get(job.job_id) is job
