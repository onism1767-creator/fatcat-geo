"""后台作业:排队、进度、做完后建批次和卡、失败与中断怎么记;网页上起新运行。全部不联网、不调模型。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from butter import cards
from butter.contracts import JobEvent
from butter.jobs import JobManager, fold_jobs
from butter.web.app import WebSettings, create_app

BASE = "http://127.0.0.1:8700"
ORIGIN = {"origin": BASE}


def manager(tmp_path, snapshot, env=None, fetch=None) -> JobManager:
    return JobManager(tmp_path / "out", tmp_path / "workspace", env=env or {},
                      fetch=fetch or (lambda url, max_pages: snapshot), start_worker=False)


def client_for(tmp_path, jobs: JobManager) -> TestClient:
    settings = WebSettings(out_root=jobs.out_root, workspace=jobs.workspace, token="tok", env=dict(jobs.env), jobs=jobs)
    return TestClient(create_app(settings), base_url=BASE)


def test_a_run_job_goes_from_queued_to_done_and_creates_batch_and_cards(tmp_path, acme_snapshot):
    jobs = manager(tmp_path, acme_snapshot)
    job = jobs.submit_run("www.acme-kettles.test", max_pages=4, draft=True)
    assert job.state == "CONFIRMED" and job.active and job.params["url"] == "https://www.acme-kettles.test/"
    assert jobs.run_next() and not jobs.run_next()
    done = jobs.get(job.job_id)
    assert done.state == "DONE" and not done.active and done.result["pages"] == 4 and done.result["cost_usd"] == 0
    assert any("没有找到任何一家模型的 key" in n for n in done.result["notes"])       # 没有 key:照实说,不起草
    run_id, batch_id = done.result["run_id"], done.result["batch_id"]
    assert (tmp_path / "out" / "www.acme-kettles.test" / run_id / "change-package" / "manifest.json").is_file()
    view, problems = cards.load(tmp_path / "workspace")
    assert not problems and [b.batch.batch_id for b in view.batches] == [batch_id] == [f"b-{run_id}"]
    batch = view.batches[0]
    assert batch.batch.run_id == run_id and batch.phase == "已出包" and batch.cards_created == len(view.cards) >= 3
    assert all(c.validation == "假设" and not c.deployed for c in view.cards)
    states = [e.state for e in fold_events(tmp_path)]
    assert states == ["CONFIRMED", "RUNNING", "RUNNING", "DONE"]


def fold_events(tmp_path) -> list[JobEvent]:
    lines = (tmp_path / "workspace" / "events" / "jobs.jsonl").read_text(encoding="utf-8").splitlines()
    return [JobEvent.model_validate_json(line) for line in lines]


def test_bad_input_is_refused_before_queueing(tmp_path, acme_snapshot):
    jobs = manager(tmp_path, acme_snapshot)
    for url in ("javascript:alert(1)", "ftp://x.test", ""):
        with pytest.raises(ValueError):
            jobs.submit_run(url)
    with pytest.raises(ValueError):
        jobs.submit_run("https://x.test", max_pages=99)
    assert jobs.jobs() == {} and not jobs.run_next()


def test_a_failed_fetch_is_recorded_and_the_queue_keeps_going(tmp_path, acme_snapshot):
    calls = []

    def fetch(url, max_pages):
        calls.append(url)
        if len(calls) == 1:
            raise OSError("连不上这个网站")
        if len(calls) == 2:
            raise RuntimeError("没想到的错")
        return acme_snapshot

    jobs = manager(tmp_path, acme_snapshot, fetch=fetch)
    a, b, c = (jobs.submit_run("https://www.acme-kettles.test/") for _ in range(3))
    while jobs.run_next():
        pass
    assert jobs.get(a.job_id).state == "FAILED" and "连不上这个网站" in jobs.get(a.job_id).error
    assert jobs.get(b.job_id).state == "FAILED" and "RuntimeError" in jobs.get(b.job_id).error
    assert jobs.get(c.job_id).state == "DONE"


def test_jobs_left_running_are_marked_interrupted_on_restart(tmp_path, acme_snapshot):
    jobs = manager(tmp_path, acme_snapshot)
    job = jobs.submit_run("https://www.acme-kettles.test/")          # 排了队,但"服务"在跑之前就关了
    again = manager(tmp_path, acme_snapshot)                           # 重启
    restarted = again.get(job.job_id)
    assert restarted.state == "INTERRUPTED" and "中断" in restarted.progress and not restarted.active
    assert not again.run_next()                                        # 不会偷偷接着跑
    assert manager(tmp_path, acme_snapshot).get(job.job_id).state == "INTERRUPTED"   # 再重启不重复记
    assert [e.state for e in fold_events(tmp_path)] == ["CONFIRMED", "INTERRUPTED"]


def test_fold_keeps_first_params_and_last_state():
    events = [JobEvent(job_id="j", at="t1", state="CONFIRMED", progress="排队中", params={"url": "u"}),
              JobEvent(job_id="j", at="t2", state="RUNNING", progress="正在读"),
              JobEvent(job_id="j", at="t3", state="DONE", result={"run_id": "r"})]
    job = fold_jobs(events)["j"]
    assert (job.state, job.created_at, job.updated_at, job.params, job.result, job.progress) == \
        ("DONE", "t1", "t3", {"url": "u"}, {"run_id": "r"}, "正在读")


def test_secrets_never_reach_the_job_log(tmp_path, acme_snapshot):
    jobs = manager(tmp_path, acme_snapshot, env={"OPENAI_API_KEY": "very-secret"})
    jobs.submit_run("https://www.acme-kettles.test/", draft=False)
    jobs.run_next()
    assert "very-secret" not in (tmp_path / "workspace" / "events" / "jobs.jsonl").read_text(encoding="utf-8")


def test_new_run_from_the_browser_end_to_end(tmp_path, acme_snapshot):
    jobs = manager(tmp_path, acme_snapshot)
    client = client_for(tmp_path, jobs)
    home = client.get("/").text
    assert 'action="/runs"' in home and "生成改动包" in home and "没有找到任何一家 AI 的 key" in home
    posted = client.post("/runs", data={"token": "tok", "url": "www.acme-kettles.test", "max_pages": "4"},
                         headers=ORIGIN, follow_redirects=False)
    assert posted.status_code == 303 and posted.headers["location"].startswith("/jobs/")
    waiting = client.get(posted.headers["location"])
    assert 'http-equiv="refresh" content="3"' in waiting.text and "排队中" in waiting.text   # 进行中:页面自己刷新
    assert "看进度" in client.get("/").text
    jobs.run_next()
    done = client.get(posted.headers["location"])
    assert "http-equiv" not in done.text and "看结果和改动包" in done.text and "看这批改动" in done.text
    data = client.get(posted.headers["location"] + ".json").json()
    assert data["state"] == "DONE" and data["pages"] == 4
    run_page = client.get(f"/runs/{data['run_id']}")
    assert run_page.status_code == 200 and "看这批改动的进展" in run_page.text      # 运行页能跳到它的批次
    batch = client.get(f"/batches/{data['batch_id']}.json").json()
    assert batch["phase_text"] == "改动包已出" and len(batch["cards"]) >= 3
    assert all(c["state_text"] == "还没证实" for c in batch["cards"])
    assert "推荐改完隔 28 天测一次" in client.get(f"/batches/{data['batch_id']}").text
    assert client.get("/batches.json").json()["batches"][0]["batch_id"] == data["batch_id"]


def test_new_run_form_guards(tmp_path, acme_snapshot):
    jobs = manager(tmp_path, acme_snapshot, env={"ANTHROPIC_API_KEY": "k"})
    client = client_for(tmp_path, jobs)
    home = client.get("/").text
    assert "让模型帮着写几句文字" in home and "claude-opus-5-5" in home and "最多花约 $" in home   # 有 key:写明用哪家、最多花多少
    form = {"token": "tok", "url": "www.acme-kettles.test", "max_pages": "4"}
    assert client.post("/runs", data={**form, "token": "x"}, headers=ORIGIN).status_code == 403
    assert client.post("/runs", data=form).status_code == 403
    assert client.post("/runs", data={**form, "url": "javascript:alert(1)"}, headers=ORIGIN).status_code == 400
    assert client.post("/runs", data={**form, "max_pages": "99"}, headers=ORIGIN).status_code == 400
    assert jobs.jobs() == {}
    assert client.get("/jobs/nope").status_code == 404
    assert json.loads(client.get("/index.json").text)["draft_offer"]["model"] == "claude-opus-5-5"
