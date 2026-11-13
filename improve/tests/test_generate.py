"""确定性生成器:robots 三段式保留原规则、JSON-LD 与事实库一致、生成物里的每个数都能溯源、不出 llms.txt、不称有效。"""

from __future__ import annotations

import html
import json
import re

import pytest

from butter import registry_ids as ids
from butter import sources
from butter.contracts import FactField, RobotsFetch
from butter.extract.robots import parse_robots, rules_allow
from butter.generate import build_package
from butter.generate.headings import tokens
from butter.generate.jsonld import to_script
from butter.generate.robots import BLOCK_FILE, FILE, build_robots
from conftest import ACME, NOW

UA = sources.load_ua_table()
ORIGINAL = (ACME / "robots.txt").read_text(encoding="utf-8")


def robots_for(text: str | None, training="keep", state="ok", sitemaps=()):
    fetch = RobotsFetch(url="https://x.test/robots.txt", state=state, status=200 if state == "ok" else 404, text=text or "")
    return build_robots(fetch, UA, training=training, sitemap_urls=list(sitemaps), today="2026-11-11")


def allowed(robots_text: str, token: str, path: str) -> bool:
    parsed = parse_robots(robots_text)
    groups = parsed.groups_for(token) or parsed.groups_for("*")
    return rules_allow([r for g in groups for r in g.rules], path)


# ── robots 三段式 ──


def test_original_robots_is_kept_verbatim_and_block_is_ascii():
    files, items, _ = robots_for(ORIGINAL)
    assert files[FILE].startswith(ORIGINAL)
    assert files[FILE].endswith(files[BLOCK_FILE])
    assert files[BLOCK_FILE].isascii(), "追加段要放到别人服务器上,只用 ASCII"
    for section in ("Section 1", "Section 2", "Section 3"):
        assert section in files[BLOCK_FILE]
    assert items[0].action == "replace" and items[0].entry_id == ids.ACCESS_ROBOTS
    assert items[0].validation_state == "官方文档要求,必要非充分"


def test_star_rules_are_copied_so_nothing_opens_up():
    full = robots_for(ORIGINAL)[0][FILE]
    for token in ("OAI-SearchBot", "Claude-SearchBot", "Kimi-SearchBot", "Googlebot", "ChatGPT-User", "Kimi-User"):
        assert allowed(full, token, "/products/classic"), token
        assert not allowed(full, token, "/admin/x"), f"{token} 单列一组后不能绕过原来的 Disallow"
        assert not allowed(full, token, "/cart"), token
    assert not allowed(ORIGINAL, "OAI-SearchBot", "/admin/x"), "对照:原文件里本来就挡着"


def test_named_user_agents_are_left_alone():
    files, items, _ = robots_for(ORIGINAL)
    block = files[BLOCK_FILE]
    assert "User-agent: PerplexityBot" not in block and "User-agent: GPTBot" not in block
    assert not allowed(files[FILE], "PerplexityBot", "/"), "原文件点名屏蔽的保持屏蔽"
    assert any("PerplexityBot" in note and "没有改" in note for note in items[0].needs_human)


def test_training_switch():
    keep = robots_for(ORIGINAL, training="keep")[0][FILE]
    assert allowed(keep, "ClaudeBot", "/products/classic"), "默认不改动:训练类爬虫继续按原规则走"
    assert "# User-agent: ClaudeBot" in keep and "# Disallow: /" in keep
    out = robots_for(ORIGINAL, training="optout")[0][FILE]
    for token in ("ClaudeBot", "KimiBot", "Google-Extended", "Applebot-Extended"):
        assert not allowed(out, token, "/"), token
    assert allowed(out, "OAI-SearchBot", "/"), "训练开关不影响搜索放行"


def test_kimi_three_user_agents_are_in_their_sections():
    block = robots_for("User-agent: *\nDisallow:\n", training="optout")[0][BLOCK_FILE]
    s1, rest = block.split("Section 2", 1)
    s2, s3 = rest.split("Section 3", 1)
    assert "User-agent: Kimi-SearchBot" in s1 and "User-agent: KimiBot" in s2 and "User-agent: Kimi-User" in s3
    assert "https://www.kimi.ai/policies/kimi-crawlers" in block and "(checked 2026-" in block


