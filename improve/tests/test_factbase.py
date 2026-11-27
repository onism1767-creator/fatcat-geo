"""事实库:每个值带来源、按固定优先级取并标冲突、空缺留空不猜、本机文件合并。"""

from __future__ import annotations

import json

from butter import sources
from butter.contracts import FactField, FactValue, FetchedPage, RobotsFetch, SiteSnapshot
from butter.extract.html import extract_site
from butter.factbase import T_LOCAL, T_META, T_STRUCT, T_TITLE, build_factbase, name_from_title, pick_single
from conftest import HOST, NOW


def all_fields(fb):
    fields: list[FactField] = [getattr(fb.entity, k) for k in type(fb.entity).model_fields]
    for p in fb.products:
        fields += [v for v in (getattr(p, k) for k in type(p).model_fields) if isinstance(v, FactField)]
    return fields


def fv(value, kind="jsonld"):
    return FactValue(value=value, source_url="https://x.test/", source_kind=kind, source_path="t")


def site(html: str, url: str = "https://shop.test/") -> tuple[SiteSnapshot, list]:
    page = FetchedPage(url=url, final_url=url, role="given", status=200, content_type="text/html", html=html, fetched_at=NOW)
    snap = SiteSnapshot(site_url=url, host="shop.test", origin="https://shop.test", fetched_at=NOW, user_agent="ua",
                        pages=[page], robots=RobotsFetch(url="https://shop.test/robots.txt", state="missing"))
    return snap, extract_site(snap.pages, [])


def build(snap, extracts, **kw):
    return build_factbase(snap, extracts, platforms=sources.load_platforms(), run_id="t", now=NOW, **kw)


def test_every_value_carries_a_source(acme_factbase):
    values = [v for f in all_fields(acme_factbase) for v in (*f.values, *f.alternatives, *f.candidates)]
    assert len(values) > 15
    assert all(v.source_url and v.source_kind for v in values)
    assert all(s.source_url for p in acme_factbase.products for s in p.specs)


def test_priority_and_conflict_flag(acme_factbase):
    name = acme_factbase.entity.name
    assert name.values[0].value == "Acme Kettles" and name.values[0].source_kind == "jsonld"
    assert name.conflict and {a.value for a in name.alternatives} == {"ACME Kettles Ltd", "Acme"}
    assert "已取 JSON-LD 的「Acme Kettles」" in name.conflict_note
    # 标题里带标语不算冲突:标题那一层只要包含了采用值就放过
    assert not any(a.source_kind == "title" for a in name.alternatives)
    desc = acme_factbase.entity.description
    assert desc.values[0].source_kind == "jsonld" and desc.conflict
    assert [a.source_kind for a in desc.alternatives] == ["og"], "meta description 与 JSON-LD 同文,不算另一种说法"


def test_pick_single_orders_by_tier_not_by_arrival():
    field = pick_single("name", [(T_LOCAL, fv("Local", "potato_brands")), (T_TITLE, fv("Title", "title")),
                                 (T_META, fv("Meta", "og")), (T_STRUCT, fv("Struct"))])
    assert field.values[0].value == "Struct"
    assert [a.value for a in field.alternatives] == ["Meta", "Title", "Local"]
    same = pick_single("name", [(T_STRUCT, fv("Acme")), (T_META, fv("  ACME ", "og"))])
    assert not same.conflict, "只是大小写和空白不同,不算冲突"
    assert pick_single("name", []).values == []


def test_same_as_adopted_vs_candidate(acme_factbase):
    same_as = acme_factbase.entity.same_as
    adopted = {v.value: v.source_kind for v in same_as.values}
    assert adopted["https://www.instagram.com/acmekettles/"] == "jsonld"
    assert adopted["https://www.youtube.com/@acmekettles"] == "link"
    assert "https://en.wikipedia.org/wiki/Acme_Kettles" in adopted, "站点自己写进 sameAs 的百科页照收"
    assert [c.value for c in same_as.candidates] == ["https://www.tiktok.com/@acmekettles"], "只在内页出现的只提示"
    everything = " ".join([*adopted, *(c.value for c in same_as.candidates)])
    assert "intent/tweet" not in everything and "facebook.com" not in everything and "partner.example.org" not in everything


def test_product_facts_come_from_structured_data(acme_factbase):
    (product,) = acme_factbase.products
    assert product.price.values[0].value == "49.00" and product.price.values[0].source_kind == "jsonld"
    assert product.price.conflict and product.price.alternatives[0].value == "59.00"
    assert product.currency.values[0].value == "USD" and not product.currency.conflict
    assert product.availability.values[0].value == "InStock"
    assert {(s.name, s.value) for s in product.specs} == {("Capacity", "1.7 L"), ("Material", "Stainless steel"), ("color", "Silver")}
    assert product.comparison_claims.values == []
    # 夹具站的 sitemap 里 6 条 lastmod 全是同一天 → 不可信,不当更新日期,所以只剩 JSON-LD 的 dateModified,也就没有冲突
    assert product.updated_date.values[0].value == "2026-05-01" and not product.updated_date.conflict
    assert product.updated_date.values[0].source_kind == "jsonld"
    assert any(g.code == "SITEMAP_LASTMOD_UNTRUSTED" and "全是同一天" in g.message for g in acme_factbase.gaps)


