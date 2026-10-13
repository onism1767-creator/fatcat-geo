"""网页核实(v0.4.26 S6.1;产品文档 D14 / D23):抽正文、看不到按原因记、边界有上限、同一网址复用。

respx 模拟全部抓取(零真实网络);时钟与 sleep 可注入,边界测试不真等。库表只追加另在 test_storage。
"""

import threading
import time
from datetime import UTC, datetime

import httpx
import psycopg
import pytest
import respx

from engine.detectors.page_mention import MentionPosition, page_mention
from engine.models import BrandSpec, PageCheck, PageFetchStatus
from engine.page_check import PageChecker, PageIndex, PageRequest, normalize_url

BRAND = BrandSpec(name="AuroraCRM", aliases=["Aurora CRM"], domains=["aurora-crm.com"])
FILLER = "This is a long paragraph of ordinary review text that makes the page look like a real article. " * 6
ARTICLE = (
    "<html><head><title>Best CRMs 2026</title></head><body>"
    "<nav><a href='/'>Home</a> AuroraCRM partners</nav>"
    f"<main><h1>Best CRMs for small teams</h1><p>{FILLER}</p>"
    f"<p>We also tried Aurora CRM for a month. {FILLER}</p></main>"
    "<footer>© footer AuroraCRM</footer></body></html>"
)
LEAD_ARTICLE = (
    "<html><head><title>Field notes</title></head><body>"
    f"<main><p>AuroraCRM review: a CRM for small teams. {FILLER}</p><p>{FILLER}</p></main></body></html>"
)
NO_BRAND = f"<html><head><title>Unrelated</title></head><body><main><p>{FILLER}</p></main></body></html>"
SHELL = "<html><head><title>App</title></head><body><div id='root'></div><script>window.x=1</script></body></html>"


class MemoryStore:
    def __init__(self) -> None:
        self.rows: list[PageCheck] = []

    def insert_page_check(self, check: PageCheck) -> None:
        self.rows.append(check)

    def recent_page_checks(self, normalized_urls, *, max_age_days=7):
        return {row.normalized_url: row for row in self.rows if row.normalized_url in normalized_urls}


def _html(body: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8"})


def _check(urls: list[str], store: MemoryStore | None = None, **kwargs) -> tuple[PageIndex, MemoryStore]:
    store = store or MemoryStore()
    checker = PageChecker(store, sleep=lambda s: None, **kwargs)
    try:
        return checker.check([PageRequest(url=u) for u in urls]), store
    finally:
        checker.close()


def test_normalize_url_strips_fragment_keeps_www_and_query():
    assert normalize_url("https://WWW.Ex.com/a?x=1#frag") == "https://www.ex.com/a?x=1"
    assert normalize_url("https://ex.com") == "https://ex.com/"


@respx.mock
def test_main_text_is_extracted_and_mentions_are_judged_by_position():
    """正文抽取去掉导航 / 页脚里的品牌名;命中位置:标题 → 首段 → 正文。"""
    respx.get("https://site-a.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://site-a.com/review").mock(return_value=_html(ARTICLE))
    respx.get("https://site-a.com/lead").mock(return_value=_html(LEAD_ARTICLE))
    respx.get("https://site-a.com/none").mock(return_value=_html(NO_BRAND))
    index, store = _check(["https://site-a.com/review#c1", "https://site-a.com/review#c2",
                           "https://site-a.com/lead", "https://site-a.com/none"])
    assert len(store.rows) == 3  # 去 fragment 后同一网址只开一次

    review = index.lookup("https://site-a.com/review")
    assert review is not None and review.status is PageFetchStatus.OK and review.text_source == "main"
    assert review.title == "Best CRMs 2026" and "footer AuroraCRM" not in (review.text or "")
    assert page_mention(review, BRAND) is not None
    assert page_mention(review, BRAND).position is MentionPosition.BODY  # 导航 / 页脚的品牌名不算,正文第二段才有

    lead = index.lookup("https://site-a.com/lead")
    assert lead is not None and page_mention(lead, BRAND).position is MentionPosition.LEAD

    none = index.lookup("https://site-a.com/none")
    assert none is not None and page_mention(none, BRAND).found is False and none.status is PageFetchStatus.OK


@respx.mock
def test_unobservable_pages_are_recorded_by_reason_not_guessed():
    respx.get("https://site-b.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private/\n"))
    respx.get("https://site-b.com/private/x").mock(return_value=_html(ARTICLE))
    respx.get("https://site-b.com/shell").mock(return_value=_html(SHELL))
    respx.get("https://site-b.com/doc.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF-1.7", headers={"content-type": "application/pdf"}))
    respx.get("https://site-b.com/blocked").mock(return_value=httpx.Response(403, text="denied"))
    respx.get("https://site-b.com/members").mock(return_value=httpx.Response(302, headers={"Location": "https://site-b.com/login?next=/members"}))
    respx.get("https://site-b.com/login?next=/members").mock(return_value=_html("<html><body>sign in</body></html>"))
    respx.get("https://site-b.com/gone").mock(return_value=httpx.Response(404))
    respx.get("https://site-b.com/down").mock(side_effect=httpx.ConnectError("no route"))
    index, store = _check([
        "https://site-b.com/private/x", "https://site-b.com/shell", "https://site-b.com/doc.pdf",
        "https://site-b.com/blocked", "https://site-b.com/members", "https://site-b.com/gone", "https://site-b.com/down",
    ])
    statuses = {url.rsplit("/", 1)[-1]: index.lookup(url).status for url in [
        "https://site-b.com/private/x", "https://site-b.com/shell", "https://site-b.com/doc.pdf",
        "https://site-b.com/blocked", "https://site-b.com/members", "https://site-b.com/gone", "https://site-b.com/down"]}
    assert statuses == {
        "x": PageFetchStatus.ROBOTS_DISALLOWED, "shell": PageFetchStatus.EMPTY_TEXT,
        "doc.pdf": PageFetchStatus.NOT_HTML,
        "blocked": PageFetchStatus.BLOCKED, "members": PageFetchStatus.LOGIN_REQUIRED,
        "gone": PageFetchStatus.FETCH_FAILED, "down": PageFetchStatus.FETCH_FAILED,
    }
    assert index.lookup("https://site-b.com/down").error == "ConnectError"
    assert index.lookup("https://site-b.com/gone").http_status == 404
    assert len(store.rows) == 7  # 失败也落库,不蒸发
    for url in ["https://site-b.com/shell", "https://site-b.com/blocked"]:
        assert page_mention(index.lookup(url), BRAND) is None  # 看不到 → 不可判定


