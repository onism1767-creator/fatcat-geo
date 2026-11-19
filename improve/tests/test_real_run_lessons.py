"""2026-11-12 第一次在有正文、有商品数据的真实站(eufy.com 扫地机器人商品页)上跑出来的五个问题,每个一条回归测试。

记录在 docs/待办.md 的 2026-11-12 条目和 docs/Butter架构草案-第2周.md 第 9 节。
"""

from __future__ import annotations

import json
from datetime import timedelta

import httpx

from butter import sources
from butter.audit import numeric_traceability, visible_date
from butter.contracts import FetchedPage, Platform, Question, QuestionBoard, RobotsFetch, SitemapEntry, SiteSnapshot
from butter.extract.html import extract_page, extract_site
from butter.extract.sitemap import lastmod_suspicion
from butter.factbase import build_factbase, looks_like_profile, strip_tracking
from butter.fetch import link_priority, skip_reason
from butter.generate.headings import build_headings, phrases, tokens
from conftest import ACME, NOW, Recorder, fetch_with

FILLER = "<p>" + "This paragraph is ordinary body copy about kettles and tea. " * 6 + "</p>"


def _html(body: str, title: str = "Page") -> str:
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


def _page(url: str, html: str) -> FetchedPage:
    return FetchedPage(url=url, final_url=url, role="home" if url.endswith(".test/") else "discovered", status=200,
                       content_type="text/html", html=html, headers={}, fetched_at=NOW)


def _snapshot(pages: list[FetchedPage], entries: list[SitemapEntry] = ()) -> SiteSnapshot:
    return SiteSnapshot(site_url="https://x.test/", host="x.test", origin="https://x.test", fetched_at=NOW,
                        user_agent="ua", pages=pages, robots=RobotsFetch(url="https://x.test/robots.txt", state="missing"),
                        sitemap_entries=list(entries))


# ── 一:题板对照要有连续词组,零散单词不算 ──


def test_phrases_are_adjacent_content_words_or_three_characters():
    assert "robot vacuum" in phrases("What is the best robot vacuum for homes with pets?")
    assert "robot vacuum" in phrases("Are self-emptying docks on robot vacuums worth the extra cost?"), "单复数归一"
    assert "血压计" in phrases("家用电子血压计怎么选") and "怎么选" not in phrases("家用电子血压计怎么选")
    assert tokens("vacuums") == tokens("vacuum")


def test_board_match_needs_a_shared_phrase(acme_factbase):
    """真实运行里:扫地机器人商品页因 home、options、available 三个词被配到「睡眠呼吸暂停筛查」。"""
    board = QuestionBoard(path="q.yml", prompt_version="v1", locale="en-US", questions=[
        Question(id="good", intent="discovery", text="Which stovetop kettle works on induction hobs?"),
        Question(id="loose", intent="discovery", text="Which classic novels are best for kettle lovers?"),
        Question(id="none", intent="discovery", text="What at-home sleep apnea testing options are available?"),
    ])
    files, items, _missing = build_headings(acme_factbase, board)
    report = json.loads(files["suggestions/titles.json"])
    by_id = {q["id"]: q for q in report["questions"]}
    assert by_id["good"]["coverage"] == "partial" and "stovetop kettle" in by_id["good"]["matched_phrases"]
    assert by_id["loose"]["coverage"] == "weak" and by_id["loose"]["overlap"].startswith("2/"), "classic、kettle 两个词重合但没有词组"
    assert by_id["none"]["coverage"] == "none"
    assert [s["based_on_question"] for s in report["suggestions"]] == ["good"]
    md = files["suggestions/titles.md"]
    assert "| loose |" not in md and "只和页面有零散单词重合" in md and "loose" in md
    assert all(i.entry_id != "EVIDENCE_TITLE_QUERY_MATCH_V1" or "stovetop kettle" in i.rationale for i in items)


# ── 二:抓取页选择:商品 / 分类优先,语言地区副本和政策类跳过 ──


