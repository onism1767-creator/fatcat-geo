"""本地导入:站长自己存下的 HTML 文件 → 同一种快照 → 同样的三样产出。全程不联网、不写源文件夹。"""

from __future__ import annotations

import json
import os
import shutil

import pytest

from butter import sources, store
from butter.cli import main
from butter.extract.html import extract_site
from butter.factbase import build_factbase
from butter.local_import import MAX_PAGE_BYTES, import_site
from butter.pipeline import RunOptions, process_snapshot
from conftest import ACME, HOST, NOW, SITE

URLS = (
    "# 文件名 网址\n"
    f"home.html {SITE}\n"
    f"classic.html {SITE}products/classic\n"
    f"about.html {SITE}about\n"
    f"boil-time.html {SITE}blog/boil-time\n"
)
PAGE = "<html><head><title>{title}</title>{head}</head><body><h1>{title}</h1><p>{body}</p></body></html>"
FILLER = "Plain words about kettles and how long they take to boil. " * 12


def acme_dir(tmp_path, *, urls: str | None = URLS):
    folder = tmp_path / "saved pages"
    shutil.copytree(ACME, folder)
    if urls is not None:
        (folder / "urls.txt").write_text(urls, encoding="utf-8")
    return folder


def write_page(folder, name: str, *, title: str = "Page", head: str = "", body: str = FILLER, prefix: str = "") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(prefix + PAGE.format(title=title, head=head, body=body), encoding="utf-8")


# ── 和联网抓到的是同一种东西 ──


def test_import_gives_the_same_facts_as_the_network_run(tmp_path, acme_snapshot, acme_factbase):
    folder = acme_dir(tmp_path)
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    snap = import_site(SITE, folder)

    assert snap.source == "local_files" and snap.user_agent == "" and snap.stop_reason == "completed"
    assert [p.role for p in snap.pages] == ["given", "discovered", "discovered", "discovered"]
    assert all(p.status is None and p.headers == {} and p.local_file for p in snap.pages), "没有联网,就没有状态和响应头"
    assert snap.robots.state == "ok" and snap.robots.text == acme_snapshot.robots.text
    assert [(e.loc, e.lastmod) for e in snap.sitemap_entries] == [(e.loc, e.lastmod) for e in acme_snapshot.sitemap_entries]
    assert "没有联网" in snap.notes[0] and "saved pages" in snap.notes[0]
    assert str(tmp_path) not in "\n".join(snap.notes), "备注里只写文件夹名,不写完整路径"

    extracts = extract_site(snap.pages, snap.sitemap_entries)
    assert len(extracts) == 4
    fb = build_factbase(snap, extracts, platforms=sources.load_platforms(), run_id="t", now=NOW)
    assert fb.entity.name.values[0].value == acme_factbase.entity.name.values[0].value
    assert fb.entity.name.conflict is acme_factbase.entity.name.conflict is True
    assert [p.name.values[0].value for p in fb.products] == [p.name.values[0].value for p in acme_factbase.products]
    assert [v.value for v in fb.products[0].price.values] == [v.value for v in acme_factbase.products[0].price.values]
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before, "导入只读,不改站长的文件夹"


def test_cli_runs_an_import_end_to_end(tmp_path, capsys):
    folder = acme_dir(tmp_path)
    code = main(["run", SITE, "--html-dir", str(folder), "--out", str(tmp_path / "out")])
    out = capsys.readouterr().out
    assert code == 0 and "不联网" in out and "导入了 4 个本机 HTML 文件" in out
    run = next((tmp_path / "out" / HOST).iterdir())
    meta = json.loads((run / "run.json").read_text(encoding="utf-8"))
    assert meta["source"] == "local_files" and meta["user_agent"] == "" and meta["snapshot_source"] == "html-dir:saved pages"
    assert [p["local_file"] for p in meta["pages"]] == ["home.html", "about.html", "boil-time.html", "classic.html"]
    assert meta["models_called"] == 0 and meta["cost_usd"] == 0

    readme = (run / "change-package" / "README.md").read_text(encoding="utf-8")
    assert "本次读取(本地导入)" in readme and "没有向你的站发任何请求" in readme
    assert "Chrome/126" not in readme, "没发请求,就不该写用了什么 UA"
    assert (run / "change-package" / "robots" / "robots.txt").is_file(), "文件夹里有 robots.txt,三段式照常生成"
    assert "取的是文件的修改时间" in readme
    audit_md = (run / "audit.md").read_text(encoding="utf-8")
    assert "来源:你存在本机的 HTML 文件" in audit_md and "X-Robots-Tag" in audit_md and "导入备注" in audit_md
    audit = json.loads((run / "audit.json").read_text(encoding="utf-8"))
    assert {f["check"] for f in audit["findings"]} == {"meta_robots", "visible_date", "numeric_traceability",
                                                       "hidden_text", "jsonld_consistency"}
    assert any(f["level"] == "problem" and f["check"] == "hidden_text" for f in audit["findings"]), "隐藏指令照样查得出"

    # 导入的快照也能存下来、不联网重跑
    again = store.load_snapshot(run)
    assert again.source == "local_files" and [p.local_file for p in again.pages] == [p["local_file"] for p in meta["pages"]]
    second = process_snapshot(again, RunOptions(out_root=tmp_path / "out2"), now=lambda: NOW, run_id="again")
    assert len(second.factbase.products) == 1