def test_gaps_are_left_empty_not_guessed(acme_factbase):
    assert acme_factbase.entity.category.values == []
    codes = {g.code for g in acme_factbase.gaps}
    assert {"NO_CATEGORY", "NO_COMPARISON"} <= codes


def test_no_price_is_ever_pulled_from_body_text():
    snap, ex = site("<html><head><title>Shop</title></head><body><h1>Kettle</h1>"
                    "<p>Now only $99.00! Capacity 1.7 L, weight 800 g. Better than Brand X.</p></body></html>")
    fb = build(snap, ex)
    assert fb.products == []
    assert "NO_PRODUCT_DATA" in {g.code for g in fb.gaps}
    assert "99" not in fb.model_dump_json().replace("shop.test", "")


def test_product_without_price_reports_the_gap():
    snap, ex = site('<html><head><title>Shop</title><script type="application/ld+json">'
                    '{"@context":"https://schema.org","@type":"Product","name":"Mug"}</script></head><body><h1>Mug</h1></body></html>')
    fb = build(snap, ex)
    assert fb.products[0].price.values == []
    messages = [g.message for g in fb.gaps if g.code == "PRODUCT_NO_PRICE"]
    assert messages and "未找到价格,商品类建议补" in messages[0]


def test_og_only_product_and_title_only_name():
    snap, ex = site('<html><head><title>Fine Mugs for Everyone</title><meta property="og:title" content="Blue Mug">'
                    '<meta property="product:price:amount" content="12.50"><meta property="product:price:currency" content="EUR">'
                    '</head><body><h1>Blue Mug</h1></body></html>')
    fb = build(snap, ex)
    assert fb.products[0].price.values[0].source_kind == "og" and fb.products[0].currency.values[0].value == "EUR"
    assert fb.entity.name.values[0].source_kind == "title"
    assert "NAME_FROM_TITLE_ONLY" in {g.code for g in fb.gaps}


def test_name_from_title():
    assert name_from_title("Acme — Stovetop Kettles", "www.acme.com")[0] == "Acme"
    assert name_from_title("首页_肥猫科技官网", "feimao.cn") == ("首页_肥猫科技官网", "title(整句)")
    assert name_from_title("Kettles | Acme Kettles", "acme-kettles.test")[0] == "Acme Kettles"
    assert name_from_title("Coca-Cola", "coca-cola.com")[0] == "Coca-Cola"


def test_merge_potato_brands(tmp_path, acme_snapshot, acme_extracts):
    path = tmp_path / "brands.yml"
    path.write_text(
        "competitor_set_version: v1\nfocal_domain: acme-kettles.test\npeers:\n"
        "  - name: Acme Kettles Inc\n    aliases: [AcmeK]\n    ambiguous_aliases: [AK]\n    domains: [acme-kettles.test, acme-kettles.co.uk]\n"
        "  - name: Rival\n    aliases: []\n    domains: [rival.test]\n", encoding="utf-8")
    fb = build(acme_snapshot, acme_extracts, brands=sources.load_brands_yml(path))
    assert fb.merged_inputs[0].status == "merged"
    assert fb.entity.name.values[0].value == "Acme Kettles", "网站自己说的优先于本机文件"
    assert "Acme Kettles Inc" in {a.value for a in fb.entity.name.alternatives}
    aliases = {v.value: v.source_kind for v in fb.entity.aliases.values}
    assert aliases["AcmeK"] == "potato_brands" and "AK" not in aliases, "歧义别名不合并"
    assert "acme-kettles.co.uk" in {v.value for v in fb.entity.official_domain.values}
    assert "Rival" not in fb.model_dump_json()

    other = tmp_path / "other.yml"
    other.write_text("peers:\n  - name: Rival\n    domains: [rival.test]\n", encoding="utf-8")
    skipped = build(acme_snapshot, acme_extracts, brands=sources.load_brands_yml(other))
    assert skipped.merged_inputs[0].status == "skipped" and "Rival" not in skipped.entity.model_dump_json()