def test_star_blocks_everything_gets_allow_and_a_warning():
    files, items, _ = robots_for("User-agent: *\nDisallow: /\n")
    assert allowed(files[FILE], "OAI-SearchBot", "/any") and not allowed(files[FILE], "SomeOtherBot", "/any")
    assert any("实质改动" in n for n in items[0].needs_human)


def test_no_robots_yet_and_unreadable_robots():
    files, items, _ = robots_for(None, state="missing", sitemaps=["https://x.test/sitemap.xml"])
    assert items[0].action == "add" and "Allow: /" in files[FILE]
    assert "Sitemap: https://x.test/sitemap.xml" in files[FILE]
    files, items, missing = build_robots(RobotsFetch(url="u", state="unreadable", status=503, note="HTTP 503"), UA,
                                         training="keep", sitemap_urls=[], today="2026-11-11")
    assert files == {} and items == [] and "没有生成" in missing[0].reason


def test_running_again_on_our_own_output_adds_nothing():
    first = robots_for(ORIGINAL, training="optout")[0][FILE]
    files, items, _ = robots_for(first, training="optout")
    assert files == {} and items[0].action == "none"


# ── 改动包整体 ──


def test_package_has_the_expected_pieces_and_no_llms_txt(acme_bundle):
    names = set(acme_bundle.files)
    assert {"README.md", "manifest.json", FILE, "jsonld/organization.json", "jsonld/product-acme-classic-kettle.json",
            "pages/facts.html", "blocks/product-acme-classic-kettle.html", "suggestions/titles.md",
            "reference/ua-table.md", "reference/excluded-vendors.md"} <= names
    assert not any("llms" in name.lower() for name in names)
    kinds = {i.kind for i in acme_bundle.package.items}
    assert kinds == {"robots", "jsonld_organization", "jsonld_product", "fact_page", "fact_block", "heading_suggestion"}


def test_every_item_has_entry_id_and_honest_validation_state(acme_bundle):
    for item in acme_bundle.package.items:
        assert item.entry_id in ids.ALL_IDS
        expected = "官方文档要求,必要非充分" if item.kind == "robots" else "假设"
        assert item.validation_state == expected, item.kind
    manifest = json.loads(acme_bundle.files["manifest.json"])
    assert len(manifest["items"]) == len(acme_bundle.package.items)
    assert {n.what for n in acme_bundle.package.not_generated} == {"对比表事实块", "按题板的标题复述建议"}


def test_no_effect_claims_anywhere(acme_bundle):
    text = "\n".join(acme_bundle.files.values())
    for banned in ("已验证有效", "证明有效", "保证", "一定能", "必然", "guarantee", "proven to"):
        assert banned not in text, banned
    assert "没有任何效果承诺" in acme_bundle.files["README.md"]
    assert "假设" in acme_bundle.files["README.md"]


def numbers_in(text: str) -> set[str]:
    text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), text)  # < 是转义的 <,不是数字
    return {re.sub(r"\.0+$", "", n) for n in re.findall(r"\d+(?:\.\d+)?", text)}


def test_every_number_in_generated_facts_traces_to_the_factbase(acme_bundle, acme_factbase):
    known = numbers_in(acme_factbase.model_dump_json()) | {"5", "8", "1"}  # utf-8 / viewport / initial-scale
    for name in ("jsonld/organization.json", "jsonld/product-acme-classic-kettle.json",
                 "blocks/product-acme-classic-kettle.html", "pages/facts.html"):
        strays = numbers_in(html.unescape(acme_bundle.files[name])) - known  # &#39; 是转义后的撇号,不是数字
        assert not strays, f"{name} 里出现了事实库里没有的数字:{strays}"


def test_facts_used_point_back_to_factbase_values(acme_bundle, acme_factbase):
    fields = [getattr(acme_factbase.entity, k) for k in type(acme_factbase.entity).model_fields]
    for p in acme_factbase.products:
        fields += [v for v in (getattr(p, k) for k in type(p).model_fields) if isinstance(v, FactField)]
    known = {(v.value, v.source_url) for f in fields for v in f.values}
    known |= {(s.value, s.source_url) for p in acme_factbase.products for s in p.specs}
    refs = [r for item in acme_bundle.package.items for r in item.facts_used if r.field != "entity.official_domain"]
    assert len(refs) > 20
    assert all((r.value, r.source_url) in known for r in refs)


# ── JSON-LD / 事实页 / 事实块 ──


