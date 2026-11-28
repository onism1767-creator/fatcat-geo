"""长报告:章节怎么排、同样的内容是不是只出现一次、没做的步骤是不是照实写。用仿造的小结果,不读真实项目。"""

from __future__ import annotations

import json
import re
import zipfile
from datetime import UTC
from pathlib import Path

import pytest

from fatcat.modules import ModuleError
from fatcat.report import Sources, build, merge_advice, render, site_mismatch
from fatcat.report.words import CHAPTERS, WORDS
from fatcat.sources import butter as butter_source
from fatcat.sources import weblite as weblite_source

NOW = "2026-11-24 15:00 -0800"


def _cell(status: str, text: str) -> dict:
    return {"status": status, "status_label": f"[{status}]", "text": text}


def _toast(website: str = "https://brand.example", still_listed: bool = True) -> dict:
    def in_lang(lang: str) -> dict:
        steps = []
        for stage, own, third in (("RETRIEVAL", "found-own", "found-third"), ("CITATION", "cited-own", "cited-third"),
                                  ("REPRESENTATION", "mentioned-19-of-24", None)):
            steps.append({"stage": stage, "label": f"{stage} label {lang}", "intents": [{
                "intent": "category_discovery", "label": "discovery", "n_prompts": 8,
                "apis": [{"api": "Alpha API", "models": [{
                    "model": "a-one", "name": "Alpha API · a-one", "own": _cell("PASS", own),
                    "third": _cell("WEAK", third) if third else None}]}],
                "compares": ([{"name": "Alpha API · a-one", "brand": {"k": 19, "n": 24},
                               "competitors": [{"name": "Rival", "k": 7, "n": 18, "relation": "less",
                                                "relation_label": "less than you"}]}]
                             if stage == "REPRESENTATION" else []),
                "sentiments": ([{"name": "Alpha API · a-one", "total": 19, "text": "positive 7"}]
                               if stage == "REPRESENTATION" else []),
            }]})
        return {
            "title": "Brand visibility check", "scope_note": "scope-note-text", "api_note": "api-note-text",
            "sampling": ["sampling-line-24-questions", "method version 9.9"], "reminder": None,
            "ai_lines": ["Alpha API: mentioned you in 33 of 60"],
            "access": {"status": "NOT_OBSERVABLE", "status_label": "[?]", "headline": "local-access-headline",
                       "evidence": ["local-access-evidence"], "rate": None, "failed_checks": [], "hint_checks": []},
            "access_label": "Site access", "steps": steps, "wrap_up": "wrap-up-paragraph",
            "actions": [
                {"evidence_id": "EVIDENCE_STRUCT_V1", "kind": "geo_method", "stage": "RETRIEVAL",
                 "evidence_level": "B", "needs_experiment": True, "title": "Make titles say what the page answers",
                 "lines": ["- [Experiment card] Make titles say what the page answers", "  - Basis: study A",
                           "  - How to check: re-test · reference EVIDENCE_STRUCT_V1"],
                 "supporters": [{"intent": "discovery", "ai": "Alpha API · a-one"}]},
                {"evidence_id": "EVIDENCE_FAQ_V1", "kind": "geo_method", "stage": "RETRIEVAL", "evidence_level": "C",
                 "needs_experiment": True, "title": "Answer real questions on a page",
                 "lines": ["- [Experiment card] Answer real questions on a page", "  - Basis: study B"],
                 "supporters": []},
            ],
            "intent_labels": {"category_discovery": "discovery"},
        }

    return {
        "job": {"job_id": "job1", "brand": {"name": "Brand", "aliases": [], "domains": ["brand.example"]},
                "website": website, "spent_usd": 7.9615, "tier": {"runs_per_prompt": 3}},
        "targets": [{"surface": "alpha", "model": "a-one", "name": "Alpha API · a-one", "provider": "alpha",
                     "still_listed": still_listed}],
        "prompts": [], "stops": [],
        "domains": {"primary": "brand.example", "domains": [
            {"domain": "brand.example.cn", "status": "confirmed", "tier": "A1", "seen_count": 3},
            {"domain": "brand.cn", "status": "unconfirmed", "tier": "B1", "seen_count": 1}]},
        "spellings": [{"text": "Brand", "count": 133, "suggested": False, "registered": True}],
        "text": {"en": in_lang("en"), "zh": in_lang("zh")},
    }


