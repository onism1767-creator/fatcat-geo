"""双语测试(16.5):测量语境驱动起草 prompt 语言;界面语言 URL 切换。

两个维度严格分离:locale 是口径,lang 只是显示。
"""

import re

from fastapi.testclient import TestClient

from engine.models import BrandSpec, InformationNeed, NeedIntent, PromptOrigin
from engine.variants import generate_variants
from tests.unit.test_needs import COMPETITORS_OK, RecordingAdapter
from tests.unit.test_report_page import REGISTRY, DummyAdapter, finished_job
from report.app import create_app

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


def test_report_page_english_chrome():
    app = create_app(adapters={"s1": DummyAdapter()}, dsn="postgresql://unused",
                     evidence=REGISTRY, job_runner=lambda **kw: None)
    job = finished_job()
    app.state.registry.add(job)
    client = TestClient(app)
    html = client.get(f"/jobs/{job.job_id}/report?lang=en").text
    assert "Four-Stage Diagnosis Report" in html
    assert "evidence level" in html
    assert "structured bilingual output ships in v1.1" in html  # 结论文本语言的诚实标注
    # 结论语句本身仍为中文(v1.1 前),但界面语言切换不得丢失口径要素
    assert "stop_reasons" in html and "EVIDENCE_" in html