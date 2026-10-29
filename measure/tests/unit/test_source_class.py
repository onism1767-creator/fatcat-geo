"""来源归属(架构书附录 v0.4.24 D1;S6.1):五类 + 官方渠道,看不到的记原因;聚合成第三方线的「N 次里 k 次」。"""

from datetime import UTC, datetime

from engine.aggregate import measure_target
from engine.detectors.source_class import SourceClassifier
from engine.models import (
    NOT_OBSERVABLE,
    Answer,
    BrandSpec,
    Citation,
    Outcome,
    PageCheck,
    PageFetchStatus,
    PromptIntent,
    RetrievedSource,
    SearchMode,
    SearchTrace,
    SourceClass,
    StopReason,
    SurfaceRun,
    Target,
)
from engine.observe import ObserveContext, observe
from engine.page_check import PageIndex
from engine.resolve import ResolutionContext

BRAND = BrandSpec(name="极光CRM", aliases=["AuroraCRM"], domains=["aurora-crm.com"],
                  official_channels=["aurora-crm.tmall.com/"])
RIVAL = BrandSpec(name="Beta", domains=["beta.example"])
FILLER = "a long enough paragraph of review text " * 5


def _page(url: str, status=PageFetchStatus.OK, *, title: str | None = None, text: str | None = None,
          text_source: str | None = None, archived: str | None = None,
          live_status: PageFetchStatus | None = None) -> PageCheck:
    return PageCheck(url=url, normalized_url=url, status=status, title=title, text=text, fetcher_version=1,
                     text_source=text_source or ("main" if text else None), archive_timestamp=archived,
                     live_status=live_status,
                     archive_url=f"https://web.archive.org/web/{archived}/{url}" if archived else None)


PAGES = PageIndex(checks={
    "https://review.test/aurora": _page("https://review.test/aurora", title="AuroraCRM review",
                                        text=f"AuroraCRM {FILLER}"),
    "https://blog.test/crm": _page("https://blog.test/crm", title="CRM tips", text=f"{FILLER}\n极光CRM is one option"),
    "https://other.test/x": _page("https://other.test/x", title="Nothing", text=FILLER),
    "https://wall.test/y": _page("https://wall.test/y", status=PageFetchStatus.BLOCKED),
    "https://video.test/v": _page("https://video.test/v", title="AuroraCRM demo", text="AuroraCRM demo\n\nA tour.",
                                  text_source="meta"),
    "https://old.test/a": _page("https://old.test/a", title="AuroraCRM in 2025", text=f"AuroraCRM {FILLER}",
                                text_source="archive_main", archived="20250102030405",
                                live_status=PageFetchStatus.BLOCKED),
}, attempted=frozenset({"https://review.test/aurora", "https://blog.test/crm", "https://other.test/x",
                        "https://wall.test/y", "https://video.test/v", "https://old.test/a"}))


def _run(run_id: str, cited: list[str], retrieved: list[str] | None = None, *, prompt_id: str = "p1",
         surface: str = "s1") -> SurfaceRun:
    return SurfaceRun(
        run_id=run_id, prompt_id=prompt_id, run_index=1, provider="p", surface=surface, model="m",
        search_mode=SearchMode.DEFAULT, locale="zh-CN", outcome=Outcome.SUCCESS,
        search_trace=SearchTrace(search_triggered=True, retrieved_sources=(
            [RetrievedSource(url=u, domain=u.split("/")[2], rank=i + 1) for i, u in enumerate(retrieved)]
            if retrieved is not None else NOT_OBSERVABLE)),
        answer=Answer(answer_text="回答", citations_available=True,
                      citations=[Citation(url=u, domain=u.split("/")[2], position=i + 1) for i, u in enumerate(cited)]),
        raw_payload={}, timestamp=datetime(2026, 10, 13, tzinfo=UTC), latency_ms=1, cost_usd=0.0,
    )


