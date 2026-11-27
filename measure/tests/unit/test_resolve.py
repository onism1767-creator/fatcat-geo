"""引用链接还原跳转(v0.4.26 S4,C2):跟到终点、数跳数、有上限;失败如实记;按链接复用。

respx 模拟全部请求(零真实网络);存储用内存假件,库表的只追加另在 test_storage。
"""

from datetime import UTC, datetime, timedelta

import httpx
import respx

from engine.models import CitationResolution, ResolutionStatus
from engine.resolve import MAX_HOPS, RESOLVER_VERSION, ResolutionContext, UrlResolver


class MemoryStore:
    def __init__(self, prior: list[CitationResolution] | None = None) -> None:
        self.rows: list[CitationResolution] = list(prior or [])

    def insert_citation_resolution(self, resolution: CitationResolution) -> None:
        self.rows.append(resolution)

    def latest_citation_resolutions(self, urls: list[str]) -> dict[str, CitationResolution]:
        latest: dict[str, CitationResolution] = {}
        for row in sorted(self.rows, key=lambda r: r.resolved_at):
            if row.url in urls:
                latest[row.url] = row
        return latest


def _redirect(location: str, status: int = 302) -> httpx.Response:
    return httpx.Response(status, headers={"Location": location})


def _no_robots(*origins: str) -> None:
    """这些站没有 robots.txt(404 = 没有规则,全放行):请求被跳转指向的站之前要先查它的 robots。"""
    for origin in origins:
        respx.get(f"{origin}/robots.txt").mock(return_value=httpx.Response(404))


@respx.mock
def test_follows_chain_joins_relative_location_and_reads_headers_only():
    first = respx.get("https://redir.test/a").mock(return_value=_redirect("https://mid.test/b"))
    second = respx.get("https://mid.test/b").mock(return_value=_redirect("/final?x=1", status=301))
    last = respx.get("https://mid.test/final?x=1").mock(return_value=httpx.Response(200, html="<html>big page</html>"))
    _no_robots("https://mid.test")
    store = MemoryStore()
    resolver = UrlResolver(store)
    try:
        result = resolver.resolve(["https://redir.test/a", "https://redir.test/a"])  # 同链接只还原一次
    finally:
        resolver.close()
    [resolution] = result.values()
    assert resolution.status is ResolutionStatus.RESOLVED
    assert resolution.final_url == "https://mid.test/final?x=1" and resolution.hops == 2
    assert resolution.http_status == 200 and resolution.resolver_version == RESOLVER_VERSION
    assert first.call_count == second.call_count == last.call_count == 1
    assert store.rows == [resolution]  # 落库一条


@respx.mock
def test_not_redirected_too_many_hops_and_network_failure_are_recorded_not_guessed():
    respx.get("https://redir.test/plain").mock(return_value=httpx.Response(200))
    respx.get("https://redir.test/loop").mock(return_value=_redirect("https://redir.test/loop"))
    respx.get("https://redir.test/down").mock(side_effect=httpx.ConnectError("boom"))
    _no_robots("https://redir.test")
    store = MemoryStore()
    resolver = UrlResolver(store)
    try:
        result = resolver.resolve(["https://redir.test/plain", "https://redir.test/loop", "https://redir.test/down"])
    finally:
        resolver.close()
    plain, loop, down = (result[u] for u in ("https://redir.test/plain", "https://redir.test/loop",
                                            "https://redir.test/down"))
    assert plain.status is ResolutionStatus.NOT_REDIRECTED and plain.final_url is None and plain.http_status == 200
    assert loop.status is ResolutionStatus.TOO_MANY_HOPS and loop.hops == MAX_HOPS and loop.final_url is None
    assert down.status is ResolutionStatus.FETCH_FAILED and down.error == "ConnectError" and down.final_url is None
    assert len(store.rows) == 3  # 失败也落库,不蒸发


@respx.mock
def test_prior_resolved_rows_are_reused_and_failures_are_retried():
    fresh = CitationResolution(url="https://redir.test/cached", final_url="https://brand.test/p", hops=1,
                               status=ResolutionStatus.RESOLVED, http_status=200, resolver_version=1)
    stale = fresh.model_copy(update={"url": "https://redir.test/stale",
                                     "resolved_at": datetime.now(UTC) - timedelta(days=40)})
    failed = CitationResolution(url="https://redir.test/failed", hops=0, status=ResolutionStatus.FETCH_FAILED,
                                error="ConnectError", resolver_version=1)
    store = MemoryStore([fresh, stale, failed])
    cached_route = respx.get("https://redir.test/cached").mock(return_value=_redirect("https://brand.test/p"))
    respx.get("https://redir.test/stale").mock(return_value=_redirect("https://brand.test/q"))
    respx.get("https://brand.test/q").mock(return_value=httpx.Response(200))
    respx.get("https://redir.test/failed").mock(return_value=_redirect("https://brand.test/r"))
    respx.get("https://brand.test/r").mock(return_value=httpx.Response(404))
    _no_robots("https://brand.test")
    resolver = UrlResolver(store, max_age_days=30)
    try:
        result = resolver.resolve([fresh.url, stale.url, failed.url])
    finally:
        resolver.close()
    assert cached_route.call_count == 0 and result[fresh.url] is fresh  # 时效内的成功记录直接复用
    assert result[stale.url].final_url == "https://brand.test/q"  # 过期的重新还原
    retried = result[failed.url]
    assert retried.status is ResolutionStatus.RESOLVED and retried.final_url == "https://brand.test/r"
    assert retried.http_status == 404  # 终点是 404 也是终点:去向清楚了,可达性是别的问题
    assert len(store.rows) == 5  # 原 3 条 + 新 2 条


