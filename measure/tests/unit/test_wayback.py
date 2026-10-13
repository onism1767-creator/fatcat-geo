"""互联网档案馆快照(S6.4):可用性接口 → 快照原文;没有快照 / 取不回 → None;限流 → ArchiveRateLimited(带 Retry-After)。"""

import httpx
import pytest
import respx

from crawler.fetch import FETCH_TIMEOUT, new_client
from crawler.wayback import ArchiveRateLimited, WaybackClient

PAGE = "https://news.test/a"
AVAIL = "https://archive.org/wayback/available?url=https%3A%2F%2Fnews.test%2Fa"
SNAP = "https://web.archive.org/web/20250102030405id_/https://news.test/a"


@pytest.fixture
def client():
    c = new_client(timeout=FETCH_TIMEOUT)
    yield c
    c.close()


def _available() -> httpx.Response:
    return httpx.Response(200, json={"url": PAGE, "archived_snapshots": {"closest": {
        "status": "200", "available": True, "timestamp": "20250102030405",
        "url": "http://web.archive.org/web/20250102030405/https://news.test/a"}}})


@respx.mock
def test_closest_snapshot_is_fetched_as_raw_html(client):
    respx.get(AVAIL).mock(return_value=_available())
    respx.get(SNAP).mock(return_value=httpx.Response(200, text="<html><body>AuroraCRM</body></html>",
                                                     headers={"content-type": "text/html"}))
    page = WaybackClient(client).fetch(PAGE)
    assert page is not None and page.timestamp == "20250102030405" and page.http_status == 200
    assert "AuroraCRM" in page.html and page.archive_url.endswith("/20250102030405/https://news.test/a")
    assert page.truncated is False


@respx.mock
def test_no_snapshot_or_unfetchable_snapshot_is_none(client):
    respx.get(AVAIL).mock(return_value=httpx.Response(200, json={"url": PAGE, "archived_snapshots": {}}))
    assert WaybackClient(client).fetch(PAGE) is None
    respx.get(AVAIL).mock(return_value=_available())
    respx.get(SNAP).mock(return_value=httpx.Response(404))
    assert WaybackClient(client).fetch(PAGE) is None
    respx.get(AVAIL).mock(side_effect=httpx.ConnectError("down"))
    assert WaybackClient(client).fetch(PAGE) is None


@respx.mock
def test_rate_limit_is_translated_with_retry_after(client):
    respx.get(AVAIL).mock(return_value=_available())
    respx.get(SNAP).mock(return_value=httpx.Response(429, headers={"Retry-After": "7"}))
    with pytest.raises(ArchiveRateLimited) as info:
        WaybackClient(client).fetch(PAGE)
    assert info.value.retry_after_s == 7.0
    respx.get(AVAIL).mock(return_value=httpx.Response(429))
    with pytest.raises(ArchiveRateLimited) as info:
        WaybackClient(client).fetch(PAGE)
    assert info.value.retry_after_s is None