def test_generated_jsonld_matches_factbase_and_drops_ratings(acme_bundle):
    org = json.loads(acme_bundle.files["jsonld/organization.json"])
    assert org["name"] == "Acme Kettles" and org["url"] == "https://www.acme-kettles.test/"
    assert "https://www.youtube.com/@acmekettles" in org["sameAs"]
    assert "https://www.tiktok.com/@acmekettles" not in org["sameAs"], "只提示的候选不进 JSON-LD"
    product = json.loads(acme_bundle.files["jsonld/product-acme-classic-kettle.json"])
    assert product["offers"] == {"@type": "Offer", "price": "49.00", "priceCurrency": "USD",
                                 "availability": "https://schema.org/InStock",
                                 "url": "https://www.acme-kettles.test/products/classic"}
    assert "aggregateRating" not in product and "review" not in product
    assert product["color"] == "Silver" and len(product["additionalProperty"]) == 2
    item = next(i for i in acme_bundle.package.items if i.kind == "jsonld_product")
    assert item.action == "replace" and any("不要整段覆盖" in n for n in item.needs_human)
    assert any("59.00" in n for n in item.needs_human), "价格冲突要摆给站长看"


def test_untrusted_strings_cannot_break_out(acme_bundle):
    snippets = acme_bundle.files["jsonld/snippets.html"]
    assert snippets.count("</script>") == snippets.count('<script type="application/ld+json">') == 2
    assert "\\u003c/script\\u003e\\u003cscript\\u003ealert" in snippets
    inner = to_script({"x": "</script><!-- & \u2028"}).split("\n", 1)[1].rsplit("\n", 1)[0]
    assert not any(ch in inner for ch in "<>&\u2028") and json.loads(inner) == {"x": "</script><!-- & \u2028"}
    raw = json.loads(acme_bundle.files["jsonld/product-acme-classic-kettle.json"])
    assert raw["description"] == "Whistles when ready. </script><script>alert('x')</script>", "值本身一字不差"
    page = acme_bundle.files["pages/facts.html"]
    assert "<script>alert" not in page and "&lt;/script&gt;&lt;script&gt;alert" in page


def test_fact_page_and_block(acme_bundle):
    page = acme_bundle.files["pages/facts.html"]
    assert '<html lang="en">' in page and "<h1>Acme Kettles: Facts</h1>" in page
    assert "read from www.acme-kettles.test on 2026-11-11" in page
    assert "last updated" not in page.lower() and "最后更新" not in page
    assert "wikipedia.org" not in page.split("</head>", 1)[1], "百科页不列在官方渠道下"
    block = acme_bundle.files["blocks/product-acme-classic-kettle.html"]
    assert block.startswith("<table>") and "<td>49.00 USD</td>" in block and "<td>1.7 L</td>" in block
    assert "4.8" not in block and "312" not in block


def test_empty_site_generates_nothing_it_cannot_back_up(acme_snapshot):
    from butter.extract.html import extract_site
    from butter.factbase import build_factbase
    bare = acme_snapshot.model_copy(update={"pages": [], "sitemap_entries": [], "sitemap_urls": []})
    extracts = extract_site(bare.pages, [])
    fb = build_factbase(bare, extracts, platforms=sources.load_platforms(), run_id="t", now=NOW)
    bundle = build_package(bare, extracts, fb, ua_table=UA, excluded=sources.load_excluded_vendors(), board=None,
                           training="keep", now=NOW)
    assert {i.kind for i in bundle.package.items} == {"robots"}
    missing = {n.what for n in bundle.package.not_generated}
    assert {"Organization / Product JSON-LD", "事实页与价格 / 规格 / 对比表事实块", "标题 / H1 / H2 / meta 建议"} <= missing
    assert not any(name.startswith(("jsonld/", "pages/", "blocks/")) for name in bundle.files)


def test_org_jsonld_flags_an_unseen_home_page(acme_snapshot, acme_extracts, acme_factbase):
    """只抓到内页、没抓到首页时:不能当作首页上没有 Organization,要提醒先看一眼。"""
    from butter.generate.jsonld import build_jsonld
    inner = [p for p in acme_extracts if p.url.rstrip("/") != acme_snapshot.origin
             and not any("Organization" in n.types for n in p.nodes)]
    assert inner, "夹具里应当有不带 Organization 的内页"
    _, items, _ = build_jsonld(acme_factbase, inner, acme_snapshot.origin)
    org = next(i for i in items if i.kind == "jsonld_organization")
    assert org.action == "add" and "没有读到首页" in org.needs_human[0]
    assert "站上现在没有" not in org.rationale and "本次读到的页面" in org.rationale


