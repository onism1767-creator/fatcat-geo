"""端到端:快照 → 三样产出落盘;只新增不覆盖;旧快照可以不联网重跑;命令行入口能跑通。"""

from __future__ import annotations

import json

import pytest

from butter import store
from butter.cli import main
from butter.pipeline import RunOptions, process_snapshot
from conftest import NOW


def test_one_run_writes_the_three_outputs(tmp_path, acme_snapshot):
    result = process_snapshot(acme_snapshot, RunOptions(out_root=tmp_path), now=lambda: NOW, run_id="run-a")
    run = result.run_dir
    assert run == tmp_path / "www.acme-kettles.test" / "run-a"
    for name in ("factbase.json", "factbase.md", "audit.json", "audit.md", "run.json", "change-package/README.md",
                 "change-package/manifest.json", "change-package/robots/robots.txt", "change-package/pages/facts.html",
                 "snapshot/snapshot.json", "snapshot/robots.txt", "snapshot/pages/00.html"):
        assert (run / name).is_file(), name
    assert not list(run.rglob("llms*.txt"))

    factbase = json.loads((run / "factbase.json").read_text(encoding="utf-8"))
    assert factbase["entity"]["name"]["values"][0]["source_url"] == "https://www.acme-kettles.test/"
    assert factbase["entity"]["name"]["conflict"] is True
    audit = json.loads((run / "audit.json").read_text(encoding="utf-8"))
    assert {f["check"] for f in audit["findings"]} == {"meta_robots", "visible_date", "numeric_traceability",
                                                       "hidden_text", "jsonld_consistency"}
    assert all(f["entry_id"].startswith("EVIDENCE_") for f in audit["findings"])
    meta = json.loads((run / "run.json").read_text(encoding="utf-8"))
    assert meta["models_called"] == 0 and meta["cost_usd"] == 0 and "Chrome/126" in meta["user_agent"]
    index = (tmp_path / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(index) == 1 and json.loads(index[0])["run_id"] == "run-a"

    md = (run / "audit.md").read_text(encoding="utf-8")
    assert "问题" in md and "EVIDENCE_HIDDEN_TEXT_INJECTION_V1" in md and "已知限制" in md
    fb_md = (run / "factbase.md").read_text(encoding="utf-8")
    assert "有冲突,已取 JSON-LD 的「Acme Kettles」" in fb_md and "(空缺)" in fb_md and "只提示,未采用" in fb_md


def test_runs_never_overwrite(tmp_path, acme_snapshot):
    options = RunOptions(out_root=tmp_path)
    process_snapshot(acme_snapshot, options, now=lambda: NOW, run_id="same")
    before = (tmp_path / "www.acme-kettles.test" / "same" / "factbase.json").read_bytes()
    with pytest.raises(store.RunDirExists):
        process_snapshot(acme_snapshot, options, now=lambda: NOW, run_id="same")
    assert (tmp_path / "www.acme-kettles.test" / "same" / "factbase.json").read_bytes() == before
    process_snapshot(acme_snapshot, options, now=lambda: NOW, run_id="other")
    assert len((tmp_path / "runs.jsonl").read_text(encoding="utf-8").splitlines()) == 2, "索引只追加"


def test_snapshot_round_trip_reruns_offline(tmp_path, acme_snapshot):
    first = process_snapshot(acme_snapshot, RunOptions(out_root=tmp_path), now=lambda: NOW, run_id="r1")
    loaded = store.load_snapshot(first.run_dir)
    assert loaded == acme_snapshot
    second = process_snapshot(loaded, RunOptions(out_root=tmp_path), now=lambda: NOW, run_id="r2")
    same = lambda fb: fb.model_copy(update={"run_id": ""})  # noqa: E731
    assert same(second.factbase) == same(first.factbase)
    assert [f.message for f in second.audit.findings] == [f.message for f in first.audit.findings]


def test_untrusted_page_text_is_neutralised_in_reports(tmp_path):
    """网页里的文字是数据:进了报告也不能变成图片、链接、HTML 或多出来的表格列。"""
    from butter.contracts import FetchedPage, RobotsFetch, SiteSnapshot
    from butter.util import md_cell

    evil = "Shop ![x](https://evil.example/p.png) [click](https://evil.example) <img src=x onerror=alert(1)> | extra `code`"
    html = (f"<html><head><title>{evil.replace('<', '&lt;')}</title>"
            f'<meta name="description" content="{evil.replace(chr(34), "")}"></head><body><h1>Shop</h1></body></html>')
    page = FetchedPage(url="https://shop.test/", final_url="https://shop.test/", role="given", status=200,
                       content_type="text/html", html=html, fetched_at=NOW)
    snap = SiteSnapshot(site_url="https://shop.test/", host="shop.test", origin="https://shop.test", fetched_at=NOW,
                        user_agent="ua", pages=[page], robots=RobotsFetch(url="https://shop.test/robots.txt", state="missing"))
    result = process_snapshot(snap, RunOptions(out_root=tmp_path), now=lambda: NOW, run_id="evil")
    reports = [result.run_dir / "factbase.md", result.run_dir / "change-package" / "README.md",
               result.run_dir / "change-package" / "suggestions" / "titles.md"]
    for path in reports:
        text = path.read_text(encoding="utf-8")
        assert "evil.example" in text, path.name
        assert "![x](" not in text and "[click](" not in text and "<img" not in text, path.name
    assert md_cell("a | b\nc [d](e) <f> `g`") == "a \\| b c \\[d\\](e) &lt;f&gt; \\`g\\`"
    facts = (result.run_dir / "change-package" / "pages" / "facts.html").read_text(encoding="utf-8")
    assert "<img src=x" not in facts and "&lt;img src=x" in facts


def test_challenged_run_claims_nothing_about_the_site(tmp_path):
    """真实运行里出过:第一个请求就被人机验证拦下,一页都没看到。这时不能说「站上没有」,也不凭空造页面。"""
    from butter.contracts import FetchedPage, RobotsFetch, SiteSnapshot

    wall = FetchedPage(url="https://blocked.test/", final_url="https://blocked.test/", role="given", status=403,
                       content_type="text/html", headers={"cf-mitigated": "challenge"},
                       html="<html><title>Just a moment...</title><body>Verify you are human</body></html>", fetched_at=NOW)
    snap = SiteSnapshot(site_url="https://blocked.test/", host="blocked.test", origin="https://blocked.test",
                        fetched_at=NOW, user_agent="ua", pages=[wall], stop_reason="challenge",
                        robots=RobotsFetch(url="https://blocked.test/robots.txt", state="unreadable", note="没有抓到这一步"),
                        notes=["https://blocked.test/ 返回了人机验证页"])
    result = process_snapshot(snap, RunOptions(out_root=tmp_path), now=lambda: NOW, run_id="wall")

    assert [g.code for g in result.factbase.gaps] == ["NO_PAGES"]
    assert not result.factbase.entity.name.values, "验证页的标题不能当品牌名"
    findings = result.audit.findings
    assert [f.check for f in findings] == ["meta_robots", "visible_date", "numeric_traceability", "hidden_text",
                                           "jsonld_consistency"]
    assert all(f.level == "not_checkable" and f.entry_id.startswith("EVIDENCE_") for f in findings)
    package = result.bundle.package
    assert package.items == [], "一页都没看到,不该有任何改动"
    assert {"robots.txt 三段式", "Organization / Product JSON-LD", "事实页与价格 / 规格 / 对比表事实块",
            "标题 / H1 / H2 / meta 建议"} == {n.what for n in package.not_generated}
    produced = {p.relative_to(result.run_dir / "change-package").as_posix()
                for p in (result.run_dir / "change-package").rglob("*") if p.is_file()}
    assert produced == {"README.md", "manifest.json", "reference/ua-table.md", "reference/excluded-vendors.md"}
    for name in ("factbase.md", "audit.md", "change-package/README.md"):
        text = (result.run_dir / name).read_text(encoding="utf-8")
        assert "站上现在没有" not in text and "站上没有机器可读" not in text, name
    readme = (result.run_dir / "change-package" / "README.md").read_text(encoding="utf-8")
    assert "可分析的 0 个" in readme and "遇到人机验证" in readme and "本次没有产出任何改动" in readme
    assert any("查不了" in w for w in result.warnings)


def test_store_refuses_path_tricks(tmp_path):
    for bad in ("../escape.txt", "/abs.txt", "a/../../b", "C:\\x.txt", ""):
        with pytest.raises(ValueError):
            store._safe_relative(bad)


def test_cli_runs_from_a_snapshot(tmp_path, acme_snapshot, capsys):
    first = process_snapshot(acme_snapshot, RunOptions(out_root=tmp_path / "a"), now=lambda: NOW, run_id="seed")
    code = main(["run", "--snapshot", str(first.run_dir), "--out", str(tmp_path / "b"), "--training", "optout"])
    out = capsys.readouterr().out
    assert code == 0 and "产出在" in out and "审计:问题" in out
    produced = list((tmp_path / "b" / "www.acme-kettles.test").iterdir())
    assert len(produced) == 1
    robots = (produced[0] / "change-package" / "robots" / "robots.txt").read_text(encoding="utf-8")
    assert "current: opt out" in robots


def test_cli_argument_errors(capsys):
    assert main(["run"]) == 2
    assert main(["run", "https://x.test", "--snapshot", "somewhere"]) == 2
    assert main(["run", "javascript:alert(1)", "--max-pages", "1"]) == 1
    assert "没跑成" in capsys.readouterr().err