def _fact(value: str, url: str = "https://www.brand.example/") -> dict:
    return {"values": [{"value": value, "source_url": url}]}


def _butter(host: str = "www.brand.example") -> dict:
    return {
        "dir": f"C:/runs/{host}/20261124-000000-abcdef",
        "files": ["audit.json", "change-package/manifest.json", "factbase.json", "run.json"],
        "run": {"run_id": "20261124-000000-abcdef", "host": host, "site_url": f"https://{host}/",
                "fetched_at": "2026-11-24T00:00:00+00:00", "pages": [{}, {}], "robots": {"state": "ok"},
                "stop_reason": "completed", "fetch_notes": ["fetch-note-kept-verbatim"], "models_called": 0,
                "cost_usd": 0, "owner_edits": 0, "audit_counts": {"problem": 1, "notice": 1}},
        "factbase": {"entity": {"name": _fact("Brand Inc")},
                     "products": [{"page_url": f"https://{host}/p1", "name": _fact("Widget"), "price": _fact("19.9"),
                                   "currency": _fact("USD"), "sku": _fact("W-1"), "availability": _fact("InStock")}],
                     "gaps": [{"code": "NO_CATEGORY", "message": "gap-message"}]},
        "audit": {"limitations": ["butter-limit-one"], "findings": [
            {"check": "meta_robots", "level": "problem", "page_url": f"https://{host}/a",
             "message": "robots-meta-problem-on-page-a"},
            {"check": "meta_robots", "level": "pass", "page_url": f"https://{host}/b", "message": "fine"},
            {"check": "jsonld_consistency", "level": "problem", "page_url": f"https://{host}/p1",
             "message": "rating <script>alert(1)</script> not on page"},
            {"check": "hidden_text", "level": "pass", "page_url": f"https://{host}/p1", "message": "nothing hidden"},
        ]},
        "changes": {"items": [
            {"item_id": "jsonld-organization", "entry_id": "EVIDENCE_STRUCT_V1", "action": "add",
             "target": "home <head>", "files": ["jsonld/organization.json"], "rationale": "add-organization-jsonld",
             "validation_state": "hypothesis", "needs_human": ["logo"]},
            {"item_id": "jsonld-product-1", "entry_id": "EVIDENCE_STRUCT_V1", "action": "none", "target": "p1",
             "files": [], "rationale": "already there", "validation_state": "hypothesis", "needs_human": []},
            {"item_id": "robots", "entry_id": "EVIDENCE_ACCESS_ROBOTS_V1", "action": "replace",
             "target": "/robots.txt", "files": ["robots/robots.txt"], "rationale": "append-three-groups",
             "validation_state": "documented", "needs_human": []},
        ]},
    }


def _weblite(url: str = "https://www.brand.example/", ruleset: str = "1.0.0") -> dict:
    return {
        "path": "C:/downloads/fatcat-FC-1.zip", "files": ["findings.json", "manifest.json"], "ruleset_known": True,
        "manifest": {"checked_url": url, "final_url": url, "ruleset_version": ruleset, "crawler_table_version": "1.0.0"},
        "findings": {"checked_at": "2026-11-24T00:00:00Z", "ai_model_calls": 0, "informational": [
            {"label": "llms.txt", "text": "not found, optional"}], "findings": [
            {"rule_id": "A01", "name": "Final HTTP status", "state": "passed", "severity": None, "value": "200 OK"},
            {"rule_id": "I03", "name": "noindex", "state": "issue", "severity": "critical", "value": "noindex",
             "observation": "meta robots noindex on line 42", "why": "search engines drop the page"},
            {"rule_id": "I11", "name": "In sitemap", "state": "unable_to_test", "severity": None,
             "value": "No result", "cause_label": "Our limit was reached"},
        ]},
        "methodology": {"known_limitations": ["weblite-limit-one"]}, "signals": None,
    }


def _full() -> Sources:
    return Sources(toast=_toast(), butter=_butter(), weblite=_weblite())


def _chapter(report, key: str):
    return next(c for c in report.chapters if c.key == key)


def _text(chapter) -> str:
    parts = []
    for block in chapter.blocks:
        parts += [block.text, block.caption, *block.items, *block.head]
        parts += [cell for row in block.rows for cell in row]
        parts += [x for title, lines in block.sections for x in (title, *lines)]
    return "\n".join(part for part in parts if part)


