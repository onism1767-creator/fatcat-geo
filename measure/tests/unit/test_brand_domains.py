"""官网名单自动发现(v0.4.26 S5;产品文档 15.2 D12,验收用例 T33–T38):主站自己承认的才算。

respx 模拟全部抓取(零真实网络)。平台清单读真实配置 config/platforms.yaml。
"""

from datetime import UTC, datetime
from xml.etree import ElementTree

import httpx
import pytest
import respx

from engine import brand_domains
from engine.brand_domains import (
    BrandDomainDiscovery,
    apply_user_choice,
    channel_match,
    load_platforms,
    parse_link_header,
    parse_sitemap,
)
from engine.models import BrandDomain, BrandDomainList, DomainEvidence, DomainStatus, DomainTier
from engine.official_domains import DomainCandidate

PLATFORMS = load_platforms()

HOMEPAGE = """<html><head>
<link rel="alternate" hreflang="zh-CN" href="https://www.brand.com.cn/">
<link rel="alternate" hreflang="en-US" href="https://www.brand.com/">
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"Organization",
 "url":"https://www.brand.com/","sameAs":["https://brand.tmall.com/","https://www.instagram.com/brandofficial","https://brand.co.jp/",
 "https://en.wikipedia.org/wiki/Brand","https://zh.wikipedia.org/wiki/Brand","https://www.crunchbase.com/organization/brand"]}]}</script>
<script type="application/ld+json">{not valid json</script>
</head><body>
<a href="https://www.brand.co.uk/">UK</a>
<a href="https://brand.pissedconsumer.com/">complaints</a>
<a href="https://www.youtube.com/brandchannel">YT</a>
<a href="/about">about</a>
</body></html>"""
LINK_HEADER = '<https://www.brand.de/>; rel="alternate"; hreflang="de-DE", </style.css>; rel="stylesheet"'
ROBOTS = "User-agent: *\nAllow: /\nSitemap: https://www.brand.com/sitemap.xml\n"
SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">
  <url><loc>https://www.brand.com/page</loc>
    <xhtml:link rel="alternate" hreflang="fr-FR" href="https://www.brand.fr/page"/>
    <xhtml:link rel="alternate" hreflang="en" href="https://www.brand.com/page"/>
  </url>
