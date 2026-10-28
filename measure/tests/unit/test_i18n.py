"""双语测试(16.5):测量语境驱动起草 prompt 语言;界面语言 URL 切换。

两个维度严格分离:locale 是口径,lang 只是显示。
"""

import re

from fastapi.testclient import TestClient

from engine.models import BrandSpec
from report.app import create_app
from tests.unit.test_needs import COMPARISON_OK, COMPETITORS_OK, FACTS_OK, RecordingAdapter
from tests.unit.test_report_page import REGISTRY, DummyAdapter, finished_job

BRAND = BrandSpec(name="AuroraCRM", domains=["aurora-crm.com"])

BLIND_EN = {
    "category_discovery": [f"discovery q{i}" for i in range(1, 9)],
    "problem_solution": [f"problem q{i}" for i in range(1, 5)],
    "recommendation": [f"recommendation q{i}" for i in range(1, 5)],
}
COMPARISON_EN = {"comparison_alternative": [f"comparison q{i}" for i in range(1, 5)]}
COMPETITORS_EN = {"competitors": [{"name": "SalesLoop", "domain": "salesloop.example"},
                                  {"name": "CloudDesk", "domain": "clouddesk.example"}]}  # 英文题的竞品名也是英文
FACTS_EN = {"brand_fact": ["Is AuroraCRM good? Strengths and weaknesses?", "What does AuroraCRM do?",
                           "How does AuroraCRM compare on value for money?", "Who is AuroraCRM for?"]}


def has_cjk(text: str) -> bool:
    return re.search(r"[一-鿿]", text) is not None


def test_en_locale_selects_english_drafting_prompts():
    from engine.drafting import draft_prompt_candidates

    adapter = RecordingAdapter([BLIND_EN, COMPETITORS_EN, COMPARISON_EN, FACTS_EN])
    draft_prompt_candidates(adapter, category="CRM software", brand=BRAND, locale="en-US", ledger=lambda run: None)
    blind_prompt, competitor_prompt, comparison_prompt, facts_prompt = adapter.prompts
    assert "Draft EXACTLY" in blind_prompt and not has_cjk(blind_prompt)
    assert "EXCLUDING" in competitor_prompt and not has_cjk(competitor_prompt)
    assert "alternatives" in comparison_prompt and not has_cjk(comparison_prompt)
    assert "TWO-SIDED" in facts_prompt and not has_cjk(facts_prompt)


def test_zh_locale_keeps_chinese_drafting_prompts():
    from engine.drafting import draft_prompt_candidates
    from tests.unit.test_needs import BLIND_OK

    adapter = RecordingAdapter([BLIND_OK, COMPETITORS_OK, COMPARISON_OK, FACTS_OK])
    draft_prompt_candidates(adapter, category="CRM", brand=BrandSpec(name="极光CRM"), locale="zh-CN",
                          ledger=lambda run: None)
    assert has_cjk(adapter.prompts[0])


def test_ui_language_toggle():
    app = create_app(call_stats=lambda: [], adapters={"s1": DummyAdapter()}, dsn="postgresql://unused",
                     evidence=REGISTRY, job_runner=lambda **kw: None)
    client = TestClient(app)
    default = client.get("/").text
    assert "Brand name" in default and "品牌名" not in default  # 默认英文(产品文档 D16;S8.6)
    zh = client.get("/?lang=zh").text
    assert "品牌名" in zh
    en = client.get("/?lang=en").text
    assert "Brand name" in en and "品牌名" not in en


