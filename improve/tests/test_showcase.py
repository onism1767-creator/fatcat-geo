"""展示页:真名换代号、换完全文查、站内链接改成能离线打开的、整份导出、命令行。全部用夹具站 acme,不联网、不花钱。"""

from __future__ import annotations

import json

import pytest

from butter import store
from butter.cli import main
from butter.jobs import create_batch
from butter.pipeline import RunOptions, process_snapshot
from butter.showcase import ShowcaseLeak, build, parse_names
from butter.showcase import export as export_module
from butter.showcase import names as N
from butter.showcase.static import relink

from conftest import NOW

MANUAL = [("Acme", "Brand A")]   # 夹具站的品牌叫 Acme Kettles;正文里有只写 Acme 的地方,这类要人工补


@pytest.fixture()
def run(tmp_path, acme_snapshot):
    out, workspace = tmp_path / "out", tmp_path / "workspace"
    result = process_snapshot(acme_snapshot, RunOptions(out_root=out), now=lambda: NOW, run_id="run-show")
    batch_id, problems = create_batch(workspace, result.bundle.package, site_host=result.factbase.host, industry="",
                                      lang="en")
    assert batch_id and not problems
    return out, workspace, result


# ── 纯层:对照表、替换、查漏 ──────────────────────────────────────────

def test_longer_names_go_first_and_nothing_is_replaced_twice():
    names = [("acme", "Brand A"), ("acme.com", "brand-a.example"), ("Brand", "Oops")]
    assert N.recode("Visit acme.com, the home of ACME.", names) == "Visit brand-a.example, the home of Brand A."
    assert N.recode("没有要换的字", names) == "没有要换的字" and N.recode("", names) == ""


def test_names_inside_urls_and_file_names_use_the_url_spelling():
    names = [("acme", "Brand A"), ("Robot Vacuum S9", "Model 1")]
    assert N.recode("product-acme-kit.html", names) == "product-brand-a-kit.html"
    assert N.recode("https://shop.test/acmeofficial", names) == "https://shop.test/brand-aofficial"
    assert N.recode("Meet acme. It cleans.", names) == "Meet Brand A. It cleans."          # 句号结尾不算夹在网址里
    assert N.recode("The Robot Vacuum S9 is here", names) == "The Model 1 is here"


def test_short_model_codes_only_match_as_whole_words():
    names = [("S2", "X1")]
    assert N.whole_word_only("S2") and N.whole_word_only("E35") and not N.whole_word_only("acme")
    assert N.recode("Omni S2 Pro", names) == "Omni X1 Pro"
    for untouched in ("EVIDENCE_STATS2", "EVIDENCE_S2_V1", "color #a3s2b0", "run 20261119-0s2f"):
        assert N.recode(untouched, names) == untouched and N.leaks(untouched, names) == []
    assert N.leaks("the s2 model", names) == ["S2"]


def test_leaks_finds_what_is_left_whatever_the_case():
    names = [("Acme Kettles", "Brand A"), ("AK-100", "SKU-1")]
    assert N.leaks("Sold by ACME KETTLES, item ak-100.", names) == ["Acme Kettles", "AK-100"]
    assert N.leaks(N.recode("Sold by ACME KETTLES, item ak-100.", names), names) == []


def test_names_are_listed_from_the_fact_base(acme_factbase):
    names, too_short = N.auto_names(acme_factbase)
    table = dict(names)
    assert table["acme-kettles.test"] == "brand-a.example" and table["Acme Kettles"] == "Brand A"
    assert table["Acme Classic Kettle"] == "Model 1" and table["acme-classic-kettle"] == "model-1" and table["AK-100"] == "SKU-1"
    assert table["https://www.instagram.com/acmekettles/"].startswith("https://social.example/")
    assert all(code for _, code in names) and too_short == []
    assert N.hosts_of(names) == ["acme-kettles.test"]