def test_discovery_prefers_commerce_pages_and_skips_variants_and_policies():
    home = _html(
        '<a href="/blog/x">blog</a> <a href="/ca">Canada</a> <a href="/uk/">UK</a> <a href="/eu-de">DE</a> '
        '<a href="/policies/privacy-policy">privacy</a> <a href="/account/login">login</a> '
        '<a href="/products/a?ref=home_banner&utm_source=home">A</a> <a href="/collections/all">all</a> '
        '<a href="/about">about</a> <a href="/collections/all?page=2">page 2</a>', "Home")
    home = home.replace("<head>", '<head><link rel="alternate" hreflang="en-ca" href="/ca">'
                                  '<link rel="alternate" hreflang="en-gb" href="/uk/">')

    def serve(html: str):
        return lambda _r: httpx.Response(200, text=html, headers={"content-type": "text/html"})

    rec = Recorder({}, ACME, {"/": serve(home), "/blog/x": serve(_html(FILLER, "Blog")),
                              "/products/a": serve(_html(FILLER, "A")), "/collections/all": serve(_html(FILLER, "All")),
                              "/about": serve(_html(FILLER, "About"))})
    snap = fetch_with(rec)
    paths = [p for p in rec.paths if p not in ("/robots.txt", "/sitemap.xml")]
    assert paths == ["/", "/products/a", "/collections/all", "/blog/x", "/about"], \
        "商品、分类先抓;副本和政策页不抓;?ref= 这类跟踪参数去掉后照抓,?page=2 这种真查询串不抓"
    assert any("跳过了 3 个语言 / 地区副本页" in n and "2 个政策、账户" in n for n in snap.notes)
    assert snap.stop_reason == "completed"


def test_skip_and_priority_rules():
    assert skip_reason("/en-us", "x.test/en-us", set()) == "variant"
    assert skip_reason("/zh-hant-tw/", "k", set()) == "variant"
    assert skip_reason("/fr/products/a", "x.test/fr/products/a", {"x.test/fr/products/a"}) == "variant", "hreflang 备选"
    assert skip_reason("/de/cart", "k", set()) == "aux" and skip_reason("/terms-of-service", "k", set()) == "aux"
    assert skip_reason("/p/123", "k", set()) is None and skip_reason("/pages/privacy-first", "k", set()) is None
    assert link_priority("https://x.test/products/a") == 0 and link_priority("https://x.test/p/123") == 0
    assert link_priority("https://x.test/collections/all") == 1, "列表页排在商品页之后"
    assert link_priority("https://x.test/blog/robot-vacuums") == 2 and link_priority("https://x.test/") == 2


# ── 三:sameAs 只收账号主页;应用商店页只提示;去掉跟踪参数 ──


def test_profile_rule_rejects_content_pages():
    yt = Platform(name="YouTube", kind="social_account", hosts=["youtube.com", "youtu.be"])
    ig = Platform(name="Instagram", kind="social_account", hosts=["instagram.com"])
    x = Platform(name="X", kind="social_account", hosts=["x.com", "twitter.com"])
    fb = Platform(name="Facebook", kind="social_account", hosts=["facebook.com"])
    assert looks_like_profile("https://www.youtube.com/@eufy", yt)
    assert looks_like_profile("https://www.youtube.com/channel/UCabc", yt)
    assert not looks_like_profile("https://www.youtube.com/shorts/x7yQkzD6Gdk", yt), "一条视频不是账号主页"
    assert not looks_like_profile("https://www.youtube.com/embed/x7yQkzD6Gdk", yt)
    assert looks_like_profile("https://www.instagram.com/eufylife/", ig)
    assert not looks_like_profile("https://www.instagram.com/reel/abc/", ig)
    assert looks_like_profile("https://twitter.com/eufyofficial", x)
    assert not looks_like_profile("https://twitter.com/eufyofficial/status/123", x)
    assert looks_like_profile("https://www.facebook.com/EufyOfficial", fb)
    assert looks_like_profile("https://www.facebook.com/profile.php?id=123", fb)
    assert not looks_like_profile("https://www.facebook.com/groups/eufyde", fb), "小组不是品牌主页"


def test_strip_tracking_keeps_real_parameters():
    assert strip_tracking("https://apps.apple.com/app/id1424956516?ref=footer") == "https://apps.apple.com/app/id1424956516"
    assert strip_tracking("https://www.instagram.com/eufyuk/?ref=footer&utm_source=site") == "https://www.instagram.com/eufyuk/"
    assert strip_tracking("https://www.facebook.com/profile.php?id=123&ref=x") == "https://www.facebook.com/profile.php?id=123"
    assert strip_tracking("https://www.youtube.com/@eufy") == "https://www.youtube.com/@eufy"


def test_app_store_links_are_hints_not_same_as():
    home = _html('<a href="https://www.instagram.com/eufylife/?ref=footer">ig</a> '
                 '<a href="https://www.youtube.com/shorts/x7yQkzD6Gdk?ref=image">video</a> '
                 '<a href="https://apps.apple.com/app/id1424956516?ref=footer">app</a> '
                 '<a href="https://play.google.com/store/apps/details?id=com.example.app">play</a>', "Eufy")
    snapshot = _snapshot([_page("https://x.test/", home)])
    fb = build_factbase(snapshot, extract_site(snapshot.pages, []), platforms=sources.load_platforms(), run_id="t", now=NOW)
    adopted = [v.value for v in fb.entity.same_as.values]
    hinted = [c.value for c in fb.entity.same_as.candidates]
    assert adopted == ["https://www.instagram.com/eufylife/"], "只采用账号主页,且去掉 ?ref=footer"
    assert "https://apps.apple.com/app/id1424956516" in hinted and any("play.google.com" in h for h in hinted)
    assert not any("shorts" in u for u in adopted + hinted), "视频链接连提示都不进"