@respx.mock
def test_own_site_pages_ignore_robots_while_third_party_respects_it():
    respx.get("https://brand.test/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n"))
    page = respx.get("https://brand.test/p").mock(return_value=_html(ARTICLE))
    store = MemoryStore()
    checker = PageChecker(store, sleep=lambda s: None)
    try:
        index = checker.check([PageRequest(url="https://brand.test/p", respect_robots=False),
                               PageRequest(url="https://brand.test/q", respect_robots=True)])
    finally:
        checker.close()
    assert index.lookup("https://brand.test/p").status is PageFetchStatus.OK and page.call_count == 1
    assert index.lookup("https://brand.test/q").status is PageFetchStatus.ROBOTS_DISALLOWED


@respx.mock
def test_recent_checks_are_reused_and_budget_stops_further_fetches():
    """7 天内同一网址复用不重抓;时间预算到顶,剩下的记「没来得及开」(不在 attempted 里)。"""
    respx.get("https://site-c.com/robots.txt").mock(return_value=httpx.Response(404))
    cached = respx.get("https://site-c.com/cached").mock(return_value=_html(ARTICLE))
    first, store = _check(["https://site-c.com/cached"])
    assert cached.call_count == 1
    second, _ = _check(["https://site-c.com/cached"], store)
    assert cached.call_count == 1 and second.lookup("https://site-c.com/cached") is not None

    ticks = iter([0.0, 0.0, 100.0, 100.0, 100.0, 100.0, 100.0])  # 第一个网址派出后时钟直接过了预算
    clock = lambda: next(ticks, 100.0)  # noqa: E731
    respx.get("https://site-d.com/robots.txt").mock(return_value=httpx.Response(404))
    one = respx.get("https://site-d.com/1").mock(return_value=_html(ARTICLE))
    two = respx.get("https://site-e.com/2").mock(return_value=_html(ARTICLE))
    index, _ = _check(["https://site-d.com/1", "https://site-e.com/2"], budget_s=10.0, concurrency=1, clock=clock)
    assert one.call_count == 1 and two.call_count == 0
    assert index.not_attempted("https://site-e.com/2") and not index.not_attempted("https://site-d.com/1")


@respx.mock
def test_same_host_is_serialized_with_a_gap_and_priority_orders_the_queue():
    """同一网站同时只开 1 个、开完隔一段再开下一个;优先级小的先开(被引 0 先于搜到 1)。真时钟:间隔要真等。"""
    respx.get("https://site-f.com/robots.txt").mock(return_value=httpx.Response(404))
    spans: list[tuple[str, float, float]] = []
    lock = threading.Lock()
    for name in ("a", "b", "c"):
        def serve(request, _n=name):
            started = time.monotonic()
            time.sleep(0.03)
            with lock:
                spans.append((_n, started, time.monotonic()))
            return _html(ARTICLE)
        respx.get(f"https://site-f.com/{name}").mock(side_effect=serve)
    store = MemoryStore()
    checker = PageChecker(store, concurrency=3, host_gap_s=0.2)
    try:
        checker.check([
            PageRequest(url="https://site-f.com/a", priority=1), PageRequest(url="https://site-f.com/b", priority=0),
            PageRequest(url="https://site-f.com/c", priority=1),
        ])
    finally:
        checker.close()
    spans.sort(key=lambda span: span[1])
    assert spans[0][0] == "b"  # 被引(0)先于搜到(1)
    for (_, _, previous_end), (_, next_start, _) in zip(spans, spans[1:], strict=False):
        assert next_start >= previous_end + 0.2 - 0.02  # 同站不重叠,且隔够间隔
    assert len(store.rows) == 3


def test_page_check_table_is_append_only_and_latest_per_url(storage):
    older = PageCheck(url="https://x.test/a", normalized_url="https://x.test/a", status=PageFetchStatus.FETCH_FAILED,
                      error="ConnectError", fetcher_version=1, fetched_at=datetime(2026, 10, 12, tzinfo=UTC))
    newer = PageCheck(url="https://x.test/a", normalized_url="https://x.test/a", status=PageFetchStatus.OK,
                      http_status=200, title="T", text="AuroraCRM is here", text_source="main", text_chars=17,
                      fetcher_version=1, fetched_at=datetime(2026, 10, 13, tzinfo=UTC))
    storage.insert_page_check(older)
    storage.insert_page_check(newer)
    latest = storage.recent_page_checks(["https://x.test/a", "https://x.test/never"])
    assert set(latest) == {"https://x.test/a"} and latest["https://x.test/a"].status is PageFetchStatus.OK
    assert latest["https://x.test/a"].text == "AuroraCRM is here" and latest["https://x.test/a"].truncated is False
    assert storage.recent_page_checks([]) == {}
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        storage._conn.execute("UPDATE page_check SET text = NULL")
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        storage._conn.execute("DELETE FROM page_check")