def test_every_source_lands_in_exactly_one_class_with_reasons_for_the_unobservable():
    resolution = ResolutionContext(frozenset({"redir"}), {"https://r.test/ok": "https://review.test/aurora"})
    classifier = SourceClassifier([RIVAL], resolution, PAGES)
    run = _run("r1", [
        "https://www.aurora-crm.com/a", "https://aurora-crm.tmall.com/item/1", "https://beta.example/b",
        "https://review.test/aurora", "https://blog.test/crm", "https://other.test/x", "https://wall.test/y",
        "https://never.test/z",
    ], retrieved=["https://review.test/aurora", "https://other.test/x"])
    observation = classifier.detect(run, BRAND)
    assert observation is not None and observation.cited is not None and observation.retrieved is not None
    assert [(a.source_class, a.position, a.reason, a.competitor) for a in observation.cited] == [
        (SourceClass.OWN_SITE, None, None, None),
        (SourceClass.OFFICIAL_CHANNEL, None, None, None),
        (SourceClass.COMPETITOR_SITE, None, None, "Beta"),
        (SourceClass.THIRD_PARTY_MENTIONS, "title", None, None),
        (SourceClass.THIRD_PARTY_MENTIONS, "body", None, None),
        (SourceClass.OTHER, None, None, None),
        (SourceClass.NOT_OBSERVABLE, None, "BLOCKED", None),
        (SourceClass.NOT_OBSERVABLE, None, "NOT_ATTEMPTED", None),
    ]
    assert [a.source_class for a in observation.retrieved] == [SourceClass.THIRD_PARTY_MENTIONS, SourceClass.OTHER]
    # 跳转仪器:按终点判;还原失败 → 看不到(UNRESOLVED)
    redirect_run = _run("r2", ["https://r.test/ok", "https://r.test/lost"], surface="redir")
    redirect_obs = classifier.detect(redirect_run, BRAND)
    assert [(a.source_class, a.final_url, a.reason) for a in redirect_obs.cited] == [
        (SourceClass.THIRD_PARTY_MENTIONS, "https://review.test/aurora", None),
        (SourceClass.NOT_OBSERVABLE, None, "UNRESOLVED"),
    ]
    assert redirect_obs.retrieved is None  # 候选集不可观测


def test_third_party_line_aggregates_apart_from_the_own_site_line():
    """一次回答:至少一条写到你 → 有;只有官网 / 没写到你的 → 没有;全部看不到 → 不进分母。标题或首段另计。"""
    context = ObserveContext(competitors=[RIVAL], pages=PAGES)
    runs = [
        _run("a", ["https://review.test/aurora"], ["https://other.test/x"]),           # 引用:有(标题);搜到:没有
        _run("b", ["https://www.aurora-crm.com/a", "https://other.test/x"], []),        # 引用:没有;搜到:没有(空候选集)
        _run("c", ["https://wall.test/y", "https://never.test/z"], None),  # 引用全看不到 → 不进分母;搜到不可观测
        _run("d", ["https://blog.test/crm"], ["https://review.test/aurora"]),           # 引用:有(正文);搜到:有
        _run("e", ["https://video.test/v"], ["https://video.test/v"]),                   # 只拿到摘要:算,但另计
        _run("f", ["https://old.test/a"], None),                                         # 只有存档:不进实时比例
    ]
    observations = [observe(run, BRAND, context) for run in runs]
    m = measure_target(Target(surface="s1", model="m"), "p", PromptIntent.CATEGORY_DISCOVERY, observations, ["p1"],
                       stop_reason=StopReason.COMPLETED, competitor_names=["Beta"], exposes_retrieval=True)
    tp = m.third_party
    assert tp is not None and tp.cited is not None and tp.cited.rate is not None
    assert (tp.cited.rate.k, tp.cited.rate.n) == (3, 4)  # c 全部看不到、f 只有存档:都不进分母;e 按摘要判也算
    assert (tp.cited_lead.k, tp.cited_lead.n) == (2, 3)  # a、e 标题就出现;d 只在正文
    assert (tp.retrieved.rate.k, tp.retrieved.rate.n) == (2, 4)  # c、f 候选集不可观测,不进
    assert tp.unobservable_cited == {"BLOCKED": 2, "NOT_ATTEMPTED": 1}  # f 的存档行按实时原因(被拦)计看不到
    assert tp.unobservable_retrieved == {}
    assert tp.summary_only == {"meta": 2}  # e 的引用 + 搜到各一条只拿到摘要,另计不并入
    assert tp.archived is not None and (tp.archived.pages, tp.archived.mentioning) == (1, 1)
    assert (tp.archived.oldest, tp.archived.newest) == ("2025-01-02", "2025-01-02")
    assert (m.citation.rate.k, m.citation.rate.n) == (1, 6)  # 官网线照旧


def test_malformed_urls_are_unattributable_not_a_crash():
    """坏网址(第二轮审核 B4):取不出主机名就当看不出是谁的网页,判定不崩。"""
    from engine.detectors.citation import channel_hit
    from engine.detectors.matching import url_host

    assert url_host("http://[bad/x") == "" and url_host("https://a.test:abc/x") == "a.test"
    assert channel_hit("http://[bad/x", ["brand.tmall.com/"]) is None
