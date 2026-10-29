"""网页层的四道防线(审核 2026-10-27 黄 5 / 18 / 19 / 23):路由不在事件循环上做阻塞活;官网地址不是网址就不建任务;
跨站 POST 一律拒绝;报告快照读不动的已完成任务不给「看报告」。零网络零数据库。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from report.app import create_app
from report.forms import brand_from_form, website_host
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
    # 第二轮审核 B15:只有点、空段、结尾点、非英文域名、全角、协议、端口
    (".", None), ("...", None), ("a.", None), ("a..com", None), ("-a.com", None), ("aurora_crm.com", None),
    ("aurora-crm.com.", "aurora-crm.com"), ("例子.中国", "xn--fsqu00a.xn--fiqs8s"),
    ("ａｕｒｏｒａ－ｃｒｍ．ｃｏｍ", "aurora-crm.com"),
    ("ftp://aurora-crm.com", None), ("javascript://aurora-crm.com", None), ("aurora-crm.com:abc", None),
    ("HTTP://aurora-crm.com:8080/x", "aurora-crm.com"),
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


@pytest.fixture
def db_down(monkeypatch):
    """库连不上:直接让建连接抛 StorageUnavailable(真去连一个没人听的端口要等满 10 秒的连接超时)。"""
    from engine.storage import Storage, StorageUnavailable

    def refuse(self, dsn):
        raise StorageUnavailable("10 秒内连不上数据库: connection refused")

    monkeypatch.setattr(Storage, "__init__", refuse)


def _down_client() -> TestClient:
    app = create_app(call_stats=lambda: [], adapters={"s1": DummyAdapter()}, dsn="postgresql://unused",
                     evidence=REGISTRY, job_runner=lambda **kw: None, calibration=None,
                     category_check=lambda job: None)
    return TestClient(app)


def test_pages_say_the_database_is_down_instead_of_a_bare_500(db_down):
    """库连不上时,单条回答页、下载这类要查库的页面给人话页 + 下一步(第二轮审核 B13:原来裸 Internal Server Error)。"""
    client = _down_client()
    job = finished_job()
    client.app.state.ctx.registry.add(job)
    for lang, words in (("zh", ("连不上本机数据库", "pgdev.ps1", "这一页暂时打不开")),
                        ("en", ("Cannot reach the local database", "pgdev.ps1", "cannot open right now"))):
        page = client.get(f"/jobs/{job.job_id}/responses/r1?lang={lang}")
        assert page.status_code == 503, lang
        text = visible_text(page.text)
        assert all(w in text for w in words), (lang, text[:300])
    assert client.get(f"/jobs/{job.job_id}/download?lang=zh").status_code == 503


def test_database_dropping_mid_query_gets_the_same_plain_page(monkeypatch):
    """查到一半库断了(psycopg 的 OperationalError,不是一开始就连不上):同样给人话页。"""
    import psycopg

    from engine.storage import Storage

    def boom(self, run_id):
        raise psycopg.OperationalError("server closed the connection unexpectedly")

    monkeypatch.setattr(Storage, "__init__", lambda self, dsn: None)
    monkeypatch.setattr(Storage, "close", lambda self: None)
    monkeypatch.setattr(Storage, "run_by_id", boom)
    client = _down_client()
    job = finished_job()
    client.app.state.ctx.registry.add(job)
    page = client.get(f"/jobs/{job.job_id}/responses/r1?lang=zh")
    assert page.status_code == 503 and "server closed the connection" in visible_text(page.text)


def test_drafting_with_the_database_down_drops_the_draft_and_explains_on_the_home_page(db_down):
    client = _down_client()
    resp = client.post("/jobs?lang=zh", data={**FORM, "generate": "on"}, follow_redirects=False)
    assert resp.status_code == 303 and "error=WEB_DB_UNREACHABLE" in resp.headers["location"]
    assert client.app.state.ctx.registry.recent() == []
    home = client.get(resp.headers["location"])
    assert "连不上本机数据库" in visible_text(home.text)


def test_unknown_check_says_so_in_both_languages_with_the_same_word_as_the_pages():
    """英文界面把任务叫 check(Start a check / Check),找不到时也说 check,不冒出 Job(第二轮审核 B16)。"""
    client = _client()
    for path in ("/jobs/nope", "/jobs/nope/confirm", "/jobs/nope/report", "/jobs/nope/download"):
        resp = client.get(path)
        assert resp.status_code == 404, path
        assert resp.text == "Check not found / 任务不存在", path


@pytest.mark.parametrize("website, normalized", [
    ("aurora-crm.com.", "https://aurora-crm.com"), ("例子.中国/关于", "https://xn--fsqu00a.xn--fiqs8s/关于"),
    ("ａｕｒｏｒａ－ｃｒｍ．ｃｏｍ", "https://aurora-crm.com"), ("http://Aurora-CRM.com:8080/zh?a=1", "http://aurora-crm.com:8080/zh?a=1"),
])
def test_brand_domain_and_site_address_use_the_normalized_host(website, normalized):
    """官网名单里的域名和开跑时访问的网址都用规整后的主机名:结尾带点、中文、全角写法也能认出 AI 引用的官网页(B15)。"""
    brand, site = brand_from_form("极光CRM", "", website)
    assert site == normalized
    assert brand.domains == [website_host(website)]