# ── 四:sitemap lastmod 不可信时不当更新日期 ──


def test_lastmod_suspicion_rules():
    near_now = [SitemapEntry(loc=f"https://x.test/{i}", lastmod=(NOW + timedelta(seconds=i)).isoformat(), source_url="s")
                for i in range(6)]
    assert "抓取时刻" in (lastmod_suspicion(near_now, NOW) or "")
    same_day = [SitemapEntry(loc=f"https://x.test/{i}", lastmod="2026-11-10", source_url="s") for i in range(6)]
    assert "全是同一天" in (lastmod_suspicion(same_day, NOW) or "")
    spread = [SitemapEntry(loc=f"https://x.test/{i}", lastmod=f"2026-0{i}-01", source_url="s") for i in range(1, 7)]
    assert lastmod_suspicion(spread, NOW) is None
    assert lastmod_suspicion(same_day[:3], NOW) is None, "条数太少不下判断"
    old_times = [SitemapEntry(loc=f"https://x.test/{i}", lastmod=f"2026-0{i}-01T10:00:00+00:00", source_url="s")
                 for i in range(1, 7)]
    assert lastmod_suspicion(old_times, NOW) is None, "带时刻但不是抓取时刻,且分布在不同日子"


def test_untrusted_lastmod_is_not_a_page_date_nor_an_update_date():
    pages = [_page("https://x.test/", _html(FILLER, "Home")), _page("https://x.test/a", _html(FILLER, "A"))]
    entries = [SitemapEntry(loc=f"https://x.test/{p}", lastmod=(NOW + timedelta(seconds=3)).isoformat(), source_url="s")
               for p in ("", "a", "b", "c", "d", "e")]
    snapshot = _snapshot(pages, entries)
    extracts = extract_site(snapshot.pages, snapshot.sitemap_entries)
    assert all(d.kind == "sitemap_lastmod" and d.suspect for p in extracts for d in p.dates), "日期还在,但标了不可信"
    findings = visible_date.check(extracts, snapshot, NOW)
    per_page = [f for f in findings if f.page_url]
    assert [f.level for f in per_page] == ["not_checkable", "not_checkable"]
    assert all("不可信" in f.message for f in per_page)
    site = [f for f in findings if f.page_url is None]
    assert site[0].level == "notice" and "抓取时刻" in site[0].message
    fb = build_factbase(snapshot, extracts, platforms=sources.load_platforms(), run_id="t", now=NOW)
    assert any(g.code == "SITEMAP_LASTMOD_UNTRUSTED" for g in fb.gaps)
    assert all(not p.dates or all(d.suspect for d in p.dates) for p in fb.pages)

    trusted_entries = [SitemapEntry(loc="https://x.test/a", lastmod="2026-03-03", source_url="s")]
    extracts = extract_site(snapshot.pages, trusted_entries)
    a = next(p for p in extracts if p.url.endswith("/a"))
    assert a.dates and not a.dates[0].suspect, "条数太少不下判断,照常采用"


# ── 五:多页重复的模板文字里的数字不计 ──


def test_numbers_in_repeated_boilerplate_are_not_counted():
    promo = "<div>Get 10% OFF and up to 50 USD OFF Coupons</div>"

    def ex(url: str, body: str):
        return extract_page(FetchedPage(url=url, final_url=url, role="given", status=200, content_type="text/html",
                                        html=_html(FILLER + promo + body), headers={}, fetched_at=NOW))

    extracts = [ex("https://x.test/1", "<p>Users grew 35% this year.</p>"), ex("https://x.test/2", ""),
                ex("https://x.test/3", "")]
    findings = numeric_traceability.check(extracts)
    assert [f.level for f in findings] == ["notice", "pass", "pass"]
    assert "正文里 1 处统计型数字,其中 1 处" in findings[0].message and "模板文字" in findings[0].message
    assert "35%" in findings[0].evidence["example_1"] and "10%" not in " ".join(findings[0].evidence.values())
    assert "没有找到统计型数字" in findings[1].message and "另有 1 处" in findings[1].message

    alone = numeric_traceability.check([ex("https://x.test/only", "")])
    assert alone[0].level == "notice" and "1 处统计型数字" in alone[0].message, "只有一页时无法判断模板文字,照常计"
