"""双语测试(16.5):测量语境驱动起草 prompt 语言;界面语言 URL 切换。

两个维度严格分离:locale 是口径,lang 只是显示。
"""

import re

from fastapi.testclient import TestClient

from engine.models import BrandSpec, InformationNeed, NeedIntent, PromptOrigin
from engine.variants import generate_variants
from report.app import create_app
from tests.unit.test_needs import COMPETITORS_OK, RecordingAdapter
from tests.unit.test_report_page import REGISTRY, DummyAdapter, finished_job

BRAND = BrandSpec(name="AuroraCRM", domains=["aurora-crm.com"])

NEUTRAL_EN = {
    "discovery": [f"discovery q{i}" for i in range(1, 7)],
    "comparison": [f"comparison q{i}" for i in range(1, 5)],
    "scenario": [f"scenario q{i}" for i in range(1, 5)],
}
PROBES_EN = {"brand_probe": ["Is AuroraCRM good? Strengths and weaknesses?",
                             "How does AuroraCRM compare on value for money?"]}


def has_cjk(text: str) -> bool:
    return re.search(r"[一-鿿]", text) is not None


def test_en_locale_selects_english_drafting_prompts():
    from engine.needs import draft_need_candidates

    adapter = RecordingAdapter([NEUTRAL_EN, COMPETITORS_OK, PROBES_EN])
    draft_need_candidates(adapter, category="CRM software", brand=BRAND, locale="en-US")
    neutral_prompt, competitor_prompt, probe_prompt = adapter.prompts
    assert "Draft EXACTLY" in neutral_prompt and not has_cjk(neutral_prompt)
    assert "EXCLUDING" in competitor_prompt and not has_cjk(competitor_prompt)
    assert "TWO-SIDED" in probe_prompt and not has_cjk(probe_prompt)


def test_zh_locale_keeps_chinese_drafting_prompts():
    from engine.needs import draft_need_candidates
    from tests.unit.test_needs import NEUTRAL_OK, PROBES_OK

    adapter = RecordingAdapter([NEUTRAL_OK, COMPETITORS_OK, PROBES_OK])
    draft_need_candidates(adapter, category="CRM", brand=BrandSpec(name="极光CRM"), locale="zh-CN")
    assert has_cjk(adapter.prompts[0])


def test_variant_template_follows_locale():
    from datetime import UTC, datetime

    from engine.models import Answer, Outcome, SurfaceRun

    class LineAdapter:
        def __init__(self):
            self.prompt = None

        def ask(self, request):
            self.prompt = request.prompt_text
            return SurfaceRun(
                run_id=request.run_id, information_need_id=request.information_need_id,
                prompt_variant_id=request.prompt_variant_id,
                provider="p", surface="s", model="m",
                search_mode=request.search_mode, locale=request.locale,
                outcome=Outcome.SUCCESS,
                answer=Answer(answer_text="Which CRM fits a small team?",
                              citations_available=False),
                timestamp=datetime.now(UTC), latency_ms=1, cost_usd=0.0,
            )

    need = InformationNeed(statement="Best CRM for small teams", intent=NeedIntent.DISCOVERY)
    adapter = LineAdapter()
    variants = generate_variants(adapter, need, count=1, locale="en-US")
    assert not has_cjk(adapter.prompt)
    assert variants[0].origin is PromptOrigin.ASSISTED


def test_ui_language_toggle():
    app = create_app(adapters={"s1": DummyAdapter()}, dsn="postgresql://unused",
                     evidence=REGISTRY, job_runner=lambda **kw: None)
    client = TestClient(app)
    zh = client.get("/").text
    assert "品牌名" in zh
    en = client.get("/?lang=en").text
    assert "Brand name" in en and "品牌名" not in en


def test_report_page_fully_english():
    """v1.1:结论语句本身随界面语言渲染,英文报告不残留中文结论。"""
    app = create_app(adapters={"s1": DummyAdapter()}, dsn="postgresql://unused",
                     evidence=REGISTRY, job_runner=lambda **kw: None)
    job = finished_job()
    app.state.registry.add(job)
    client = TestClient(app)
    html = client.get(f"/jobs/{job.job_id}/report?lang=en").text
    assert "Four-Stage Diagnosis Report" in html
    assert "largely absent from retrieval candidates" in html  # L3 结论英文化
    assert "Significantly behind competitor" in html
    assert "NOT recommended" in html  # 不建议全页改写的英文警示
    assert "experiment recommended" in html  # C 级以下实验标注英文版
    assert "stop_reasons" in html and "EVIDENCE_" in html  # 口径要素不丢
    assert "品牌网站基本不进检索候选" not in html  # 中文结论不残留


def test_catalog_bilingual_completeness():
    """每个结论码必须 zh/en 双版齐备——组句永不静默缺语言。"""
    from diagnosis.messages import CATALOG

    for code, versions in CATALOG.items():
        assert set(versions) == {"zh", "en"}, code
        assert versions["zh"].strip() and versions["en"].strip(), code


def test_all_diagnosis_output_renders_in_both_languages():
    """把一份完整诊断的全部 Msg 在两种语言下渲染一遍:缺码/缺参立即炸。"""
    from diagnosis.messages import render_msg

    job = finished_job()
    diagnosis = job.need_reports[0].diagnosis
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