def test_twelve_chapters_in_the_agreed_order() -> None:
    report = build(_full(), "en", generated_at=NOW)
    assert [c.key for c in report.chapters] == list(CHAPTERS)
    assert [c.no for c in report.chapters] == list(range(12))
    assert report.warning is None


def test_steps_that_were_not_run_say_so() -> None:
    only_ai = build(Sources(toast=_toast()), "en", generated_at=NOW)
    for key in ("facts", "content", "compare"):
        assert _text(_chapter(only_ai, key)) == WORDS["en"]["not_done_improve"]
    only_site = build(Sources(butter=_butter()), "zh", generated_at=NOW)
    for key in ("retrieval", "citation", "mention"):
        assert _text(_chapter(only_site, key)) == WORDS["zh"]["not_done_ai"]
    assert only_site.title.startswith("www.brand.example")


def test_nothing_given_is_refused() -> None:
    with pytest.raises(ModuleError):
        build(Sources(), "en", generated_at=NOW)
    with pytest.raises(ModuleError):
        build(_full(), "fr", generated_at=NOW)


def test_advice_on_the_same_evidence_becomes_one_card() -> None:
    toast, butter = _toast(), _butter()
    cards = merge_advice(toast["text"]["en"]["actions"], butter["changes"]["items"])
    assert [c.evidence_id for c in cards] == ["EVIDENCE_STRUCT_V1", "EVIDENCE_FAQ_V1", "EVIDENCE_ACCESS_ROBOTS_V1"]
    both, toast_only, butter_only = cards
    assert both.toast is not None and [i["item_id"] for i in both.items] == ["jsonld-organization"]
    assert toast_only.items == [] and butter_only.toast is None
    assert butter_only.title == "/robots.txt"


def test_advice_chapter_counts_and_does_not_repeat_the_title_or_the_evidence_id() -> None:
    advice = _chapter(build(_full(), "en", generated_at=NOW), "advice")
    text = _text(advice)
    assert "2 recommendations from diagnosis and 2 changes" in text and "into 3 cards; 1 cards have both" in text
    assert "1 items are already in place" in text
    assert text.count("Make titles say what the page answers") == 1  # 标题那一行不在卡片里再出现
    assert text.count("EVIDENCE_STRUCT_V1") == 1  # Toast 的话里已经带了编号
    assert text.count("EVIDENCE_ACCESS_ROBOTS_V1") == 1  # 只有 Butter 的卡,由这里补一行
    assert "home <head> · add · hypothesis · jsonld/organization.json" in text


def test_robots_meta_finding_lives_in_the_access_chapter_only() -> None:
    report = build(_full(), "en", generated_at=NOW)
    assert "robots-meta-problem-on-page-a" in _text(_chapter(report, "access"))
    content = _text(_chapter(report, "content"))
    assert "robots-meta-problem-on-page-a" not in content and "meta_robots" not in content
    assert "jsonld_consistency" in content


def test_quick_check_owns_the_access_chapter_and_the_local_check_shrinks_to_one_line() -> None:
    with_bundle = _text(_chapter(build(_full(), "en", generated_at=NOW), "access"))
    assert "I03 noindex" in with_bundle and "issue · critical" in with_bundle
    assert "A01" not in with_bundle  # 通过的不逐项列
    assert "local-access-headline" in with_bundle and "local-access-evidence" not in with_bundle
    without = _text(_chapter(build(Sources(toast=_toast()), "en", generated_at=NOW), "access"))
    assert "local-access-evidence" in without


def test_each_fact_appears_once_in_the_page() -> None:
    page = render(build(_full(), "en", generated_at=NOW))
    for once in ("sampling-line-24-questions", "method version 9.9", "wrap-up-paragraph", "scope-note-text",
                 "weblite-limit-one", "butter-limit-one", "fetch-note-kept-verbatim", "mentioned-19-of-24",
                 WORDS["en"]["c10_shared_2"], "meta robots noindex on line 42"):
        assert page.count(once) == 1, once
    report = build(_full(), "en", generated_at=NOW)
    assert "sampling-line-24-questions" in _text(_chapter(report, "method"))
    assert "scope-note-text" in _text(_chapter(report, "limits"))