def test_context_only_applies_to_redirect_surfaces():
    context = ResolutionContext.from_resolutions(frozenset({"redirect_surface"}), {
        "https://redir.test/ok": CitationResolution(url="https://redir.test/ok", final_url="https://brand.test/x",
                                                    hops=1, status=ResolutionStatus.RESOLVED, resolver_version=1),
        "https://redir.test/bad": CitationResolution(url="https://redir.test/bad", hops=0,
                                                     status=ResolutionStatus.NOT_REDIRECTED, resolver_version=1),
    })
    assert context.final_url("redirect_surface", "https://redir.test/ok") == "https://brand.test/x"
    assert context.final_url("redirect_surface", "https://redir.test/bad") is None  # 还原失败 → 看不到
    assert context.final_url("redirect_surface", "https://redir.test/never-seen") is None
    assert context.final_url("plain_surface", "https://redir.test/bad") == "https://redir.test/bad"  # 非跳转仪器照原样
    assert ResolutionContext().final_url("anything", "https://a.test/") == "https://a.test/"


@respx.mock
def test_stops_before_a_destination_whose_robots_disallows_and_still_records_where_it_points():
    """请求被跳转指向的那一站之前先查它的 robots(用户 2026-10-29 定):不让抓就停,网址取自上一跳的跳转头照记为去向,
    不去请求它;读不到 robots(5xx)当全禁,同样停。第一站是平台给的跳转链本身,不查。robots.txt 自己跳转也要跟。"""
    respx.get("https://redir.test/a").mock(return_value=_redirect("https://closed.test/article"))
    closed_page = respx.get("https://closed.test/article").mock(return_value=httpx.Response(200))
    respx.get("https://closed.test/robots.txt").mock(
        return_value=_redirect("https://www.closed.test/robots.txt", status=301))  # http→https 这类跳转:要跟着读
    respx.get("https://www.closed.test/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /article\n"))
    respx.get("https://redir.test/b").mock(return_value=_redirect("https://broken.test/x"))
    broken_page = respx.get("https://broken.test/x").mock(return_value=httpx.Response(200))
    respx.get("https://broken.test/robots.txt").mock(return_value=httpx.Response(503))
    store = MemoryStore()
    resolver = UrlResolver(store)
    try:
        result = resolver.resolve(["https://redir.test/a", "https://redir.test/b"])
    finally:
        resolver.close()
    closed, broken = result["https://redir.test/a"], result["https://redir.test/b"]
    assert closed.status is ResolutionStatus.STOPPED_AT_ROBOTS and closed.final_url == "https://closed.test/article"
    assert closed.hops == 1 and closed.http_status == 302 and closed_page.call_count == 0
    assert broken.status is ResolutionStatus.STOPPED_AT_ROBOTS and broken_page.call_count == 0
    context = ResolutionContext.from_resolutions(frozenset({"s"}), result)  # 去向清楚:判定照用这个网址
    assert context.final_url("s", "https://redir.test/a") == "https://closed.test/article"
    again = UrlResolver(MemoryStore(store.rows))  # 时效内照样复用,不重新还原
    try:
        assert again.resolve(["https://redir.test/a"])["https://redir.test/a"] == closed
    finally:
        again.close()


@respx.mock
def test_a_malformed_redirect_location_is_recorded_not_raised():
    """跳转头给了个坏网址:还原记「失败」(InvalidURL),不让这一批还原崩(第二轮审核 B4)。"""
    respx.get("https://redir.test/bad").mock(return_value=_redirect("http://a.test:abc/p"))  # 端口不是数字:httpx 拒绝
    store = MemoryStore()
    resolver = UrlResolver(store)
    try:
        result = resolver.resolve(["https://redir.test/bad"])
    finally:
        resolver.close()
    bad = result["https://redir.test/bad"]
    assert bad.status is ResolutionStatus.FETCH_FAILED and bad.final_url is None
    assert bad.error in ("InvalidURL", "RemoteProtocolError")  # httpx 收包时就可能先拒绝,两种都是「去向看不到」
