"""抓取层:只抓该抓的,遇验证码即停,不冒充爬虫。全部走 MockTransport,不出本机。"""

from __future__ import annotations

import httpx
import pytest

from butter.fetch import BROWSER_UA, _decode, looks_like_challenge, normalize_site_url
from conftest import ACME, ACME_ROUTES, HOST, Recorder, fetch_with


def test_acme_crawl_stays_on_host_and_obeys_robots(acme_recorder, acme_snapshot):
    assert acme_snapshot.stop_reason == "completed"
    assert {r.url.host for r in acme_recorder.requests} == {HOST}
    paths = acme_recorder.paths
    assert paths[0] == "/" and "/robots.txt" in paths and "/sitemap.xml" in paths
    assert not any(p.startswith("/admin") for p in paths), "robots.txt 对 * 挡住的路径不该抓"
    assert "/pricing" not in paths, "带查询串的地址不抓"
    assert "/files/manual.pdf" not in paths, "非网页文件不抓"
    assert any("被该站 robots.txt" in n for n in acme_snapshot.notes)
    assert acme_snapshot.robots.state == "ok"
    assert all(e.loc.startswith(f"https://{HOST}/") for e in acme_snapshot.sitemap_entries), "别的主机的 sitemap 条目要丢掉"


def test_always_fixed_browser_ua_never_a_crawler(acme_recorder, acme_snapshot):
    agents = {r.headers["user-agent"] for r in acme_recorder.requests}
    assert agents == {BROWSER_UA}
    assert "Chrome/157.0.0.0" in BROWSER_UA
    assert "bot" not in BROWSER_UA.lower() and "compatible" not in BROWSER_UA.lower()
    assert acme_snapshot.user_agent == BROWSER_UA
    assert not any("cookie" in r.headers for r in acme_recorder.requests)


def test_challenge_on_first_page_stops_immediately():
    body = "<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"
    rec = Recorder(ACME_ROUTES, ACME, {"/": lambda r: httpx.Response(403, text=body, headers={"content-type": "text/html"})})
    snap = fetch_with(rec)
    assert snap.stop_reason == "challenge"
    assert rec.paths == ["/"], "遇到人机验证后不该再发任何请求,也不重试"
    assert any("人机验证" in n for n in snap.notes)


def test_challenge_mid_crawl_stops_the_rest():
    captcha = "<html><body><div class='g-recaptcha'></div>请完成验证</body></html>"
    rec = Recorder(ACME_ROUTES, ACME, {
        "/products/classic": lambda r: httpx.Response(200, text=captcha, headers={"content-type": "text/html"})})
    snap = fetch_with(rec)
    assert snap.stop_reason == "challenge"
    assert rec.paths[-1] == "/products/classic"
    assert "/about" not in rec.paths


def test_cf_mitigated_header_counts_as_challenge():
    assert looks_like_challenge(403, {"cf-mitigated": "challenge"}, "") is not None
    assert looks_like_challenge(200, {}, "<html>" + "正常内容 " * 5000 + "</html>") is None
    assert looks_like_challenge(403, {}, "<html><body>Forbidden</body></html>") is None, "普通的 403 不是验证码"
    contact = "<html><body><h1>Contact us</h1><p>" + "We answer within two working days. " * 20 + \
              "</p><form><div class='g-recaptcha'></div></form></body></html>"
    assert looks_like_challenge(200, {}, contact) is None, "正常页面上带验证码组件的表单不算被挡"
    assert looks_like_challenge(200, {}, "<html><script>var a='" + "x" * 3000 + "'</script><body>请完成验证</body></html>")
    assert looks_like_challenge(503, {}, contact) is not None, "被拒时出现验证标记就算"


def test_too_many_requests_stops():
    rec = Recorder(ACME_ROUTES, ACME, {"/about": lambda r: httpx.Response(429, text="slow down")})
    snap = fetch_with(rec)
    assert snap.stop_reason == "refused"
    assert rec.paths[-1] == "/about"


def test_off_host_redirect_is_not_followed():
    rec = Recorder(ACME_ROUTES, ACME, {
        "/": lambda r: httpx.Response(301, headers={"location": "https://elsewhere.example/landing"})})
    snap = fetch_with(rec)
    assert snap.stop_reason == "off_host"
    assert {r.url.host for r in rec.requests} == {HOST}
    assert any("elsewhere.example" in n for n in snap.notes)
    assert len(rec.requests) == 1, "给定页跳去别的主机:到此为止,连 robots.txt 也不再抓"
    assert any("请直接用这个网址重跑" in n and "https://elsewhere.example/landing" in n for n in snap.notes), \
        "真实运行里出过(yuwell.com → yuwell-eu.com):要告诉用户下一步怎么办"