def test_every_address_on_the_site_gets_a_numbered_stand_in():
    texts = ["see https://www.acme-kettles.test/kettles/classic, and https://acme-kettles.test/about.",
             "again https://www.acme-kettles.test/kettles/classic/ (home: https://www.acme-kettles.test)",
             "elsewhere https://other.test/acme"]
    urls = N.site_urls(texts, ["acme-kettles.test"])
    assert urls == ["https://www.acme-kettles.test/kettles/classic", "https://acme-kettles.test/about",
                    "https://www.acme-kettles.test/kettles/classic/", "https://www.acme-kettles.test"]
    named = dict(N.url_names(urls, [("https://acme-kettles.test/about", "https://brand-a.example/products/model-1")]))
    assert "https://acme-kettles.test/about" not in named                                      # 对照表里已经有的不重复编
    assert named["https://www.acme-kettles.test/kettles/classic"] == named["https://www.acme-kettles.test/kettles/classic/"]
    assert named["https://www.acme-kettles.test"] == "https://brand-a.example/"


def test_manual_names_file_is_checked():
    assert parse_names(None) == [] and parse_names({"names": []}) == []
    assert parse_names({"names": [{"real": " SkyLine ", "code": "Series 1"}]}) == [("SkyLine", "Series 1")]
    for bad in ({"names": "SkyLine"}, {"names": [{"real": "x"}]}, ["x"], {"names": [{"real": "", "code": "y"}]}):
        with pytest.raises(ValueError):
            parse_names(bad)


def test_site_links_become_relative_and_missing_pages_lose_their_link():
    pages = {"/": "index.html", "/runs/r1": "example/run.html", "/registry": "registry.html"}
    html = '<a href="/">home</a> <a href="/runs/r1" class="x">run</a> <a href="/settings">设置</a> <a href="https://a.example/">out</a>'
    assert relink(html, "index.html", pages) == \
        '<a href="index.html">home</a> <a href="example/run.html" class="x">run</a> 设置 <a href="https://a.example/">out</a>'
    assert relink('<a href="/registry">证据表</a>', "example/run.html", pages) == '<a href="../registry.html">证据表</a>'


# ── 整份导出 ──────────────────────────────────────────────────────────

def test_export_has_every_page_no_buttons_and_no_real_names(run):
    out, workspace, result = run
    files, report = build(result.run_dir, workspace, manual=MANUAL, today="2026-11-23")
    for page in ("index.html", "registry.html", "example/run.html", "example/batch.html"):
        assert page in files and page.removesuffix(".html") + ".json" in files, page    # 每页都带 .json 孪生
        html = files[page]
        assert "<form" not in html and "<button" not in html and "<script" not in html.lower(), page
        assert 'href="/' not in html and "http-equiv" not in html, page                    # 站内链接都改成了相对路径
        json.loads(files[page.removesuffix(".html") + ".json"])
    assert report.pages == ["example/batch.html", "example/run.html", "index.html", "registry.html"]
    assert report.package_files > 5 and any(p.startswith("example/change-package/") for p in files)
    for path, text in files.items():
        assert "acme" not in f"{path}\n{text}".casefold(), path                             # 文件名和内容里都搜不到真名
        assert "ak-100" not in text.casefold(), path
    home = files["index.html"]
    for heading in ("1. 一次完整的例子", "2. 一批改动会走过哪几步", "3. 什么都不改，结果自己会晃多少",
                    "4. 哪些厂商的爬虫没有放进来", "5. Butter 做什么，不做什么"):
        assert heading in home, heading
    assert 'href="example/run.html"' in home and 'href="example/batch.html"' in home
    assert "不是在证明 Butter 判得准" in home and "不保证改完之后 AI 就会提到你" in home
    for word in ("σ", "τ", "预注册", "事后", "安慰剂", "bootstrap"):                        # 不出现研究用语
        assert word not in home, word
    run_page = files["example/run.html"]
    assert "Brand A" in run_page and "brand-a.example" in run_page and 'href="../index.html"' in run_page
    assert "下载改动包" not in run_page and "改事实" not in run_page                        # 展示页里没有这些入口
    assert "记下：已放上网站" not in files["example/batch.html"]
    assert ("Acme Kettles", "Brand A") in report.names and MANUAL[0] in report.names