def test_read_date_comes_from_the_file_time(tmp_path):
    folder = acme_dir(tmp_path)
    stamp = 1_788_000_000  # 2026-08-29(UTC)
    for path in folder.glob("*.html"):
        os.utime(path, (stamp, stamp))
    snap = import_site(SITE, folder)
    assert snap.fetched_at.date().isoformat() == "2026-08-29" and "2026-08-29" in snap.notes[0]
    result = process_snapshot(snap, RunOptions(out_root=tmp_path / "out"), now=lambda: NOW, run_id="dated")
    facts = (result.run_dir / "change-package" / "pages" / "facts.html").read_text(encoding="utf-8")
    assert "2026-08-29" in facts and "2026-11-11" not in facts, "页脚写的是页面存下来的日子,不是导入的日子"


# ── 每个文件对应哪个网址 ──


def test_url_is_taken_from_what_the_page_declares(tmp_path):
    folder = tmp_path / "pages"
    write_page(folder, "a.html", head=f'<link rel="canonical" href="{SITE}guide">')
    write_page(folder, "b.html", head=f'<meta property="og:url" content="{SITE}pricing">')
    write_page(folder, "c.html", prefix=f"<!-- saved from url=(0040){SITE}blog/post -->\n",
               head=f'<link rel="canonical" href="{SITE}ignored-because-comment-wins">')
    write_page(folder, "d.html", head='<link rel="canonical" href="/relative/path">')
    write_page(folder, "index.html")
    write_page(folder, "mystery.html")
    snap = import_site(SITE, folder)
    by_file = {p.local_file: p for p in snap.pages}
    assert by_file["a.html"].url == f"{SITE}guide"
    assert by_file["b.html"].url == f"{SITE}pricing"
    assert by_file["c.html"].url == f"{SITE}blog/post" and by_file["c.html"].rendered_dom is True
    assert by_file["d.html"].url == f"{SITE}relative/path"
    assert by_file["index.html"].url == SITE and by_file["index.html"].role == "given"
    assert not by_file["a.html"].rendered_dom
    assert "mystery.html" not in by_file, "认不出地址的不导入,不猜"
    notes = "\n".join(snap.notes)
    assert "mystery.html:认不出它对应哪个网址" in notes and "urls.txt" in notes
    assert "网页,全部" in notes and "c.html" in notes, "渲染后保存的文件要提醒"
    result = process_snapshot(snap, RunOptions(out_root=tmp_path / "out"), now=lambda: NOW, run_id="r")
    assert any("脚本运行之后的页面" in text for text in result.audit.limitations)


def test_urls_txt_wins_and_is_never_second_guessed(tmp_path):
    folder = tmp_path / "pages"
    write_page(folder, "My Saved Page.html", head=f'<link rel="canonical" href="{SITE}from-canonical">')
    write_page(folder, "wrong.html", head=f'<link rel="canonical" href="{SITE}would-have-worked">')
    write_page(folder, "junk.html")
    (folder / "urls.txt").write_text(
        f"﻿# 注释\nMy Saved Page.html\t{SITE}from-manifest\nwrong.html https://other-site.test/x\n"
        "junk.html javascript:alert(1)\n只有一段\n", encoding="utf-8")
    snap = import_site(SITE, folder)
    assert [(p.local_file, p.url) for p in snap.pages] == [("My Saved Page.html", f"{SITE}from-manifest")]
    notes = "\n".join(snap.notes)
    assert "wrong.html" in notes and "别的主机(other-site.test)" in notes, "站长写错了就不导入,不退回去用 canonical"
    assert "junk.html" in notes and "不是 http / https 网址" in notes
    assert "第 5 行看不懂" in notes


def test_pages_from_another_host_are_not_adopted(tmp_path):
    folder = tmp_path / "pages"
    write_page(folder, "theirs.html", head='<link rel="canonical" href="https://competitor.test/page">')
    write_page(folder, "ours.html", head=f'<link rel="canonical" href="https://acme-kettles.test/ours">')
    snap = import_site(SITE, folder)
    assert [p.local_file for p in snap.pages] == ["ours.html"], "www 有无算同一个站;别的主机不算"
    assert "别的主机 competitor.test" in "\n".join(snap.notes)


def test_single_page_folder_is_the_given_url(tmp_path):
    folder = tmp_path / "one"
    write_page(folder, "whatever name.html")
    snap = import_site(f"{SITE}products/classic", folder)
    assert [(p.url, p.role) for p in snap.pages] == [(f"{SITE}products/classic", "given")]
    assert snap.robots.state == "unreadable" and "没有联网" in (snap.robots.note or "")
    result = process_snapshot(snap, RunOptions(out_root=tmp_path / "out"), now=lambda: NOW, run_id="one")
    assert "robots.txt 三段式" in {n.what for n in result.bundle.package.not_generated}, "没给 robots.txt 就不生成,不猜原有规则"
    org = [i for i in result.bundle.package.items if i.kind == "jsonld_organization"]
    assert not org or "没有读到首页" in org[0].needs_human[0]


