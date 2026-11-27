"""抽取层:JSON-LD / microdata / 链接 / 可见与隐藏文字 / 日期 / robots / sitemap。"""

from __future__ import annotations

import pytest

from butter.contracts import FetchedPage
from butter.extract.dates import find_labeled_dates, to_iso_date
from butter.extract.html import extract_page
from butter.extract.jsonld import parse_block
from butter.extract.robots import blocks_everything, parse_robots, rules_allow
from butter.extract.sitemap import parse_sitemap
from conftest import NOW, page


def html_page(html: str, url: str = "https://x.test/p", headers: dict | None = None) -> FetchedPage:
    return FetchedPage(url=url, final_url=url, role="given", status=200, content_type="text/html", html=html,
                       headers=headers or {}, fetched_at=NOW)


def test_jsonld_numbers_keep_their_original_text(acme_extracts):
    product = next(n for n in page(acme_extracts, "/products/classic").nodes if n.types == ["Product"])
    assert product.props["offers"]["price"] == "49.00", "49.00 不能变成 49.0"


def test_broken_jsonld_is_reported_not_repaired(acme_extracts):
    blocks = page(acme_extracts, "/blog/boil-time").jsonld_blocks
    assert blocks[0].error is None and blocks[1].error and "解析失败" in blocks[1].error
    assert parse_block("", 0).error


def test_microdata_becomes_the_same_node_shape(acme_extracts):
    node = next(n for n in page(acme_extracts, "/about").nodes if n.source_kind == "microdata")
    assert node.types == ["Organization"] and node.props["name"] == "ACME Kettles Ltd"
    assert node.props["sameAs"] == "https://www.linkedin.com/company/acme-kettles"


def test_links_are_http_only(acme_extracts):
    hrefs = [l.href for l in page(acme_extracts, "test/").links]
    assert hrefs and all(h.startswith(("http://", "https://")) for h in hrefs)
    assert not any("javascript" in h for h in hrefs)


def test_hidden_text_classification(acme_extracts):
    home = page(acme_extracts, "test/")
    assert [h.technique for h in home.hidden_texts] == ["ui_state"]
    assert "newsletter" not in home.visible_text
    assert "made in small batches" in home.visible_text, "@media 里的 display:none 是有条件的,不算隐藏"
    about = page(acme_extracts, "/about")
    assert about.hidden_texts[0].technique == "suspicious" and "font-size:0" in about.hidden_texts[0].reason
    assert "Ignore all previous" not in about.visible_text
    assert any(a.carrier == "comment" and "Note to AI" in a.text for a in about.attr_texts)


@pytest.mark.parametrize("style,technique", [
    ("display:none", "ui_state"), ("visibility: hidden", "ui_state"), ("opacity:0", "ui_state"),
    ("text-indent:-9999px", "suspicious"), ("position:absolute;left:-10000px", "suspicious"),
    ("color:#fff;background-color:#fff", "suspicious"), ("height:0;overflow:hidden", "suspicious"),
    ("color:transparent", "suspicious"),
])
def test_inline_hiding_techniques(style, technique):
    ex = extract_page(html_page(f"<html><body><p>shown</p><div style='{style}'>secret words here</div></body></html>"))
    assert [h.technique for h in ex.hidden_texts] == [technique]
    assert "secret" not in ex.visible_text and "shown" in ex.visible_text


def test_things_that_are_not_hiding():
    ex = extract_page(html_page(
        "<html><body><p style='color:transparent;-webkit-background-clip:text;background-clip:text'>gradient title</p>"
        "<ul style='font-size:0'><li>menu item one</li></ul><p style='opacity:0.8'>faded</p></body></html>"))
    assert ex.hidden_texts == []
    assert "gradient title" in ex.visible_text and "menu item one" in ex.visible_text and "faded" in ex.visible_text


def test_scripts_are_never_text():
    ex = extract_page(html_page("<html><head><title>T</title></head><body><script>var secret='x'</script>"
                                "<noscript>enable js</noscript><p>hello</p></body></html>"))
    assert ex.visible_text == "hello" and ex.title == "T"


@pytest.mark.parametrize("raw,iso", [
    ("2026-03-05", "2026-03-05"), ("2026-03-05T10:00:00+08:00", "2026-03-05"), ("2026/3/5", "2026-03-05"),
    ("2026.03.05", "2026-03-05"), ("2026年3月5日", "2026-03-05"), ("March 5, 2026", "2026-03-05"),
    ("5 Mar 2026", "2026-03-05"), ("Thu, 05 Mar 2026 08:00:00 GMT", "2026-03-05"),
    ("2026-13-45", None), ("sometime soon", None), ("", None),
])
def test_to_iso_date(raw, iso):
    assert to_iso_date(raw) == iso


def test_only_labeled_dates_count_as_visible():
    found = find_labeled_dates("最后更新:2026年3月5日。我们成立于 1998-01-01。 Published on March 1, 2026")
    assert [(d, kind) for _, d, kind in found] == [("2026年3月5日", "modified"), ("March 1, 2026", "published")]


def test_dates_from_every_source(acme_extracts):
    kinds = {d.kind for d in page(acme_extracts, "/blog/boil-time").dates}
    assert {"jsonld_published", "jsonld_modified", "time_element", "visible", "sitemap_lastmod"} <= kinds
    ex = extract_page(html_page("<html><body>x</body></html>", headers={"last-modified": "Thu, 05 Mar 2026 08:00:00 GMT"}))
    assert [(d.kind, d.iso_date) for d in ex.dates] == [("http_last_modified", "2026-03-05")]


def test_robots_grouping_and_longest_match():
    parsed = parse_robots("User-agent: a\nUser-agent: b\nDisallow: /x\nAllow: /x/open\n\nUser-agent: *\nDisallow: /\n"
                          "Sitemap: https://x.test/s.xml\n")
    assert [g.user_agents for g in parsed.groups] == [["a", "b"], ["*"]]
    rules = parsed.groups_for("B")[0].rules
    assert rules_allow(rules, "/y") and not rules_allow(rules, "/x/closed") and rules_allow(rules, "/x/open/1")
    assert blocks_everything(parsed.star_rules()) and not blocks_everything(rules)
    assert parsed.sitemaps == ["https://x.test/s.xml"]
    assert rules_allow(parse_robots("User-agent: *\nDisallow: /*.pdf$").star_rules(), "/a.pdf") is False
    assert rules_allow(parse_robots("User-agent: *\nDisallow:").star_rules(), "/anything")


def test_sitemap_rejects_dtd_and_reads_index():
    assert parse_sitemap('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><urlset></urlset>').error
    index = parse_sitemap('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                          '<sitemap><loc>https://x.test/a.xml</loc></sitemap></sitemapindex>')
    assert index.children == ["https://x.test/a.xml"] and not index.entries
    assert parse_sitemap("<html><body>nope</body></html>").error
    assert parse_sitemap("not xml at all").error