def test_chinese_copy_uses_full_width_punctuation_and_curly_quotes() -> None:
    """中文模板用全角标点和“”(决定 Z14)。花括号里是占位和格式(比如 {usd:.2f}),不算。"""
    for key, text in WORDS["zh"].items():
        bare = re.sub(r"\{[^}]*\}", "", text)
        assert not re.search(r"[「」,;:()]", bare), f"{key}: {text}"


def test_at_a_glance_is_written_in_whole_sentences() -> None:
    glance = _text(_chapter(build(_full(), "zh", generated_at=NOW), "conclusion"))
    assert "Quick Check 的 3 项检查完成了 2 项，查出 1 个问题（严重 1 个），其余 1 项无法检测。" in glance
    assert "我们读了网站上的 2 个页面，查出 1 个问题和 1 个提醒，建议改动 2 条。另外有 1 条网站上已经做到了。" in glance
    en = _text(_chapter(build(_full(), "en", generated_at=NOW), "conclusion"))
    assert ("Quick Check completed 2 of its 3 checks and found 1 issue (1 critical). "
            "The other one could not be tested.") in en
    assert ("We read 2 pages of your site and found 1 problem and 1 notice, and we suggest 2 changes. "
            "The site already has 1 more item in place.") in en
    assert "score" not in en  # 「没有总分」那一句删掉了:它只在解释报告里没有什么


def test_quick_check_sentence_says_where_the_rest_of_the_checks_went() -> None:
    bundle = _weblite()
    bundle["findings"]["findings"] = [
        {"rule_id": "A01", "name": "status", "state": "passed", "severity": None},
        {"rule_id": "I10", "name": "sitemap", "state": "not_applicable", "severity": None},
        {"rule_id": "I11", "name": "in sitemap", "state": "not_applicable", "severity": None},
    ]
    zh = _text(_chapter(build(Sources(weblite=bundle), "zh", generated_at=NOW), "conclusion"))
    assert zh == "Quick Check 的 3 项检查完成了 1 项，没有查出问题，其余 2 项不适用。"
    en = _text(_chapter(build(Sources(weblite=bundle), "en", generated_at=NOW), "conclusion"))
    assert en == "Quick Check completed 1 of its 3 checks and found no issues. The other 2 did not apply to this page."


def test_how_the_site_was_read_is_said_in_words_not_codes() -> None:
    method = _text(_chapter(build(Sources(butter=_butter()), "zh", generated_at=NOW), "method"))
    assert "robots.txt 读到了，计划要读的页面都读完了。" in method
    assert "读网站时没有调用 AI，花费是 $0.00。" in method and "改过" not in method  # 没改过就不写
    busy = _butter()
    busy["run"].update(robots={"state": "unreadable"}, stop_reason="challenge", models_called=1, cost_usd=0.02,
                       owner_edits=2)
    en = _text(_chapter(build(Sources(butter=busy), "en", generated_at=NOW), "method"))
    assert "robots.txt could not be read, and reading stopped at a human-verification page." in en
    assert "Drafting text while reading the site used 1 AI call and cost $0.02." in en
    assert "You corrected 2 of the facts it read." in en
    busy["run"]["stop_reason"] = "a-code-from-the-future"  # 不认识的代码原样印,不猜
    assert "a-code-from-the-future" in _text(_chapter(build(Sources(butter=busy), "en", generated_at=NOW), "method"))


def test_what_the_site_read_saw_in_the_sitemap_sits_with_the_sitemap_checks() -> None:
    """同一个 sitemap 的毛病,Quick Check 和读网站各有一句。有 Quick Check 时两句放在第 2 章一处,第 1 章不再说。"""
    butter = _butter()
    butter["run"]["fetch_notes"] = ["fetch-note-kept-verbatim", "sitemap https://www.brand.example/sitemap.xml: bad XML"]
    both = build(Sources(butter=butter, weblite=_weblite()), "en", generated_at=NOW)
    assert "bad XML" in _text(_chapter(both, "access")) and "bad XML" not in _text(_chapter(both, "method"))
    assert "fetch-note-kept-verbatim" in _text(_chapter(both, "method"))
    assert render(both).count("bad XML") == 1
    alone = build(Sources(butter=butter), "en", generated_at=NOW)  # 没有 Quick Check:留在第 1 章
    assert "bad XML" in _text(_chapter(alone, "method")) and "bad XML" not in _text(_chapter(alone, "access"))