# ── 标题建议 ──


def test_headings_without_board_only_state_facts(acme_bundle):
    md = acme_bundle.files["suggestions/titles.md"]
    assert "缺 meta description" in md and "## 建议" not in md
    report = json.loads(acme_bundle.files["suggestions/titles.json"])
    assert report["mode"] == "status_only" and report["suggestions"] == []


def test_headings_with_question_board(tmp_path, acme_snapshot, acme_extracts, acme_factbase):
    board_file = tmp_path / "questions.yml"
    board_file.write_text(
        "prompt_version: test-v1\nlocale: en-US\nquestions:\n"
        "  - {id: d01, intent: discovery, text: 'What is the best stovetop kettle for induction hobs?'}\n"
        "  - {id: d02, intent: discovery, text: 'How long does a kettle take to boil?'}\n"
        "  - {id: d03, intent: discovery, text: 'Which stovetop kettle brands last longest?'}\n"
        "  - {id: x01, intent: discovery, text: 'Where can I rent a canoe?'}\n", encoding="utf-8")
    board = sources.load_questions_yml(board_file)
    bundle = build_package(acme_snapshot, acme_extracts, acme_factbase, ua_table=UA,
                           excluded=sources.load_excluded_vendors(), board=board, training="keep", now=NOW)
    report = json.loads(bundle.files["suggestions/titles.json"])
    coverage = {q["id"]: q["coverage"] for q in report["questions"]}
    assert coverage["d02"] == "covered" and coverage["x01"] == "none" and coverage["d01"] == "partial"
    suggestion = next(s for s in report["suggestions"] if s["based_on_question"] in ("d01", "d03"))
    question = next(q.text for q in board.questions if q.id == suggestion["based_on_question"])
    assert suggestion["proposed"]["h1"] == question.rstrip("?"), "题目原文照抄,不改写"
    assert suggestion["proposed"]["title"] == f"{question.rstrip('?')} | Acme Kettles"
    assert suggestion["proposed"]["meta_description"] == "Acme Kettles makes stovetop kettles in stainless steel."
    items = [i for i in bundle.package.items if i.entry_id == ids.TITLE_QUERY_MATCH]
    assert items and all(i.validation_state == "假设" and i.action == "review" for i in items)


def test_tokens_handle_both_languages():
    assert tokens("What are the best backpacking packs?") == {"best", "backpacking", "pack"}, "单复数归一"
    assert tokens("哪些电水壶值得买") == {"电水", "水壶"}
    assert "水壶" in tokens("不锈钢水壶怎么选") & tokens("水壶选购指南")


@pytest.mark.parametrize("name", ["reference/ua-table.md", "reference/excluded-vendors.md"])
def test_reference_tables_ship_with_the_package(acme_bundle, name):
    text = acme_bundle.files[name]
    assert "核对日期" in text and "2026-11-" in text


def test_board_with_no_matching_question_is_one_line_not_fifty_rows(acme_factbase):
    """真实运行里出过:50 题的题板和站点无关,报告里列了 50 行「没有页面对得上」。"""
    from butter.contracts import Question, QuestionBoard
    from butter.generate.headings import build_headings

    questions = [Question(id=f"q{i:02d}", intent="discovery", text=f"Which zeppelin harbour number {i} is nicest?")
                 for i in range(40)]
    board = QuestionBoard(path="questions.yml", prompt_version="v1", locale="en-US", questions=questions)
    files, items, missing = build_headings(acme_factbase, board)
    md = files["suggestions/titles.md"]
    assert "| 题号 |" not in md and md.count("zeppelin") == 0, "对不上的题不逐行列"
    assert "有 40 道题和本次读到的页面在字面上完全对不上" in md and "q00、q01" in md and " 等。" in md
    assert len(json.loads(files["suggestions/titles.json"])["questions"]) == 40, "完整清单留在 JSON 里"
    assert any(n.what == "按题板的标题复述建议" and "40 题" in n.reason for n in missing)
    assert not any(i.entry_id == ids.TITLE_QUERY_MATCH for i in items)
