"""品牌常见写法(S9.2b,用户定「甲」):AI 列写法 → 在真实网页里数次数 → 出现过的预勾、太短的不勾;
报告末尾按 AI 回答里的实际写法数次数。零网络零数据库。"""

import json
from datetime import UTC, datetime

import pytest

from engine.aggregate import NameSpelling, tally_spellings
from engine.ai_task import TaskFailed
from engine.models import (
    Answer,
    BrandMentionObservation,
    BrandSpec,
    Citation,
    Outcome,
    PageCheck,
    PageFetchStatus,
    RetrievedSource,
    SearchMode,
    SearchTrace,
    SurfaceRun,
)
from engine.observe import RunObservations
from engine.page_check import PageIndex, PageRequest, normalize_url
from engine.spellings import MAX_SPELLINGS, SpellingCandidate, suggest_spellings

BRAND = BrandSpec(name="极光CRM", aliases=["AuroraCRM"], domains=["aurora-crm.com"])
WEBSITE = "https://aurora-crm.com"


class SearchingAdapter:
    """返回脚本化 JSON,并带上「搜到」与「引用」的网页,像真的联网调用一样。"""

    def __init__(self, payloads: list, citations: list[str] = (), sources: list[str] = ()) -> None:
        self._payloads = list(payloads)
        self.prompts: list[str] = []
        self._citations, self._sources = list(citations), list(sources)

    def ask(self, request) -> SurfaceRun:
        self.prompts.append(request.prompt_text)
        payload = self._payloads.pop(0)
        return SurfaceRun(
            run_id=request.run_id, prompt_id=request.prompt_id, run_index=request.run_index,
            provider="test_provider", surface="test_surface", model="m1",
            search_mode=request.search_mode, locale=request.locale, outcome=Outcome.SUCCESS,
            answer=Answer(answer_text=json.dumps(payload, ensure_ascii=False), citations_available=True,
                          citations=[Citation(url=u, domain=u.split("/")[2], position=i + 1)
                                     for i, u in enumerate(self._citations)]),
            search_trace=SearchTrace(search_triggered=True, retrieved_sources=[
                RetrievedSource(url=u, domain=u.split("/")[2], rank=i + 1) for i, u in enumerate(self._sources)]),
            timestamp=datetime.now(UTC), latency_ms=1, cost_usd=0.01,
        )


class FakeOpener:
    def __init__(self, texts: dict[str, str]) -> None:
        self._texts = texts
        self.requests: list[PageRequest] = []

    def check(self, requests) -> PageIndex:
        self.requests = list(requests)
        checks = {}
        for r in self.requests:
            key = normalize_url(r.url)
            text = self._texts.get(r.url)
            checks[key] = PageCheck(url=r.url, normalized_url=key, fetcher_version=1,
                                    status=PageFetchStatus.OK if text else PageFetchStatus.FETCH_FAILED,
                                    text=text, text_source="main" if text else None)
        return PageIndex(checks=checks, attempted=frozenset(checks))


PAYLOAD = {"spellings": [
    {"text": "Aurora CRM", "reason": "英文名加空格的写法"},
    {"text": "极光crm", "reason": "小写写法"},
    {"text": "AuroraCRM", "reason": "已登记的英文名(应被去掉)"},
    {"text": "极光CRM", "reason": "就是登记名(应被去掉)"},
    {"text": "JG", "reason": "拼音首字母简称(太短)"},
    {"text": "aurora crm", "reason": "重复的大小写变化(应去重)"},
    {"text": "Aurora", "reason": "只写前半截"},
    {"text": "Aurora Cloud CRM", "reason": "旧名(网页里已经没有)"},
]}