# ── 存错了的文件 ──


def test_a_saved_challenge_page_is_not_imported(tmp_path):
    folder = tmp_path / "pages"
    folder.mkdir()
    (folder / "index.html").write_text(
        "<html><head><title>Just a moment...</title></head><body>Verify you are human"
        '<script src="/cdn-cgi/challenge-platform/x.js"></script></body></html>', encoding="utf-8")
    with pytest.raises(ValueError) as err:
        import_site(SITE, folder)
    assert "没有导入任何页面" in str(err.value) and "像是人机验证页" in str(err.value)

    write_page(folder, "real.html", title="Acme Kettles", head=f'<link rel="canonical" href="{SITE}">')
    snap = import_site(SITE, folder)
    assert [p.local_file for p in snap.pages] == ["real.html"]
    extracts = extract_site(snap.pages, [])
    fb = build_factbase(snap, extracts, platforms=sources.load_platforms(), run_id="t", now=NOW)
    assert "Just a moment" not in json.dumps(fb.model_dump(mode="json")), "验证页的标题不能进事实库"


def test_odd_folders_are_reported_not_guessed(tmp_path):
    with pytest.raises(ValueError, match="找不到文件夹"):
        import_site(SITE, tmp_path / "nope")
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "page.mhtml").write_text("MIME-Version: 1.0", encoding="utf-8")
    (empty / "blank.html").write_text("  \n", encoding="utf-8")
    with pytest.raises(ValueError) as err:
        import_site(SITE, empty)
    assert "不支持这种格式" in str(err.value) and "文件是空的" in str(err.value)
    with pytest.raises(ValueError, match="不是可用的 http / https 网址"):
        import_site("javascript:alert(1)", empty)
    assert main(["run", SITE, "--html-dir", str(tmp_path / "nope")]) == 1
    assert main(["run", "--html-dir", str(empty)]) == 2
    assert main(["run", "--snapshot", str(empty), "--html-dir", str(empty)]) == 2


def test_limits_duplicates_and_subfolders(tmp_path):
    folder = tmp_path / "pages"
    write_page(folder, "a.html", head=f'<link rel="canonical" href="{SITE}same">')
    write_page(folder, "b.html", head=f'<link rel="canonical" href="{SITE}same/">')
    write_page(folder, "big.html", head=f'<link rel="canonical" href="{SITE}big">', body="x" * (MAX_PAGE_BYTES + 10))
    write_page(folder / "a_files", "frame.html", head=f'<link rel="canonical" href="{SITE}frame">')
    for i in range(4):
        write_page(folder, f"p{i}.html", head=f'<link rel="canonical" href="{SITE}p{i}">')
    snap = import_site(SITE, folder, max_files=5)
    files = [p.local_file for p in snap.pages]
    assert files == ["a.html", "big.html", "p0.html", "p1.html"], "只读前 5 个文件;重复的一个不算;不进子文件夹"
    assert next(p for p in snap.pages if p.local_file == "big.html").truncated is True
    notes = "\n".join(snap.notes)
    assert "b.html:和 a.html 对应同一个网址" in notes and "只读了按文件名排在前面的 5 个" in notes
    assert "只读了前面一段" in notes


def test_robots_and_sitemap_files_that_are_not_what_they_claim(tmp_path):
    folder = acme_dir(tmp_path)
    (folder / "robots.txt").write_text("<html><body><pre>User-agent: *</pre></body></html>", encoding="utf-8")
    (folder / "sitemap.xml").write_text("not xml at all", encoding="utf-8")
    snap = import_site(SITE, folder)
    assert snap.robots.state == "unreadable" and "是一个网页" in (snap.robots.note or "")
    assert snap.sitemap_entries == [] and snap.sitemap_urls == []
    assert "sitemap.xml" in "\n".join(snap.notes)
    result = process_snapshot(snap, RunOptions(out_root=tmp_path / "out"), now=lambda: NOW, run_id="r")
    assert not (result.run_dir / "change-package" / "robots").exists()


def test_untrusted_text_in_import_notes_is_neutralised(tmp_path):
    folder = tmp_path / "pages"
    write_page(folder, "index.html")
    write_page(folder, "x ![img](evil.example) [click](evil.example) `y`.html")  # 文件名也是外来文字
    snap = import_site(SITE, folder)
    result = process_snapshot(snap, RunOptions(out_root=tmp_path / "out"), now=lambda: NOW, run_id="n")
    for name in ("audit.md", "change-package/README.md"):
        text = (result.run_dir / name).read_text(encoding="utf-8")
        assert "evil.example" in text, name
        assert "[click](evil.example)" not in text and "![img](" not in text, name
