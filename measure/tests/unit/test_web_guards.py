"""网页层的四道防线(审核 2026-10-27 黄 5 / 18 / 19 / 23):路由不在事件循环上做阻塞活;官网地址不是网址就不建任务;
跨站 POST 一律拒绝;报告快照读不动的已完成任务不给「看报告」。零网络零数据库。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from adapters.base import SurfaceMeta
from report.app import create_app
from report.forms import brand_from_form, website_host
from report.jobs import JobState
from tests.unit.test_report_page import REGISTRY, DummyAdapter, finished_job, visible_text

ROOT = Path(__file__).resolve().parents[2]
FORM = {"brand_name": "极光CRM", "aliases": "", "website": "aurora-crm.com", "category": "CRM",
        "questions": "问题一?", "generate": ""}


def _client() -> TestClient:
    app = create_app(call_stats=lambda brand=None: [], adapters={"s1": DummyAdapter()}, dsn="postgresql://unused",
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


def test_brand_name_or_category_of_only_spaces_is_refused_before_anything_is_spent():
    """只填空格原来能过,整单照跑照花钱、全是没提到(第三轮审核 A19)。"""
    client = _client()
    for field in ("brand_name", "category"):
        resp = client.post("/jobs?lang=zh", data={**FORM, field: "   "})
        assert resp.status_code == 200 and "不能只填空格" in visible_text(resp.text), field
    assert client.app.state.ctx.registry.recent() == []
    en = client.post("/jobs?lang=en", data={**FORM, "brand_name": " "})
    assert "left blank or just spaces" in visible_text(en.text)


def test_extra_address_bar_params_on_the_home_page_are_ignored_not_a_500():
    """地址栏多带的参数原来全当关键字传进错误句子,带 code= / request= 就整页 500(第三轮审核 A20)。"""
    client = _client()
    assert client.get("/?code=1&request=2&self=3&with_next_step=0").status_code == 200
    page = client.get("/?lang=zh&error=DRAFTING_FAILED&detail=boom&code=1&request=2&lang2=x")
    assert page.status_code == 200 and "boom" in visible_text(page.text)


def test_every_placeholder_a_redirect_puts_in_the_home_address_is_let_through():
    """首页只收白名单里的占位符:跳回首页的地址里拼的每个参数都得在名单里,不然那句错误就不显示了。"""
    import re

    from report.routes import HOME_ERROR_PARAMS

    source = (ROOT / "report" / "routes.py").read_text(encoding="utf-8")
    redirects = re.findall(r'f"/\?error=[^)]*', source)
    used = {name for chunk in redirects for name in re.findall(r"&(\w+)=", chunk)}
    assert used and used <= set(HOME_ERROR_PARAMS)


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
    app = create_app(call_stats=lambda brand=None: [], adapters={"s1": DummyAdapter()}, dsn="postgresql://unused",
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


def test_pages_may_not_be_framed_by_other_sites():
    """第四轮审核(点击劫持):别的网页用透明的框套住本工具、骗用户点「接着跑」,那次点击是本站页面发出的,
    跨站检查拦不住。每个回应都声明不许被套进框里,连两条拒绝的回应也带。"""
    client = _client()
    responses = [client.get("/"),
                 client.post("/jobs", data=FORM, headers={"origin": "http://evil.test"}),  # 跨站:403
                 client.get("/", headers={"host": "evil.test"})]  # 不是本机的主机名:400
    assert [r.status_code for r in responses] == [200, 403, 400]
    for resp in responses:
        assert resp.headers["x-frame-options"] == "DENY"
        assert resp.headers["content-security-policy"] == "frame-ancestors 'none'"


class _OneModelLeft(DummyAdapter):
    """清单里只剩 m-new:任务测过的 m1 已移出(工程原则 9)。"""

    def meta(self) -> SurfaceMeta:
        return super().meta().model_copy(
            update={"models": ["m-new"], "default_model": "m-new", "consumer_default_model": "m-new",
                    "main_model": "m-new", "est_cost_per_call_usd": {"m-new": 0.005}})


def _sync_client(monkeypatch, runs: list, adapters: dict) -> TestClient:
    """线程换成同步执行;执行器桩只记下被怎么叫(replay 与否)。"""

    class SyncThread:
        def __init__(self, *, target, kwargs, daemon):
            self._target, self._kwargs = target, kwargs

        def start(self):
            self._target(**self._kwargs)

    monkeypatch.setattr("report.launcher.threading.Thread", SyncThread)
    return TestClient(create_app(
        call_stats=lambda brand=None: [], adapters=adapters, dsn="postgresql://unused", evidence=REGISTRY,
        job_runner=lambda *, job, replay, **_: runs.append(replay), calibration=None, category_check=lambda job: None))


def test_resuming_a_job_whose_model_left_the_list_is_refused_plainly_but_recompute_still_runs(monkeypatch):
    """第四轮审核:真要接着问的单里有型号已移出清单,现在问不了——拦下、说清楚、不起执行器(原来起来就崩,
    部分完成的单被写成「出错」,报告打不开)。只用已有回答重新生成报告不受影响。"""
    runs: list = []
    client = _sync_client(monkeypatch, runs, {"s1": _OneModelLeft()})
    job = finished_job()
    job.state, job.phase = JobState.PARTIAL, "phase_partial"
    client.app.state.ctx.registry.add(job)
    resp = client.post(f"/jobs/{job.job_id}/resume?lang=zh", follow_redirects=False)
    assert resp.status_code == 303 and "error=RESUME_RETIRED_MODEL" in resp.headers["location"]
    assert runs == [] and job.state is JobState.PARTIAL  # 没起执行器,单子原样
    home = visible_text(client.get(resp.headers["location"]).text)
    assert "已经不在型号清单里,不能接着问:S1 · m1" in home and "修好后再测" in home
    en = client.post(f"/jobs/{job.job_id}/resume?lang=en", follow_redirects=False)
    assert "no longer on the model list" in visible_text(client.get(en.headers["location"]).text)
    client.post(f"/jobs/{job.job_id}/resume?lang=zh", data={"regenerate": "1"}, follow_redirects=False)
    assert runs == [True]  # 只重算:照常


def test_official_domain_choice_is_not_applied_when_the_report_cannot_be_recomputed(monkeypatch):
    """第四轮审核:原来先改名单、后查缺 key——缺 key 时名单和编号改了、报告却没重算,页面上名单是新的、数字是旧的。
    现在先查,不能重算就什么都不改。"""
    from datetime import UTC, datetime

    from engine.models import BrandDomain, BrandDomainList, DomainEvidence, DomainStatus, DomainTier

    runs: list = []
    client = _sync_client(monkeypatch, runs, {})  # 这单测过的 AI 现在没接入(key 被拿掉了)
    job = finished_job()
    job.adopt_domain_list(BrandDomainList(
        primary="aurora-crm.com", discovered_at=datetime(2026, 10, 13, tzinfo=UTC), public_suffix_list="test",
        domains=[BrandDomain(domain="aurora-crm.jp", tier=DomainTier.B1, status=DomainStatus.UNCONFIRMED,
                             evidence=[DomainEvidence(kind="seen", detail="5")], seen_count=5)],
    ))
    client.app.state.ctx.registry.add(job)
    before = (job.fingerprint, job.domain_list, list(job.brand.domains))
    resp = client.post(f"/jobs/{job.job_id}/official_domains?lang=zh", data={"confirm": ["aurora-crm.jp"]},
                       follow_redirects=False)
    assert resp.status_code == 303 and "error=RESUME_MISSING_AI" in resp.headers["location"]
    assert runs == [] and (job.fingerprint, job.domain_list, list(job.brand.domains)) == before
