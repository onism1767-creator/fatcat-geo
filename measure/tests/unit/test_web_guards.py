"""网页层的四道防线(审核 2026-10-27 黄 5 / 18 / 19 / 23):路由不在事件循环上做阻塞活;官网地址不是网址就不建任务;
跨站 POST 一律拒绝;报告快照读不动的已完成任务不给「看报告」。零网络零数据库。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from report.app import create_app
from report.forms import website_host
from tests.unit.test_report_page import REGISTRY, DummyAdapter, finished_job, visible_text

ROOT = Path(__file__).resolve().parents[2]
FORM = {"brand_name": "极光CRM", "aliases": "", "website": "aurora-crm.com", "category": "CRM",
        "questions": "问题一?", "generate": ""}


def _client() -> TestClient:
    app = create_app(call_stats=lambda: [], adapters={"s1": DummyAdapter()}, dsn="postgresql://unused",
                     evidence=REGISTRY, job_runner=lambda **kw: None, calibration=None,
                     category_check=lambda job: None)
    return TestClient(app)


def test_route_handlers_are_plain_functions_so_blocking_work_leaves_the_event_loop():
    """出题 1–2 分钟、品类核对、查库都是阻塞活;写成 async def 会让整个服务跟着排队(黄 5)。机械把守。"""
    assert "async def" not in (ROOT / "report" / "routes.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("website, host", [
    ("aurora-crm.com", "aurora-crm.com"), ("https://www.Aurora-CRM.com/zh", "www.aurora-crm.com"),
    (" aurora-crm.com ", "aurora-crm.com"),
    ("https://", None), ("x", None), ("", None), ("http://", None), ("aurora crm.com", None), ("http://[bad", None),
])
def test_website_host_only_accepts_something_that_looks_like_an_address(website, host):
    assert website_host(website) == host


def test_bad_website_is_refused_on_the_home_page_before_anything_is_spent():
    client = _client()
    for website in ("https://", "x"):
        resp = client.post("/jobs?lang=zh", data={**FORM, "website": website})
        assert resp.status_code == 200 and "官网地址看着不像一个网址" in visible_text(resp.text)
        assert client.app.state.ctx.registry.recent() == []
    en = client.post("/jobs?lang=en", data={**FORM, "website": "x"})
    assert "does not look like a web address" in visible_text(en.text)


def test_cross_site_posts_are_rejected_and_same_site_ones_pass():
    """建任务那条 POST 不要任务编号:别的网页发来的表单能替用户花钱出题,按 Origin 头拒绝(黄 19)。"""
    client = _client()
    evil = client.post("/jobs?lang=zh", data=FORM, headers={"origin": "http://evil.test"}, follow_redirects=False)
    assert evil.status_code == 403 and client.app.state.ctx.registry.recent() == []
    own = client.post("/jobs?lang=zh", data=FORM, headers={"origin": "http://testserver"}, follow_redirects=False)
    assert own.status_code == 303 and len(client.app.state.ctx.registry.recent()) == 1
    bare = client.post("/jobs?lang=zh", data=FORM, follow_redirects=False)  # 没有 Origin 头(不是浏览器):照常
    assert bare.status_code == 303


def test_finished_job_with_unreadable_report_offers_regenerate_not_view():
    """报告快照读不动(装回时清空 sampling)是设计好的场景:任务页给「重新生成报告」,报告页与下载回任务页(黄 23)。"""
    client = _client()
    job = finished_job()
    job.sampling = None
    client.app.state.ctx.registry.add(job)
    page = client.get(f"/jobs/{job.job_id}?lang=zh")
    assert page.status_code == 200
    assert "重新生成报告" in visible_text(page.text) and f"/jobs/{job.job_id}/report" not in page.text
    for path in ("report", "download"):
        resp = client.get(f"/jobs/{job.job_id}/{path}?lang=zh", follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"].endswith(f"/jobs/{job.job_id}?lang=zh")


def test_only_this_computers_host_names_are_served():
    """DNS 重绑定:别的网站把域名解析到 127.0.0.1,来源头与主机名一致,跨站检查放行——所以主机名本身要把关(第二轮 24)。"""
    from report.app import ALLOWED_HOSTS

    assert {"127.0.0.1", "localhost"} <= set(ALLOWED_HOSTS)
    app = _client().app
    for base in ("http://127.0.0.1:8600", "http://localhost:8600", "http://localhost"):
        assert TestClient(app, base_url=base).get("/").status_code == 200, base
    rebound = TestClient(app, base_url="http://attacker.test:8600")
    page = rebound.get("/")
    assert page.status_code == 400 and "本机" in page.text and "this computer" in page.text
    post = rebound.post("/jobs?lang=zh", data=FORM, headers={"origin": "http://attacker.test:8600"},
                        follow_redirects=False)
    assert post.status_code == 400 and app.state.ctx.registry.recent() == []  # 来源与主机名一致也不放行
