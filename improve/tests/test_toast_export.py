"""读 Toast 的报告导出包(只读):官网域名、汇总里的灯、被引用的其他网站。

导出包的格式照 Toast 的 report/export.py(2026-11-14 核对,见 research/architecture-inputs/A)。
这里用的是按那个格式造的包,不是真从 Toast 导出来的。
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from butter.pipeline import RunOptions, process_snapshot
from butter.sources import SourceFileError, load_toast_export, parse_toast_summary

from conftest import NOW

SUMMARY = """# Acme Kettles 诊断报告

只代表这次采样。

## 采样
- 每题问 3 次

## 结论
- 提到你:3/24

## 找产品时
- ChatGPT(gpt-6-luna):官网访问(爬虫能不能进你的网站) 🟢 良好; 被搜到 🟡 不够稳定; 被引用 🔴 较弱
  - 第三方:评测站提到你 ⚪ 暂时无法判断
- Claude(claude-sonnet-5-5):官网访问(爬虫能不能进你的网站) 🟢 良好; 被搜到 🟢 良好

## 包里有什么
- prompts.csv
"""
CITATIONS = ("﻿run_id,prompt_no,run_index,ai,position,url,domain,final_url,source_class,competitor,reason\n"
             "r1,1,0,openai,1,https://kettlereview.test/a,kettlereview.test,,OTHER,,\n"
             "r1,1,0,openai,2,https://kettlereview.test/b,kettlereview.test,,OTHER,,\n"
             "r1,1,0,openai,3,https://www.acme-kettles.test/,www.acme-kettles.test,,OWN_SITE,,\n"
             "r1,2,0,openai,1,https://rival.test/,rival.test,,COMPETITOR_SITE,Rival,\n"
             "r1,2,0,openai,2,https://teablog.test/x,TeaBlog.test,,OTHER,,'=note\n")
BRAND = {"brand": {"name": "Acme Kettles", "aliases": ["Acme"], "domains": ["www.acme-kettles.test", "acme-shop.test"]},
         "list": None}


def make_zip(path, files: dict[str, str], prefix: str = ""):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(prefix + name, text.encode("utf-8"))
    path.write_bytes(buffer.getvalue())
    return path


FULL = {"summary.md": SUMMARY, "citations.csv": CITATIONS, "brand-domains.json": json.dumps(BRAND, ensure_ascii=False),
        "prompts.csv": "no,prompt_id\n", "responses.jsonl": ""}


def test_summary_lights_are_copied_not_rejudged():
    rows = parse_toast_summary(SUMMARY)
    assert [(r.section, r.surface, len(r.lights)) for r in rows] == [
        ("找产品时", "ChatGPT(gpt-6-luna)", 3), ("找产品时", "", 1), ("找产品时", "Claude(claude-sonnet-5-5)", 2)]
    first = rows[0].lights
    assert (first[0].label, first[0].light, first[0].text) == ("官网访问(爬虫能不能进你的网站)", "green", "🟢 良好")
    assert [x.light for x in first] == ["green", "yellow", "red"] and rows[1].lights[0].light == "white"
    assert parse_toast_summary("# 没有灯\n- 普通一行\n") == []


def test_export_zip_gives_domains_lights_and_other_cited_sites(tmp_path):
    export = load_toast_export(make_zip(tmp_path / "geo-job.zip", FULL))
    assert export.files_found == sorted(FULL) and not export.notes
    assert export.brand_domains.primary == "www.acme-kettles.test"
    assert export.brand_domains.confirmed_domains == ["acme-shop.test"]          # list 为空时也读得到(漏读已修)
    assert len(export.stage_lines) == 3
    assert [(c.domain, c.citations) for c in export.cited_other] == [("kettlereview.test", 2), ("teablog.test", 1)]
    from butter.sources import _csv_cell

    assert _csv_cell("'=SUM(A1)") == "=SUM(A1)" and _csv_cell("'quoted") == "'quoted" and _csv_cell("OTHER") == "OTHER"


def test_folder_nested_zip_and_missing_files(tmp_path):
    folder = tmp_path / "unzipped"
    folder.mkdir()
    (folder / "summary.md").write_text(SUMMARY, encoding="utf-8")
    export = load_toast_export(folder)
    assert len(export.stage_lines) == 3 and export.brand_domains is None and export.cited_other == []
    assert "导出包里没有 brand-domains.json" in export.notes and "导出包里没有 citations.csv" in export.notes
    nested = load_toast_export(make_zip(tmp_path / "nested.zip", FULL, prefix="geo-job/"))
    assert nested.brand_domains is not None and len(nested.stage_lines) == 3
    with pytest.raises(SourceFileError):
        load_toast_export(tmp_path / "nope.zip")
    with pytest.raises(SourceFileError):
        load_toast_export(make_zip(tmp_path / "other.zip", {"readme.txt": "hi"}))
    (tmp_path / "broken.zip").write_bytes(b"not a zip")
    with pytest.raises(SourceFileError):
        load_toast_export(tmp_path / "broken.zip")
    bad_json = load_toast_export(make_zip(tmp_path / "badjson.zip", {**FULL, "brand-domains.json": "{oops"}))
    assert bad_json.brand_domains is None and any("读不了" in n for n in bad_json.notes)


def test_pipeline_uses_the_export_and_quotes_it_in_the_audit_report(tmp_path, acme_snapshot):
    path = make_zip(tmp_path / "geo-job.zip", FULL)
    result = process_snapshot(acme_snapshot, RunOptions(out_root=tmp_path / "out", toast_export_path=path),
                              now=lambda: NOW, run_id="with-toast")
    merged = [m for m in result.factbase.merged_inputs if m.kind == "toast_brand_domains"]
    assert len(merged) == 1 and merged[0].status == "merged"                   # 导出包里的官网域名并进了事实库
    text = (result.run_dir / "audit.md").read_text(encoding="utf-8")
    assert "Toast 的诊断怎么说(照抄,粗粒度)" in text and "🟢 良好" in text and "Butter 没有重新判" in text
    assert "kettlereview.test:2 次" in text and "比 Toast 自己的那份多" in text
    meta = json.loads((result.run_dir / "run.json").read_text(encoding="utf-8"))
    assert len(meta["toast"]["stage_lines"]) == 3 and meta["inputs"]["toast_export"] == str(path)
    plain = process_snapshot(acme_snapshot, RunOptions(out_root=tmp_path / "out"), now=lambda: NOW, run_id="no-toast")
    assert "Toast 的诊断怎么说" not in (plain.run_dir / "audit.md").read_text(encoding="utf-8")