def test_numbers_and_check_results_are_left_alone(run):
    out, workspace, result = run
    files, _ = build(result.run_dir, workspace, manual=MANUAL)
    before = json.loads((result.run_dir / "change-package" / "manifest.json").read_text(encoding="utf-8"))
    after = json.loads(files["example/change-package/manifest.json"])
    assert len(after["items"]) == len(before["items"])
    assert [i["kind"] for i in after["items"]] == [i["kind"] for i in before["items"]]
    view = json.loads(files["example/run.json"])
    assert view["audit_counts"] == {k: sum(1 for f in result.audit.findings if f.level == k) for k in view["audit_counts"]}
    prices = [p["price"] for p in view["products"]]
    assert prices and prices == [" ".join(filter(None, [p.price.values[0].value if p.price.values else "",
                                                        p.currency.values[0].value if p.currency.values else ""]))
                                 for p in result.factbase.products]


def test_a_single_leftover_name_stops_the_whole_export(run, monkeypatch):
    out, workspace, result = run
    monkeypatch.setattr(export_module, "_rename_file", lambda rel, text, names: text)      # 假装改动包里的文件漏换了
    with pytest.raises(ShowcaseLeak) as caught:
        build(result.run_dir, workspace, manual=MANUAL)
    assert caught.value.found and all(path.startswith("example/change-package/") for path, _ in caught.value.found)
    assert "没有导出" in str(caught.value)


def test_a_run_without_a_batch_still_exports(tmp_path, acme_snapshot):
    result = process_snapshot(acme_snapshot, RunOptions(out_root=tmp_path / "out"), now=lambda: NOW, run_id="run-solo")
    files, report = build(result.run_dir, tmp_path / "empty-workspace", manual=MANUAL)
    assert "example/run.html" in files and "example/batch.html" not in files
    assert "看这批改动走到哪一步了" not in files["index.html"]
    with pytest.raises(ValueError):
        build(tmp_path / "no-such-run", tmp_path / "empty-workspace")


# ── 命令行 ────────────────────────────────────────────────────────────

def test_command_writes_a_new_folder_and_keeps_the_names_file_out_of_it(run, capsys, monkeypatch):
    out, workspace, result = run
    names_file = workspace / "showcase" / "names.yaml"
    dest = out / "showcase-site"
    base = ["showcase", "build", "--out", str(out), "--workspace", str(workspace), "--to", str(dest)]
    with monkeypatch.context() as patch:                                                    # 有文件漏换了:不导出,说明在哪
        patch.setattr(export_module, "_rename_file", lambda rel, text, names: text)
        assert main(base) == 1
    assert "搜得到真名" in capsys.readouterr().err and not dest.exists()
    assert names_file.is_file() and "names: []" in names_file.read_text(encoding="utf-8")  # 给了一份空的对照表让人补
    store.ensure_text_files(workspace / "showcase2", {"names.yaml": 'names:\n  - {real: "Acme", code: "Brand A"}\n'})
    args = [*base, "--names", str(workspace / "showcase2" / "names.yaml")]
    assert main(args) == 0
    said = capsys.readouterr().out
    assert "展示页导出在" in said and "run-show" in said and "人工补的 1 个" in said
    assert (dest / "index.html").is_file() and (dest / "example" / "run.html").is_file()
    assert not list(dest.rglob("names.yaml"))                                              # 对照表不随展示页导出
    assert main(args) == 1 and "已存在" in capsys.readouterr().err                          # 只新增,不覆盖
    assert main(["showcase", "build", "--out", str(out / "nothing-here"), "--workspace", str(workspace)]) == 1