def test_merge_toast_brand_domains(tmp_path, acme_snapshot, acme_extracts):
    path = tmp_path / "brand-domains.json"
    path.write_text(json.dumps({
        "brand": {"name": "Acme Kettles", "aliases": ["艾克米水壶"], "domains": ["acme-kettles.test"], "official_channels": []},
        "list": {"primary": "acme-kettles.test", "discovered_at": "2026-11-01T00:00:00Z", "public_suffix_list": "x",
                 "domains": [{"domain": "acme-kettles.cn", "tier": "A1", "status": "confirmed", "evidence": []},
                             {"domain": "acme-fans.test", "tier": "B1", "status": "unconfirmed", "evidence": []}],
                 "official_channels": [{"match": "acme.tmall.com/", "platform": "天猫", "kind": "marketplace_store",
                                        "evidence": {"kind": "sameAs"}}],
                 "notes": [], "candidate_fetches": 0}}, ensure_ascii=False), encoding="utf-8")
    fb = build(acme_snapshot, acme_extracts, brand_domains=sources.load_brand_domains_json(path))
    assert fb.merged_inputs[0].status == "merged"
    domains = fb.entity.official_domain
    assert "acme-kettles.cn" in {v.value for v in domains.values}
    assert "acme-fans.test" in {c.value for c in domains.candidates}, "未确认的域名只提示"
    assert "https://acme.tmall.com/" in {v.value for v in fb.entity.same_as.values}
    assert "艾克米水壶" in {v.value for v in fb.entity.aliases.values}


def test_pages_and_host(acme_factbase):
    assert acme_factbase.host == HOST
    assert [p.url.rsplit("/", 1)[-1] for p in acme_factbase.pages] == ["", "classic", "about", "boil-time"]


def test_canonical_on_another_host_is_flagged_for_a_human():
    """真实运行里出过(yuwell-eu.com 首页的 canonical 写的是 yuwell.com):官网域名按给的网址填,但要提醒人确认。"""
    from butter.contracts import FetchedPage, RobotsFetch, SiteSnapshot
    from butter.extract.html import extract_site
    from butter.render import factbase_md

    html = ('<html><head><title>Shop</title><link rel="canonical" href="https://www.other-brand.test/en">'
            '<meta property="og:site_name" content="Shop"></head><body><h1>Shop</h1></body></html>')
    page = FetchedPage(url="https://www.shop.test/", final_url="https://www.shop.test/", role="given", status=200,
                       content_type="text/html", html=html, fetched_at=NOW)
    snap = SiteSnapshot(site_url="https://www.shop.test/", host="www.shop.test", origin="https://www.shop.test",
                        fetched_at=NOW, user_agent="ua", pages=[page],
                        robots=RobotsFetch(url="https://www.shop.test/robots.txt", state="missing"))
    fb = build_factbase(snap, extract_site(snap.pages, []), platforms=sources.load_platforms(), run_id="t", now=NOW)
    gap = next(g for g in fb.gaps if g.code == "CANONICAL_OTHER_HOST")
    assert "https://www.other-brand.test/en" in gap.message
    assert fb.entity.official_domain.values[0].value == "shop.test", "不替用户改官网域名"
    assert "CANONICAL_OTHER_HOST" in factbase_md(fb)
    same = page.model_copy(update={"html": html.replace("www.other-brand.test", "shop.test")})
    fb2 = build_factbase(snap.model_copy(update={"pages": [same]}), extract_site([same], []),
                         platforms=sources.load_platforms(), run_id="t", now=NOW)
    assert not any(g.code == "CANONICAL_OTHER_HOST" for g in fb2.gaps), "www 有无算同一个站"


def test_brand_domains_keeps_every_counted_domain(tmp_path):
    """Toast 的 brand.domains 是"计入的官网,主站在前";主站不在 list.domains 里,list 为空时原来只读到主站一个。"""
    import json

    from butter.sources import load_brand_domains_json

    path = tmp_path / "brand-domains.json"
    path.write_text(json.dumps({"brand": {"name": "Acme", "domains": ["acme.test", "acme-shop.test", "acme.cn"]},
                                "list": None}), encoding="utf-8")
    loaded = load_brand_domains_json(path)
    assert loaded.primary == "acme.test" and loaded.confirmed_domains == ["acme-shop.test", "acme.cn"]
    path.write_text(json.dumps({
        "brand": {"name": "Acme", "domains": ["acme.test", "acme-shop.test", "acme.cn"]},
        "list": {"primary": "acme.test", "domains": [
            {"domain": "acme-shop.test", "status": "confirmed"}, {"domain": "acme-old.test", "status": "unconfirmed"},
            {"domain": "gone.test", "status": "removed"}]}}), encoding="utf-8")
    loaded = load_brand_domains_json(path)
    assert loaded.confirmed_domains == ["acme-shop.test", "acme.cn"]        # 不重复,去掉的不算
    assert loaded.unconfirmed_domains == ["acme-old.test"] and "acme.test" not in loaded.confirmed_domains