def test_every_moment_is_written_the_same_way() -> None:
    """诊断记本地时间,Quick Check 和读网站记世界时。报告里都换成本地时间、一个写法。"""
    from datetime import timedelta, timezone

    from fatcat.report.build import _when

    west = timezone(timedelta(hours=-8))
    assert _when("2026-11-24T23:31:38Z", west) == "2026-11-24 15:31 UTC-8"
    assert _when("2026-11-25T01:30:07+00:00", west) == "2026-11-24 17:30 UTC-8"  # 世界时已经是第二天,本地还是同一天
    assert _when("2026-11-24T23:31:38Z", timezone(timedelta(hours=5, minutes=30))) == "2026-11-25 05:01 UTC+5:30"
    assert _when("2026-11-24T23:31:38Z", UTC) == "2026-11-24 23:31 UTC+0"
    assert _when("not a time") == "not a time" and _when(None) == ""
    assert _when("2026-11-24T10:00:00") == "2026-11-24T10:00:00"  # 没写时区的不猜
    method = _text(_chapter(build(_full(), "en", generated_at=NOW), "method"))
    assert "T00:00:00" not in method


def test_local_file_paths_never_reach_the_page() -> None:
    """读网站那一步把本机文件的完整路径记成出处。报告里只留文件名:文件夹和用户名不能印出来。"""
    butter = _butter()
    butter["factbase"]["entity"]["aliases"] = {"values": [
        {"value": "Brand Corp", "source_url": r"C:\Users\you\runs\p1\measure-export.zip!brand-domains.json"},
        {"value": "Brand Ltd", "source_url": "/home/someone/exports/list.json"},
        {"value": "Brand Co", "source_url": "typed in by the site owner"},
    ]}
    for lang in ("en", "zh"):
        page = render(build(Sources(butter=butter), lang, generated_at=NOW))
        assert "someone" not in page and "Users" not in page, lang
        assert "measure-export.zip · brand-domains.json" in page and "list.json" in page
        assert "typed in by the site owner" in page  # 不是路径的出处原样留着
        assert "https://www.brand.example/" in page  # 网址原样留着


def test_models_that_left_the_list_are_called_out() -> None:
    listed = _text(_chapter(build(Sources(toast=_toast()), "en", generated_at=NOW), "method"))
    assert "no longer on the list" not in listed and "$7.96" in listed
    retired = _text(_chapter(build(Sources(toast=_toast(still_listed=False)), "en", generated_at=NOW), "method"))
    assert "Alpha API · a-one, which are no longer on the list" in retired


def test_parts_about_different_sites_are_refused_unless_asked() -> None:
    mixed = Sources(toast=_toast(), butter=_butter(host="www.other.example"))
    assert site_mismatch(mixed) == ["brand.example", "www.other.example"]
    with pytest.raises(ModuleError, match="不是同一个网站"):
        build(mixed, "en", generated_at=NOW)
    report = build(mixed, "en", generated_at=NOW, allow_mixed=True)
    assert "www.other.example" in report.warning and 'class="warn"' in render(report)


def test_regional_site_the_brand_confirmed_counts_as_the_same_site() -> None:
    assert site_mismatch(Sources(toast=_toast(), weblite=_weblite(url="https://shop.brand.example.cn/x"))) == []
    assert site_mismatch(Sources(toast=_toast(), weblite=_weblite(url="https://brand.cn/"))) != []  # 没确认的不算
    assert site_mismatch(Sources(butter=_butter(host="www.brand.example"),
                                 weblite=_weblite(url="https://brand.example/"))) == []
    assert site_mismatch(Sources(butter=_butter(host="www.brand.example"),
                                 weblite=_weblite(url="https://other.example/"))) != []


def test_page_has_no_script_and_escapes_outside_text() -> None:
    page = render(build(_full(), "en", generated_at=NOW))
    assert "<script" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert '<meta name="color-scheme" content="light dark">' in page
    assert page.count("<h2 ") == 12 and 'role="region"' in page and "<caption id=" in page
    assert 'href="http' not in page and 'src="' not in page


def test_single_language_parts_are_marked_once_per_chapter() -> None:
    page = render(build(_full(), "en", generated_at=NOW))
    access = page.split('<section id="c2"')[1].split("</section>")[0]
    assert access.count(WORDS["en"]["lang_note_zh"]) == 1  # Butter 的那一块是中文
    assert WORDS["en"]["lang_note_en"] == "" and 'lang="zh"' in access
    zh_page = render(build(_full(), "zh", generated_at=NOW))
    assert zh_page.split('<section id="c2"')[1].split("</section>")[0].count(WORDS["zh"]["lang_note_en"]) == 1