def test_same_host_redirect_is_followed():
    rec = Recorder(ACME_ROUTES, ACME, {
        "/start": lambda r: httpx.Response(302, headers={"location": "/products/classic"})})
    snap = fetch_with(rec, url=f"https://{HOST}/start")
    given = snap.pages[0]
    assert given.status == 200 and given.final_url.endswith("/products/classic")
    assert any(p.role == "home" for p in snap.pages), "给的不是首页时要另抓首页"


def test_page_cap_and_time_budget():
    rec = Recorder(ACME_ROUTES, ACME)
    snap = fetch_with(rec, max_pages=2)
    assert len(snap.pages) == 2

    ticks = iter(range(0, 10_000, 40))  # 每问一次时间过去 40 秒
    rec = Recorder(ACME_ROUTES, ACME)
    client = httpx.Client(transport=httpx.MockTransport(rec))
    from butter.fetch import FetchLimits, fetch_site
    snap = fetch_site(f"https://{HOST}/", limits=FetchLimits(total_budget_s=90), client=client,
                      sleep=lambda _s: None, clock=lambda: float(next(ticks)))
    client.close()
    assert snap.stop_reason == "time_budget"
    assert len(snap.pages) < 5


@pytest.mark.parametrize("status,state", [(500, "unreadable"), (403, "unreadable"), (404, "missing"), (410, "missing")])
def test_robots_states(status, state):
    rec = Recorder(ACME_ROUTES, ACME, {"/robots.txt": lambda r: httpx.Response(status, text="x")})
    assert fetch_with(rec, max_pages=1).robots.state == state


def test_robots_that_is_really_a_web_page_counts_as_missing():
    rec = Recorder(ACME_ROUTES, ACME, {
        "/robots.txt": lambda r: httpx.Response(200, text="<!doctype html><html><body>Not found</body></html>",
                                                headers={"content-type": "text/html"})})
    robots = fetch_with(rec, max_pages=1).robots
    assert robots.state == "missing" and "不是规则文件" in (robots.note or "")


def test_no_retries_on_failure():
    def boom(request):
        raise httpx.ConnectError("refused", request=request)
    rec = Recorder(ACME_ROUTES, ACME, {"/": boom})
    snap = fetch_with(rec)
    assert snap.stop_reason == "error" and rec.paths == ["/"]


def test_normalize_site_url():
    assert normalize_site_url("example.com") == "https://example.com/"
    assert normalize_site_url("http://example.com/a#frag") == "http://example.com/a"
    for bad in ("javascript:alert(1)", "file:///etc/passwd", "ftp://example.com", ""):
        with pytest.raises(ValueError):
            normalize_site_url(bad)


def test_decode_handles_gbk_pages():
    raw = '<html><head><meta charset="gbk"><title>水壶</title></head></html>'.encode("gbk")
    assert "水壶" in _decode(raw, "text/html")
    assert "水壶" in _decode("水壶".encode("gb18030"), "text/html; charset=gb2312")
    assert "水壶" in _decode("水壶".encode("utf-8"), "")


def test_script_rendered_home_and_foreign_sitemap_are_explained():
    """真实运行里出过(yuwell-eu.com):首页是空壳、sitemap 里全是别的主机的地址,只抓到一页却没说为什么。"""
    shell = '<html><head><title>App</title></head><body><div id="app"></div><script src="/app.js"></script></body></html>'
    sitemap = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
               "<url><loc>https://www.elsewhere.example/a</loc></url><url><loc>https://www.elsewhere.example/b</loc></url></urlset>")
    rec = Recorder({}, ACME, {
        "/": lambda r: httpx.Response(200, text=shell, headers={"content-type": "text/html"}),
        "/robots.txt": lambda r: httpx.Response(200, text="User-agent: *\nDisallow:\n", headers={"content-type": "text/plain"}),
        "/sitemap.xml": lambda r: httpx.Response(200, text=sitemap, headers={"content-type": "application/xml"}),
    })
    snap = fetch_with(rec)
    assert snap.stop_reason == "completed" and rec.paths == ["/", "/robots.txt", "/sitemap.xml"]
    assert snap.sitemap_entries == []
    notes = "\n".join(snap.notes)
    assert "有 2 个地址,其中 2 个在别的主机上" in notes
    assert "没有找到可以接着抓的站内页面" in notes and "只看了这 1 页" in notes
