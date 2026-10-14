"""下载结果文件(S8.4,产品文档 15.2「输出文件」):报告页一个按钮,从库里打包
prompts.csv / responses.jsonl / citations.csv /
brand-domains.json / summary.md;只读已存判定行;没完成的任务不给下;外来文字在数据文件里原样(不是网页,不转义)。"""

import csv
import io
import json
import zipfile
from datetime import UTC, datetime

from engine.models import BrandDomainList, BrandSpec
from engine.observe import ObserveContext, persist_observations
from engine.page_check import PageIndex
from report.jobs import DiagnosisJob
from tests.unit.test_report_page import finished_job
from tests.unit.test_response_page import ANSWER, _client, _run


def _prepared(storage):
    job = finished_job()
    job.domain_list = BrandDomainList(primary="aurora-crm.com", discovered_at=datetime(2026, 10, 14, tzinfo=UTC),
                                      public_suffix_list="t")
    prompt = job.prompts[0]
    storage.insert_prompt(prompt)
    runs = [_run(prompt.prompt_id, run_id="b1", index=1),
            _run(prompt.prompt_id, run_id="b2", index=2, text="别家更好。")]
    for run in runs:
        storage.insert_surface_run(run)
    persist_observations(storage, runs, job.brand, ObserveContext(competitors=[], pages=PageIndex()))
    return job


def test_bundle_has_the_five_files_built_from_the_database(storage):
    job = _prepared(storage)
    client = _client(job)
    resp = client.get(f"/jobs/{job.job_id}/download?lang=zh")
    assert resp.status_code == 200 and resp.headers["content-type"] == "application/zip"
    assert resp.headers["content-disposition"].startswith(f'attachment; filename="geo-{job.job_id}.zip"')
    archive = zipfile.ZipFile(io.BytesIO(resp.content))
    assert set(archive.namelist()) == {"prompts.csv", "responses.jsonl", "citations.csv", "brand-domains.json",
                                       "summary.md"}

    prompts = list(csv.reader(io.StringIO(archive.read("prompts.csv").decode("utf-8-sig"))))
    assert prompts[0] == ["no", "prompt_id", "intent", "origin", "locale", "text"]
    assert prompts[1][0] == "1" and prompts[1][2] == "category_discovery" and "CRM" in prompts[1][5]

    records = [json.loads(line) for line in archive.read("responses.jsonl").decode("utf-8").splitlines()]
    by_id = {r["run_id"]: r for r in records}
    assert set(by_id) == {"b1", "b2"} and by_id["b1"]["ai"] == "S1 · m1" and by_id["b1"]["prompt_no"] == 1
    assert by_id["b1"]["answer_text"] == ANSWER and "raw_payload" not in by_id["b1"]  # 原文照放,不含平台原始返回
    assert by_id["b1"]["judgements"]["mentioned"] is True
    assert by_id["b1"]["judgements"]["excerpt"] == "极光CRM 适合中小团队 <script>alert(1)</script>"  # 数据文件不转义
    assert by_id["b1"]["judgements"]["cited_brand_domains"] == ["aurora-crm.com"]
    assert by_id["b2"]["judgements"]["mentioned"] is False and by_id["b2"]["judgements"]["excerpt"] is None
    assert by_id["b1"]["judgements"]["detectors"]["brand_mention_rules"] == 2
    assert by_id["b1"]["timestamp"] == "2026-10-14T03:04:00+00:00"  # 一律 UTC

    cites = list(csv.reader(io.StringIO(archive.read("citations.csv").decode("utf-8-sig"))))
    assert cites[0][:6] == ["run_id", "prompt_no", "run_index", "ai", "position", "url"]
    own = [row for row in cites[1:] if row[5] == "https://www.aurora-crm.com/pricing" and row[0] == "b1"]
    assert own and own[0][8] == "OWN_SITE"
    third = [row for row in cites[1:] if row[5] == "https://review.test/best-crm" and row[0] == "b1"]
    assert third and third[0][8] == "NOT_OBSERVABLE" and third[0][10] == "NOT_ATTEMPTED"

    domains = json.loads(archive.read("brand-domains.json").decode("utf-8"))
    assert domains["brand"]["name"] == "极光CRM" and domains["list"]["primary"] == "aurora-crm.com"

    summary = archive.read("summary.md").decode("utf-8")
    assert summary.startswith("# 极光CRM AI 可见度报告") and "## 这次是怎么测的" in summary
    assert "题目 1 道 · 型号 2 个 · 每题问 3 次" in summary and "S1 · m1" in summary
    assert "## 各 AI 一句话" in summary and "## 建议先做" in summary and "## 文件说明" in summary
    assert "竞品提及对比(AI 回答里提到谁更多):你 12/30 · 竞品甲 20/30(比你多)" in summary
    assert "提到你的 18 句话里:正面 1(6%)、中性 9(50%)、负面 8(44%)" in summary

    en = _client(job).get(f"/jobs/{job.job_id}/download?lang=en")
    assert "## How this was measured" in zipfile.ZipFile(io.BytesIO(en.content)).read("summary.md").decode("utf-8")


def test_bundle_is_only_for_finished_jobs(storage):
    draft = DiagnosisJob(brand=BrandSpec(name="X", domains=["x.com"]), website="https://x.com", category="c",
                         locale="zh-CN", candidates=[], competitors_suggested=[])
    client = _client(draft)
    resp = client.get(f"/jobs/{draft.job_id}/download", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith(f"/jobs/{draft.job_id}")  # 回任务页
    assert client.get("/jobs/unknown/download").status_code == 404