</urlset>"""
POINTS_BACK = '<html><head><link rel="alternate" hreflang="en" href="https://www.brand.com/"></head><body>cn</body></html>'
NO_BACK = "<html><body>nothing here</body></html>"


def _html(body: str, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8", **(headers or {})})


def _mock_brand_site() -> None:
    respx.get("https://brand.com/").mock(return_value=httpx.Response(301, headers={"Location": "https://www.brand.com/"}))
    respx.get("https://www.brand.com/").mock(return_value=_html(HOMEPAGE, {"Link": LINK_HEADER}))
    respx.get("https://www.brand.com/robots.txt").mock(return_value=httpx.Response(200, text=ROBOTS))
    respx.get("https://www.brand.com/sitemap.xml").mock(return_value=httpx.Response(200, text=SITEMAP))
    respx.get("https://www.brand.com.cn/").mock(return_value=_html(POINTS_BACK))
    respx.get("https://www.brand.de/").mock(return_value=_html(NO_BACK))
    respx.get("https://brand.co.jp/").mock(return_value=_html(NO_BACK))
    respx.get("https://www.brand.co.uk/").mock(side_effect=httpx.ConnectError("down"))
    respx.get("https://www.brand.fr/").mock(return_value=_html(NO_BACK))


def _discover() -> BrandDomainList:
    discovery = BrandDomainDiscovery(PLATFORMS)
    try:
        return discovery.discover("https://brand.com")
    finally:
        discovery.close()


@respx.mock
def test_primary_site_declarations_are_counted_with_evidence_and_channels_are_listed_apart():
    """T33 hreflang(HTML / 响应头 / sitemap)、T34 首页同名链接、A2 结构化数据 → 自动计入,每项附依据;
    T38 sameAs 与首页链接里的平台页面 → 官方渠道,按主机名 + 路径前缀单列;投诉站主体不同,不列。
    sameAs 里名字不同的参考页面(维基百科、企业名录)不是官网:不计入,只记一条说明(审核 2026-10-27 红 1)。"""
    _mock_brand_site()
    result = _discover()
    assert result.primary == "brand.com" and "tldextract" in result.public_suffix_list
    by_domain = {d.domain: d for d in result.domains}
    assert set(by_domain) == {"brand.com.cn", "brand.de", "brand.fr", "brand.co.jp", "brand.co.uk"}
    assert {d: e.tier for d, e in by_domain.items()} == {
        "brand.com.cn": DomainTier.A1, "brand.de": DomainTier.A1, "brand.fr": DomainTier.A1,
        "brand.co.jp": DomainTier.A2, "brand.co.uk": DomainTier.A3,
    }
    assert all(d.status is DomainStatus.CONFIRMED for d in result.domains)
    cn = by_domain["brand.com.cn"]
    assert [(e.kind, e.detail) for e in cn.evidence] == [("hreflang", "zh-CN"), ("back_link", None)]
    assert cn.bidirectional is True and cn.evidence[0].source == "https://www.brand.com/"
    assert by_domain["brand.de"].evidence[0].kind == "link_header" and by_domain["brand.de"].bidirectional is False
    assert by_domain["brand.fr"].evidence[0] == DomainEvidence(
        kind="sitemap_hreflang", source="https://www.brand.com/sitemap.xml", detail="fr-FR")
    assert by_domain["brand.co.jp"].evidence[0].detail == "sameAs"
    assert by_domain["brand.co.uk"].bidirectional is None  # 对方首页没打开:不知道指不指回,不写
    assert result.confirmed_domains[0] == "brand.com" and len(result.confirmed_domains) == 6
    channels = {c.match: c for c in result.official_channels}
    assert set(channels) == {"brand.tmall.com/", "www.instagram.com/brandofficial", "www.youtube.com/brandchannel"}
    assert channels["brand.tmall.com/"].platform == "天猫"
    assert channels["brand.tmall.com/"].evidence.kind == "jsonld_sameas"
    assert channels["www.youtube.com/brandchannel"].evidence.kind == "homepage_link"
    assert result.channel_prefixes == list(channels)
    assert result.candidate_fetches == 5
    assert result.notes == [  # 两个维基百科页面同一注册域名,只记一次
        DomainEvidence(kind="schema_other_name", source="https://www.brand.com/", detail="wikipedia.org"),
        DomainEvidence(kind="schema_other_name", source="https://www.brand.com/", detail="crunchbase.com"),
    ]


@respx.mock
def test_unreachable_homepage_leaves_only_the_primary_and_says_so():
    respx.get("https://brand.com/").mock(side_effect=httpx.ConnectError("down"))
    result = _discover()
    assert result.domains == [] and result.official_channels == []
    assert result.confirmed_domains == ["brand.com"]
    assert [(n.kind, n.detail) for n in result.notes] == [("homepage_unreachable", "unreachable")]


@respx.mock
def test_look_alikes_from_answers_are_hints_only_b1_or_b2():
    """T35 回答引用同名域名、主站无依据 → B1;T36 对方单方面指向主站 → B2;都只提示不计入;名单里已有的不再列。"""
    _mock_brand_site()
    respx.get("https://brand.cn/").mock(return_value=_html(NO_BACK))
    respx.get("https://brand.com.hk/").mock(return_value=_html(POINTS_BACK))
    discovery = BrandDomainDiscovery(PLATFORMS)
    try:
        before = discovery.discover("https://brand.com")
        after = discovery.look_alikes(before, [
            DomainCandidate(domain="brand.cn", count=3),
            DomainCandidate(domain="brand.com.hk", count=1),
            DomainCandidate(domain="brand.com.cn", count=9),  # 已是 A1,不重复列
        ])
    finally:
        discovery.close()
    added = {d.domain: d for d in after.domains if d.domain not in {x.domain for x in before.domains}}
    assert set(added) == {"brand.cn", "brand.com.hk"}
    assert added["brand.cn"].tier is DomainTier.B1 and added["brand.cn"].seen_count == 3
    assert [e.kind for e in added["brand.cn"].evidence] == ["seen"]
    assert added["brand.com.hk"].tier is DomainTier.B2
    assert [e.kind for e in added["brand.com.hk"].evidence] == ["seen", "one_way"]
    assert all(d.status is DomainStatus.UNCONFIRMED for d in added.values())
    assert after.confirmed_domains == before.confirmed_domains  # 提示不计入
    assert after.candidate_fetches == before.candidate_fetches + 2


def test_user_choice_confirms_b_and_removes_a_and_ignores_unknown():
    discovered = datetime(2026, 10, 13, tzinfo=UTC)
    original = BrandDomainList(primary="brand.com", discovered_at=discovered, public_suffix_list="test", domains=[
        BrandDomain(domain="brand.co.uk", tier=DomainTier.A3, status=DomainStatus.CONFIRMED,
                    evidence=[DomainEvidence(kind="homepage_link", source="https://www.brand.com/")]),
        BrandDomain(domain="brand.cn", tier=DomainTier.B1, status=DomainStatus.UNCONFIRMED,
                    evidence=[DomainEvidence(kind="seen", detail="3")], seen_count=3),
    ])
    assert original.user_changed is False
    changed = apply_user_choice(original, confirm=["brand.cn", "evil.example"], remove=["brand.co.uk", "brand.cn"])
    by_domain = {d.domain: d for d in changed.domains}
    assert by_domain["brand.cn"].status is DomainStatus.CONFIRMED
    assert by_domain["brand.cn"].evidence[-1].kind == "user_confirmed"
    assert by_domain["brand.co.uk"].status is DomainStatus.REMOVED
    assert by_domain["brand.co.uk"].evidence[-1].kind == "user_removed"
    assert changed.confirmed_domains == ["brand.com", "brand.cn"] and changed.user_changed is True
    assert apply_user_choice(original, confirm=["evil.example"], remove=[]) == original  # 不在名单里的一律不认


@respx.mock
def test_sitemap_and_candidate_homepage_budgets_are_bounded(monkeypatch):
    """抓取有上限(产品文档 15.2:sitemap 最多 3 个文件、候选首页最多 20 个):到顶就停,并在说明里写明。"""
    monkeypatch.setattr(brand_domains, "MAX_CANDIDATE_HOMEPAGES", 2)
    respx.get("https://brand.com/").mock(return_value=_html(
        '<html><head><link rel="alternate" hreflang="a" href="https://www.brand.com.cn/">'
        '<link rel="alternate" hreflang="b" href="https://www.brand.de/">'
        '<link rel="alternate" hreflang="c" href="https://www.brand.fr/"></head></html>'))
    respx.get("https://brand.com/robots.txt").mock(return_value=httpx.Response(200, text=(
        "Sitemap: https://brand.com/s1.xml\nSitemap: https://brand.com/s2.xml\n"
        "Sitemap: https://brand.com/s3.xml\nSitemap: https://brand.com/s4.xml\n")))
    index = ('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
             '<sitemap><loc>https://brand.com/child.xml</loc></sitemap></sitemapindex>')
    for name in ("s1", "s2", "s3"):
        respx.get(f"https://brand.com/{name}.xml").mock(return_value=httpx.Response(200, text=index))
    s4 = respx.get("https://brand.com/s4.xml").mock(return_value=httpx.Response(200, text=index))
    child = respx.get("https://brand.com/child.xml").mock(return_value=httpx.Response(200, text=index))
    cn = respx.get("https://www.brand.com.cn/").mock(return_value=_html(NO_BACK))
    de = respx.get("https://www.brand.de/").mock(return_value=_html(NO_BACK))
    fr = respx.get("https://www.brand.fr/").mock(return_value=_html(NO_BACK))
    result = _discover()
    assert s4.call_count == 0 and child.call_count == 0  # 只读了前 3 个 sitemap 文件
    assert cn.call_count == 1 and de.call_count == 1 and fr.call_count == 0  # 候选首页只开了 2 个
    assert {n.kind for n in result.notes} == {"sitemap_limit", "homepage_limit"}
    assert result.candidate_fetches == 2


def test_parsers_and_channel_match():
    assert parse_link_header(LINK_HEADER, "https://www.brand.com/") == [("de-DE", "https://www.brand.de/")]
    assert parse_link_header("", "https://www.brand.com/") == []
    children, hreflangs = parse_sitemap(
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        '<sitemap><loc> https://brand.com/a.xml </loc></sitemap></sitemapindex>')
    assert children == ["https://brand.com/a.xml"] and hreflangs == []
    assert channel_match("https://Brand.TMALL.com") == "brand.tmall.com/"
    assert channel_match("https://www.instagram.com/brandofficial?hl=en") == "www.instagram.com/brandofficial"
    with pytest.raises(ElementTree.ParseError):
        parse_sitemap("<not xml")
    assert any("tmall.com" in p.hosts for p in PLATFORMS)