def test_before_after_chapter_reports_what_exists() -> None:
    none_yet = _text(_chapter(build(_full(), "en", generated_at=NOW), "compare"))
    assert none_yet == WORDS["en"]["c9_none"]
    sources = _full()
    sources.batches = [{"batch_id": "b-1", "created_at": "x", "cards": 5,
                        "events": {"batch_created": 1, "remeasure_finished": 1}}]
    text = _text(_chapter(build(sources, "en", generated_at=NOW), "compare"))
    assert WORDS["en"]["c9_pending"] in text and "Batch b-1: 5 cards" in text and "remeasure_finished ×1" in text


# ---------------------------------------------------------------- 读文件的两处


def _bundle(path: Path, files: dict[str, object], folder: str = "fatcat-FC-1/") -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(folder + name, json.dumps(data))
        archive.writestr(folder + "main-document.html", "<html>not read</html>")
    return path


def test_evidence_bundle_is_read_from_inside_its_folder(tmp_path: Path) -> None:
    path = _bundle(tmp_path / "b.zip", {"manifest.json": {"ruleset_version": "1.0.0"}, "findings.json": {"findings": []}})
    loaded = weblite_source.load_bundle(path)
    assert loaded["manifest"]["ruleset_version"] == "1.0.0" and loaded["ruleset_known"] is True
    assert loaded["signals"] is None and "main-document.html" in loaded["files"]


def test_evidence_bundle_problems_are_named(tmp_path: Path) -> None:
    with pytest.raises(ModuleError, match="找不到证据包"):
        weblite_source.load_bundle(tmp_path / "missing.zip")
    with pytest.raises(ModuleError, match="findings.json"):
        weblite_source.load_bundle(_bundle(tmp_path / "half.zip", {"manifest.json": {}}))
    (tmp_path / "broken.zip").write_text("not a zip", encoding="utf-8")
    with pytest.raises(ModuleError, match="读不了"):
        weblite_source.load_bundle(tmp_path / "broken.zip")
    newer = _bundle(tmp_path / "new.zip", {"manifest.json": {"ruleset_version": "2.0.0"}, "findings.json": {}})
    assert weblite_source.load_bundle(newer)["ruleset_known"] is False


def test_butter_run_folder_is_read_and_missing_files_are_named(tmp_path: Path) -> None:
    run = tmp_path / "out" / "www.brand.example" / "r1"
    (run / "change-package").mkdir(parents=True)
    (run / "snapshot").mkdir()
    (run / "snapshot" / "snapshot.json").write_text("{}", encoding="utf-8")
    for name, data in (("run.json", {"run_id": "r1", "host": "www.brand.example", "generated_at": "2026"}),
                       ("factbase.json", {}), ("audit.json", {})):
        (run / name).write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ModuleError, match="manifest.json"):
        butter_source.load_run(run)
    (run / "change-package" / "manifest.json").write_text('{"items": []}', encoding="utf-8")
    loaded = butter_source.load_run(run)
    assert loaded["run"]["run_id"] == "r1" and "snapshot/snapshot.json" not in loaded["files"]
    assert [r["run_id"] for r in butter_source.find_runs(tmp_path / "out")] == ["r1"]


def test_batches_are_counted_from_the_event_log(tmp_path: Path) -> None:
    events = tmp_path / "ws" / "events"
    events.mkdir(parents=True)
    rows = [
        {"type": "batch_created", "batch_id": "b-1", "at": "t0", "payload": {"run_id": "r1", "card_ids": ["a", "b"]}},
        {"type": "card_created", "batch_id": "b-1", "payload": {}},
        {"type": "card_created", "batch_id": "b-1", "payload": {}},
        {"type": "batch_created", "batch_id": "b-2", "at": "t1", "payload": {"run_id": "other", "card_ids": []}},
    ]
    (events / "cards.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n", encoding="utf-8")
    assert butter_source.batches_for(tmp_path / "ws", "r1") == [
        {"batch_id": "b-1", "created_at": "t0", "cards": 2, "events": {"batch_created": 1, "card_created": 2}}]
    assert butter_source.batches_for(tmp_path / "nowhere", "r1") == []
