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


@respx.mock
def test_follows_chain_joins_relative_location_and_reads_headers_only():
    first = respx.get("https://redir.test/a").mock(return_value=_redirect("https://mid.test/b"))
    second = respx.get("https://mid.test/b").mock(return_value=_redirect("/final?x=1", status=301))
    last = respx.get("https://mid.test/final?x=1").mock(return_value=httpx.Response(200, html="<html>big page</html>"))
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