def test_report_page_fully_english():
    """v1.1:结论语句本身随界面语言渲染,英文报告不残留中文结论。"""
    app = create_app(call_stats=lambda: [], adapters={"s1": DummyAdapter()}, dsn="postgresql://unused",
                     evidence=REGISTRY, job_runner=lambda **kw: None)
    job = finished_job()
    app.state.ctx.registry.add(job)
    client = TestClient(app)
    html = client.get(f"/jobs/{job.job_id}/report?lang=en").text
    assert "AI Visibility Report" in html
    assert "the brand rarely reaches the model" in html  # L3 结论英文化(s2 生成端检索 FAIL;撇号会被转义)
    assert "points behind competitor" in html
    assert "NOT recommended" in html  # 不建议全页改写的英文警示
    assert "experiment recommended" in html  # C 级以下实验标注英文版
    assert "stop_reasons" in html and "EVIDENCE_" in html  # 口径要素不丢
    assert "在 AI 自己的搜索习惯下" not in html  # 中文结论不残留


def test_catalog_bilingual_completeness():
    """每个结论码必须 zh/en 双版齐备——组句永不静默缺语言。"""
    from diagnosis.messages import CATALOG, PLAIN

    for code, versions in CATALOG.items():
        assert set(versions) == {"zh", "en"}, code
        assert versions["zh"].strip() and versions["en"].strip(), code
    for code, versions in PLAIN.items():  # 人话目录(v0.4.14):码必须存在于技术目录,且双语齐备
        assert code in CATALOG, code
        assert set(versions) == {"zh", "en"} and versions["zh"].strip() and versions["en"].strip(), code


def test_plain_register_renders_counts_and_falls_back():
    """用户视图人话版:比例写「N 次里 k 次(p%)」——计数在前、百分比跟后(用户定 S8);无人话版的码回落技术版。"""
    from diagnosis.findings import rate_params
    from diagnosis.messages import msg, render_msg
    from engine.metrics import rate

    line = msg("citation.rate", **rate_params(rate(7, 16)))
    # 计数在前、百分比跟在后面(用户定 S8)
    assert render_msg(line, "zh", "plain") == "本次 16 次回答里,7 次(44%)拿你的官网当参考"
    assert render_msg(line, "zh") == "被引比例:7/16 = 44%"  # 审计视图:计数 + 比例,不出区间
    nested = msg("caution.unobservable", stages=[msg("stage.ACCESS"), msg("stage.RETRIEVAL")])
    assert "官网访问(爬虫能不能进你的网站)" in render_msg(nested, "zh", "plain")  # 正式用词(口语解释)
    assert render_msg(nested, "zh") == "以下阶段不可观测,未参与瓶颈定位:ACCESS、RETRIEVAL"
    assert render_msg(msg("common.caliber", surface="s", model="m", n_prompts=1, n_valid=1, n_total=1,
                          stop_reasons="", n_refused=0, n_empty=0, n_failed=0), "zh", "plain").startswith("口径")


def test_all_diagnosis_output_renders_in_both_languages():
    """把一份完整诊断的全部 Msg 在两种语言下渲染一遍:缺码/缺参立即炸。"""
    from diagnosis.messages import render_msg

    job = finished_job()
    diagnosis = job.class_reports[0].diagnosis
    messages = [diagnosis.access.headline, *diagnosis.access.evidence_lines]
    for sd in diagnosis.surfaces:
        messages += [f.headline for f in sd.findings]
        messages += [line for f in sd.findings for line in f.evidence_lines]
        messages += sd.cautions
        if sd.retrieval_dependency is not None:
            messages.append(sd.retrieval_dependency.headline)
            messages += sd.retrieval_dependency.evidence_lines
    assert messages
    for lang in ("zh", "en"):
        for message in messages:
            assert render_msg(message, lang).strip()


def test_message_lists_are_joined_per_language():
    """英文输出里不能出现中文顿号(审核 2026-10-27 黄 22)。"""
    from diagnosis.messages import msg, render_msg

    nested = msg("caution.unobservable", stages=[msg("stage.ACCESS"), msg("stage.RETRIEVAL")])
    assert render_msg(nested, "zh").endswith("ACCESS、RETRIEVAL")
    assert render_msg(nested, "en").endswith("ACCESS, RETRIEVAL") and "、" not in render_msg(nested, "en")
