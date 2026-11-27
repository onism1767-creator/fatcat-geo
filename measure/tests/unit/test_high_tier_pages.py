"""高档单(带 AI 裁判)的单条回答页:只读已存的判定行,不需要装配裁判(第二轮红 3:原来直接 500)。
库用假存储替身,零网络零数据库。"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from engine.models import Answer, Outcome, SearchMode, SurfaceRun
from engine.observe import mention_detector_name, sentiment_detector_name
from engine.tiers import MentionMethod, TierChoice
from report.app import create_app
from tests.unit.test_report_page import REGISTRY, DummyAdapter, finished_job

HIGH = TierChoice(name="high", runs_per_prompt=3, mention_method=MentionMethod.RULES_FUZZY_JUDGE, custom=False)


def test_detector_names_for_reading_stored_rows_never_need_a_judge():
    assert mention_detector_name(MentionMethod.RULES_FUZZY_JUDGE) == "brand_mention_judge"
    assert sentiment_detector_name(MentionMethod.RULES_FUZZY_JUDGE) == "sentiment_judge"
    assert mention_detector_name(MentionMethod.RULES) == "brand_mention_rules"
    assert sentiment_detector_name(MentionMethod.RULES_FUZZY) == "sentiment_lexicon"


def test_high_tier_response_page_renders_the_judges_verdict(monkeypatch):
    job = finished_job()
    job.tier = HIGH
    prompt = job.prompts[0]
    run = SurfaceRun(run_id="run-h", prompt_id=prompt.prompt_id, run_index=1, provider="test_provider", surface="s1",
                     model="m1", search_mode=SearchMode.DEFAULT, locale="zh-CN", outcome=Outcome.SUCCESS,
                     answer=Answer(answer_text="推荐极光CRM,口碑不错。", citations_available=True),
                     timestamp=datetime(2026, 10, 29, tzinfo=UTC), latency_ms=1, cost_usd=0.0)
    rows = [
        {"observation_type": "brand_mention", "detector_name": "brand_mention_judge", "detector_version": 1,
         "computed_at": "2026-10-29T00:00:00+00:00", "mentioned": True, "matched_alias": None,
         "matched_text": "极光CRM", "match_kind": "judge", "first_position": 2},
        {"observation_type": "sentiment", "detector_name": "sentiment_judge", "detector_version": 1,
         "computed_at": "2026-10-29T00:00:00+00:00", "sentiment": "positive"},
    ]

    class FakeStorage:
        def __init__(self, dsn: str) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            pass

        def run_by_id(self, run_id: str):
            return run if run_id == run.run_id else None

        def observations_for_run(self, run_id: str):
            return rows

    monkeypatch.setattr("report.routes.Storage", FakeStorage)
    app = create_app(call_stats=lambda brand=None: [], adapters={"s1": DummyAdapter()}, dsn="postgresql://unused",
                     evidence=REGISTRY, job_runner=lambda **kw: None, calibration=None)
    app.state.ctx.registry.add(job)
    client = TestClient(app)
    zh = client.get(f"/jobs/{job.job_id}/responses/run-h?lang=zh")
    assert zh.status_code == 200
    assert "AI 裁判" in zh.text and "极光CRM" in zh.text and "正面" in zh.text  # 裁判给的情感也取得到
    en = client.get(f"/jobs/{job.job_id}/responses/run-h?lang=en")
    assert en.status_code == 200 and "AI judge" in en.text