def test_ai_listed_spellings_are_counted_in_real_pages_and_preselected_by_evidence():
    adapter = SearchingAdapter([PAYLOAD], citations=["https://review.test/best"], sources=["https://forum.test/t/1"])
    opener = FakeOpener({
        WEBSITE: "极光CRM 官网。Aurora CRM helps teams. Aurora CRM pricing.",
        "https://review.test/best": "We compared Aurora CRM and others. JG is not here.",
        "https://forum.test/t/1": "极光crm 怎么样?极光crm 还行。",
    })
    out = suggest_spellings(adapter, brand=BRAND, website=WEBSITE, locale="zh-CN", ledger=lambda run: None,
                            pages=opener)
    assert "极光CRM" in adapter.prompts[0] and "aurora-crm.com" in adapter.prompts[0]
    # 去掉登记名、已登记别名、它们的大小写变化(匹配本来就不分大小写)与重复;按次数排序,同分保持 AI 的顺序
    assert [c.text for c in out] == ["Aurora CRM", "Aurora", "JG", "Aurora Cloud CRM"]
    aurora = out[0]
    assert (aurora.pages_seen, aurora.pages_with, aurora.occurrences, aurora.preselected) == (3, 2, 3, True)
    assert out[1].occurrences == 3 and out[1].preselected is True  # 「Aurora CRM」里的 Aurora 按词边界也算
    assert out[2].occurrences == 1 and out[2].short is True and out[2].preselected is False  # 两个字符:太短不预勾
    assert out[3].occurrences == 0 and out[3].preselected is False  # 网页里没出现:不预勾
    # 官网不看 robots,第三方尊重;引用的在前、搜到的在后
    by_url = {r.url: r for r in opener.requests}
    assert by_url[WEBSITE].respect_robots is False and by_url["https://review.test/best"].respect_robots is True
    assert [r.url for r in opener.requests] == [WEBSITE, "https://review.test/best", "https://forum.test/t/1"]


def test_english_prompt_and_cap_at_eight():
    many = {"spellings": [{"text": f"Variant{i}", "reason": "x"} for i in range(12)]}
    adapter = SearchingAdapter([many])
    out = suggest_spellings(adapter, brand=BRAND, website=WEBSITE, locale="en-US", ledger=lambda run: None,
                            pages=FakeOpener({}))
    assert "Using web search" in adapter.prompts[0]
    assert len(out) == MAX_SPELLINGS and all(c.pages_seen == 0 and not c.preselected for c in out)


def test_bad_json_three_times_is_an_explicit_failure():
    adapter = SearchingAdapter([{"nope": 1}, {"spellings": "x"}, {"spellings": [1]}])
    with pytest.raises(TaskFailed):
        suggest_spellings(adapter, brand=BRAND, website=WEBSITE, locale="zh-CN", ledger=lambda run: None,
                          pages=FakeOpener({}))


def test_candidate_model_round_trips():
    c = SpellingCandidate(text="Aurora CRM", reason="r", pages_seen=3, pages_with=2, occurrences=3, short=False)
    assert SpellingCandidate.model_validate(c.model_dump()) == c and c.preselected


# ── 报告末尾:AI 回答里的写法 ─────────────────────────────────────────────────────────


def _obs(text: str | None, alias: str | None = None, mentioned: bool = True) -> RunObservations:
    run = SurfaceRun(run_id="r", prompt_id="p", run_index=1, provider="p", surface="s", model="m",
                     search_mode=SearchMode.DEFAULT, locale="zh-CN", outcome=Outcome.SUCCESS,
                     answer=Answer(answer_text="…", citations_available=True), timestamp=datetime.now(UTC),
                     latency_ms=1, cost_usd=0.0)
    mention = BrandMentionObservation(run_id="r", detector_name="d", detector_version=1, mentioned=mentioned,
                                      matched_text=text, matched_alias=alias)
    return RunObservations(run=run, mention=mention, citation=None, presence=None, sentiment=None, query=None,
                           competitor_citations={}, brand_cited_urls=[])


def test_tally_spellings_counts_actual_wordings_and_flags_unregistered():
    observations = [_obs("auroracrms"), _obs("auroracrms"), _obs("极光CRM"), _obs("极光 CRM"),
                    _obs(None, alias="AuroraCRM"), _obs("Auroracrms"), _obs(None, mentioned=False)]
    out = tally_spellings(observations, BRAND)
    assert out == [
        NameSpelling(text="auroracrms", count=3, registered=False),  # 大小写合并,显示第一次见到的写法
        NameSpelling(text="极光CRM", count=1, registered=True),
        NameSpelling(text="极光 CRM", count=1, registered=False),  # 带空格的写法没登记
        NameSpelling(text="AuroraCRM", count=1, registered=True),  # 规则判定器没有命中片段:用登记名
    ]
    assert tally_spellings([_obs("x")] * 20, BRAND, limit=1) == [NameSpelling(text="x", count=20, registered=False)]
