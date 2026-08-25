"""引用独立核验测试:三档证据的「已核验」层。

respx 模拟全部抓取(零真实网络);证据落只追加库(共享测试库夹具)。
"""

import httpx
import psycopg
import pytest
import respx

from diagnosis.citation_verify import (
    CitationVerifier,
    brand_verified,
    is_soft_miss,
    normalize_url,
)
from engine.models import BrandSpec

BRAND = BrandSpec(name="AuroraCRM", aliases=["Aurora CRM"], domains=["aurora-crm.com"])

PAGE_WITH_BRAND = (
    "<html><body><main>AuroraCRM is a CRM used by small teams. "
    + "Long review content here. " * 20 + "</main></body></html>"
)
PAGE_WITHOUT_BRAND = (
    "<html><body><main>Sales Channel Not Found. Generic storefront text. "
    + "filler " * 30 + "</main></body></html>"
)
PAGE_WITH_ALIAS = (
    "<html><body><p>We compared Aurora CRM against others…"
    + "text " * 30 + "</p></body></html>"
)


def test_normalize_url_strips_fragment_keeps_www_and_query():
    assert normalize_url("https://WWW.Ex.com/a?x=1#frag") == "https://www.ex.com/a?x=1"
    assert normalize_url("https://ex.com") == "https://ex.com/"


@respx.mock
def test_three_tiers_and_append(storage):
    ok = respx.get("https://site-a.com/review").mock(
        return_value=httpx.Response(200, html=PAGE_WITH_BRAND))
    soft = respx.get("https://site-b.com/soft404").mock(
        return_value=httpx.Response(200, html=PAGE_WITHOUT_BRAND))
    dead = respx.get("https://site-c.com/gone").mock(return_value=httpx.Response(404))

    verifier = CitationVerifier(storage)
    evidence = verifier.verify(
        ["https://site-a.com/review#cite1", "https://site-a.com/review#cite2",  # 去 fragment 后同一 URL
         "https://site-b.com/soft404", "https://site-c.com/gone"],
        [BRAND],
    )
    verifier.close()

    assert ok.call_count == 1  # 规范化去重:同页两条引用只抓一次
    assert soft.call_count == 1 and dead.call_count == 1

    verified = evidence["https://site-a.com/review"]
    assert verified.reachable and brand_verified(verified, BRAND.name)

    soft_ev = evidence["https://site-b.com/soft404"]
    assert soft_ev.reachable and not brand_verified(soft_ev, BRAND.name)
    assert is_soft_miss(soft_ev, BRAND.name)  # 可达但正文无品牌 → 软 404 指纹

    dead_ev = evidence["https://site-c.com/gone"]
    assert not dead_ev.reachable and dead_ev.http_status == 404
    assert not brand_verified(dead_ev, BRAND.name)

    count = storage._conn.execute("SELECT count(*) FROM citation_evidence").fetchone()[0]
    assert count == 3  # 三个唯一 URL 各一条证据(含失败,不蒸发)


@respx.mock
def test_alias_counts_as_brand_found(storage):
    respx.get("https://site-d.com/alias").mock(
        return_value=httpx.Response(200, html=PAGE_WITH_ALIAS))
    verifier = CitationVerifier(storage)
    evidence = verifier.verify(["https://site-d.com/alias"], [BRAND])
    verifier.close()
    assert brand_verified(evidence["https://site-d.com/alias"], BRAND.name)


@respx.mock
def test_cache_reuse_across_verifier_instances(storage):
    route = respx.get("https://site-e.com/cached").mock(
        return_value=httpx.Response(200, html=PAGE_WITH_BRAND))
    first = CitationVerifier(storage)
    first.verify(["https://site-e.com/cached"], [BRAND])
    first.close()

    second = CitationVerifier(storage)  # 新实例:命中库缓存,不重抓
    evidence = second.verify(["https://site-e.com/cached"], [BRAND])
    second.close()
    assert route.call_count == 1
    assert brand_verified(evidence["https://site-e.com/cached"], BRAND.name)


@respx.mock
def test_cache_not_reused_for_unchecked_brand(storage):
    """缓存查过的品牌集合未覆盖本次品牌 → 必须重抓(没查过的缺席不可解读)。"""
    route = respx.get("https://site-f.com/page").mock(
        return_value=httpx.Response(200, html=PAGE_WITH_BRAND))
    CitationVerifier(storage).verify(["https://site-f.com/page"], [BRAND])
    other_brand = BrandSpec(name="OtherBrand")
    CitationVerifier(storage).verify(["https://site-f.com/page"], [other_brand])
    assert route.call_count == 2


@respx.mock
def test_fetch_error_recorded_not_evaporated(storage):
    respx.get("https://site-g.com/err").mock(side_effect=httpx.ConnectError("no route"))
    verifier = CitationVerifier(storage)
    evidence = verifier.verify(["https://site-g.com/err"], [BRAND])
    verifier.close()
    ev = evidence["https://site-g.com/err"]
    assert not ev.reachable and ev.error == "ConnectError"


def test_citation_evidence_is_append_only(storage):
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        storage._conn.execute("UPDATE citation_evidence SET reachable = true")
