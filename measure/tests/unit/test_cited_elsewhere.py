"""第三方线 C1 的名单(用户 2026-10-28 定补):「AI 引用了、但没写到你」的第三方网页——聚合层数出来、
实验卡只在名单非空时出、报告证据区真的列出来(中英两套)。零网络零数据库。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from diagnosis.diagnose import diagnose_surface
from diagnosis.findings import Stage, StageStatus
from diagnosis.recommender import EvidenceEntry, load_evidence_registry, recommend
from diagnosis.representation_diag import diagnose_representation
from diagnosis.retrieval_diag import retrieval_not_observable
from diagnosis.third_party_diag import diagnose_third_party
from engine.aggregate import CITED_ELSEWHERE_LIMIT, CitedElsewhere, CitedPage, cited_elsewhere
from engine.models import (
    Answer,
    Citation,
    Outcome,
    PromptIntent,
    SearchMode,
    SourceAttribution,
    SourceAttributionObservation,
    SourceClass,
    SurfaceRun,
)
from engine.observe import RunObservations
from report.app import create_app
from tests.unit.test_report_page import DummyAdapter, finished_job, visible_text
from tests.unit.test_stage_diagnostics import make_metric

REGISTRY = load_evidence_registry()
C1 = "EVIDENCE_THIRDPARTY_CITED_PAGE_GAP_V1"


def _obs(run_id: str, cited: list[tuple[str, SourceClass, str | None]],
         titles: dict[str, str] | None = None) -> RunObservations:
    """一条回答:cited = [(引用链接, 归类, 存档时间戳)];titles = AI 返回的引用标题。"""
    titles = titles or {}
    run = SurfaceRun(
        run_id=run_id, prompt_id="p", run_index=1, provider="test_provider", surface="s1", model="m1",
        search_mode=SearchMode.DEFAULT, locale="zh-CN", outcome=Outcome.SUCCESS,
        answer=Answer(answer_text="回答", citations_available=True, citations=[
            Citation(url=url, domain="x", title=titles.get(url), position=i + 1) for i, (url, _, _) in enumerate(cited)
        ]),
        timestamp=datetime(2026, 10, 28, tzinfo=UTC), latency_ms=1, cost_usd=0.0,
    )
    attribution = SourceAttributionObservation(
        run_id=run_id, detector_name="source_attribution", detector_version=1, retrieved=None,
        cited=[SourceAttribution(url=url, final_url=url, source_class=kind, archived_at=archived)
               for url, kind, archived in cited],
    )
    return RunObservations(run=run, mention=None, citation=None, presence=None, sentiment=None, query=None,
                           competitor_citations={}, brand_cited_urls=[], attribution=attribution)


OTHER, MENTIONS = SourceClass.OTHER, SourceClass.THIRD_PARTY_MENTIONS


def test_counts_cited_third_party_pages_that_do_not_mention_you_once_per_answer():
    a, b, c = "https://www.review.test/best-crm", "https://blog.test/crm", "https://news.test/about-you"
    observations = [
        _obs("r1", [(a, OTHER, None), (a, OTHER, None), (b, OTHER, None)], titles={a: "Best CRM 2026"}),
        _obs("r2", [(a, OTHER, None), (c, MENTIONS, None)]),  # 写到你的不算
        _obs("r3", [(b, OTHER, "20260101000000"),  # 存档行:不算实时观测,不进名单
                    ("https://rival.test/", SourceClass.COMPETITOR_SITE, None),
                    ("https://gone.test/", SourceClass.NOT_OBSERVABLE, None)]),
    ]
    result = cited_elsewhere(observations)
    assert result is not None and result.total == 2
    assert [(p.domain, p.title, p.answers) for p in result.pages] == [
        ("review.test", "Best CRM 2026", 2),  # 一条回答里引两次只算一次;www. 去掉
        ("blog.test", None, 1),  # 平台没给标题:不猜
    ]
    assert cited_elsewhere([_obs("r4", [(c, MENTIONS, None)])]) is None  # 一个都没有 → None


def test_list_is_capped_but_the_total_is_kept():
    observations = [_obs(f"r{i}", [(f"https://site{i:02d}.test/", OTHER, None)])
                    for i in range(CITED_ELSEWHERE_LIMIT + 5)]
    result = cited_elsewhere(observations)
    assert result is not None and result.total == CITED_ELSEWHERE_LIMIT + 5
    assert len(result.pages) == CITED_ELSEWHERE_LIMIT


def test_c1_only_comes_out_when_the_list_exists():
    ids = lambda recs: {r.evidence_id for r in recs}  # noqa: E731
    with_list = recommend(REGISTRY, stage=Stage.CITATION, intent=PromptIntent.CATEGORY_DISCOVERY,
                          channel="third_party", data_available=frozenset({"third_party_cited_elsewhere"}))
    without = recommend(REGISTRY, stage=Stage.CITATION, intent=PromptIntent.CATEGORY_DISCOVERY,
                        channel="third_party", data_available=frozenset())
    assert C1 in ids(with_list) and C1 not in ids(without)
    assert ids(with_list) - {C1} == ids(without)  # 别的条目不受影响


def test_unknown_data_condition_in_the_registry_fails_loudly(tmp_path):
    entry = next(e for e in REGISTRY if e.id == C1)
    assert entry.requires_data == ["third_party_cited_elsewhere"]
    bad = tmp_path / "registry.yaml"
    raw = entry.model_dump(mode="json") | {"requires_data": ["typo_condition"]}
    import yaml

    bad.write_text(yaml.safe_dump({"entries": [raw]}, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="未知的数据条件"):
        load_evidence_registry(bad)
    assert isinstance(entry, EvidenceEntry)


def _surface(cited_list: CitedElsewhere | None):
    third_party = [diagnose_third_party(Stage.RETRIEVAL, make_metric(8, 12), None, {}),
                   diagnose_third_party(Stage.CITATION, make_metric(0, 12), None, {})]  # 引用步 FAIL
    own = diagnose_third_party(Stage.CITATION, make_metric(10, 12), None, {})  # 官网线随便一个 PASS
    return diagnose_surface(
        provider="test_provider", surface="s1", model="m1",
        access=own.model_copy(update={"stage": Stage.ACCESS}),
        retrieval=retrieval_not_observable(), citation=own,
        representation=diagnose_representation(PromptIntent.CATEGORY_DISCOVERY, make_metric(10, 12), {}),
        intent=PromptIntent.CATEGORY_DISCOVERY, registry=REGISTRY, third_party=third_party,
        third_party_cited_elsewhere=cited_list,
    )


LIST = CitedElsewhere(pages=[
    CitedPage(url="https://review.test/best-crm", domain="review.test", title="Best <CRM> 2026", answers=3),
    CitedPage(url="javascript:alert(1)", domain="evil.test", title=None, answers=1),
], total=4)


def test_diagnosis_carries_the_list_and_only_then_offers_c1():
    with_list, without = _surface(LIST), _surface(None)
    assert with_list.third_party_failure is Stage.CITATION and without.third_party_failure is Stage.CITATION
    assert with_list.third_party[1].status is StageStatus.FAIL
    assert C1 in {r.evidence_id for r in with_list.third_party_recommendations}
    assert C1 not in {r.evidence_id for r in without.third_party_recommendations}
    assert with_list.third_party_cited_elsewhere == LIST and without.third_party_cited_elsewhere is None


def test_report_lists_the_pages_in_both_languages_and_links_only_web_addresses():
    job = finished_job()
    cr = job.class_reports[0]
    first = cr.diagnosis.surfaces[0].model_copy(update={
        "third_party": _surface(LIST).third_party, "third_party_cited_elsewhere": LIST,
    })
    job.class_reports = [cr.model_copy(update={"diagnosis": cr.diagnosis.model_copy(update={
        "surfaces": [first, *cr.diagnosis.surfaces[1:]]})}), *job.class_reports[1:]]
    app = create_app(call_stats=lambda: [], adapters={"s1": DummyAdapter()}, dsn="postgresql://unused",
                     evidence=REGISTRY, job_runner=lambda **kw: None, calibration=None)
    app.state.ctx.registry.add(job)
    client = TestClient(app)
    zh = client.get(f"/jobs/{job.job_id}/report?lang=zh").text
    assert "AI 引用了、但没写到你的别人的文章(4 篇)" in zh  # 名单在折叠的证据区里
    assert "AI 引用了、但没写到你" not in visible_text(zh)  # 不进主视图
    assert "review.test · Best &lt;CRM&gt; 2026 · 3 次回答引用了它" in zh  # 外来标题转义
    assert 'href="https://review.test/best-crm"' in zh and "javascript:" not in zh.split("cited-elsewhere", 1)[1]
    assert "还有 2 篇没列出" in zh
    en = client.get(f"/jobs/{job.job_id}/report?lang=en").text
    assert "Other people&#39;s articles the AI cited that don&#39;t mention you (4)" in en
    assert "cited in 3 answer(s)" in en and "2 more not listed" in en
