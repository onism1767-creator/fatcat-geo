"""Grok 开着 X 帖子搜索的那台仪器的契约测试(12.1;2026-11-20 用户定「让用户可以自选」)。

它和只搜网页的那台共用请求与翻译(那台的契约测试把守共用部分),这里只把守多出来的:请求里多带的工具、
X 搜索词、标了出处的帖子、按帖子条数的计费、它自己的仪器名与说明。
golden 是 2026-11-18 两个工具都开着的**真实回包**裁出来的(裁法同那台:每次网页搜索只留前 3 个候选 + 被引用到的,
正文留到第一条 X 帖子出处之后,用量与费用原样):
- success_x_posts_cited.json   grok-4.5 口碑题:5 次网页搜索 + 3 次 X 搜索(抓回 29 条帖子),回答里标了帖子的出处
- success_x_unanchored.json    grok-4.7 口碑题:8 次网页搜索 + 1 次 X 搜索(抓回 5 条),正文不标出处、只挂零宽度清单
全部 respx 模拟,零真实网络。
"""

import json
from pathlib import Path

import httpx
import pytest
import respx

from adapters.base import AdapterPermanentError, AdapterTransientError, priced
from adapters.grok_web_search import GrokWebSearchAdapter
from adapters.grok_web_xposts_search import GrokWebXPostsSearchAdapter
from engine.models import AskRequest, Outcome, SearchMode

GOLDEN = Path(__file__).parent / "golden" / "grok_web_xposts_search"
RESP_URL = "https://api.x.ai/v1/responses"


def golden(name: str) -> dict:
    return json.loads((GOLDEN / name).read_text(encoding="utf-8"))


def make_request(**overrides) -> AskRequest:
    fields = dict(run_id="run-1", prompt_id="need-1", run_index=1,
                  prompt_text="What are people saying about Notion lately? Is it still worth using?", locale="en-US")
    fields.update(overrides)
    return AskRequest(**fields)


@respx.mock
def test_request_carries_both_tools_and_nothing_else_changes():
    """口径钉死:只比只搜网页的那台多一个 X 搜索工具;闭卷腿一个工具都不带(和那台的闭卷是同一种请求)。"""
    route = respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=golden("success_x_posts_cited.json")))
    adapter = GrokWebXPostsSearchAdapter(api_key="test-key")
    adapter.ask(make_request())
    assert json.loads(route.calls[0].request.content) == {
        "model": "grok-4.5", "input": "What are people saying about Notion lately? Is it still worth using?",
        "max_output_tokens": 4096, "store": False,
        "tools": [{"type": "web_search"}, {"type": "x_search"}],
    }
    adapter.ask(make_request(search_mode=SearchMode.SEARCH_DISABLED))
    assert "tools" not in json.loads(route.calls[1].request.content)


@respx.mock
def test_x_queries_are_seen_and_cited_posts_enter_the_citation_pool():
    """X 搜索词和网页搜索词按出现顺序放在一起;回答里标了出处的帖子进引用;搜到了哪些帖子平台不列,
    候选来源里只有网页(不拿别处的清单代偿,铁律 7)。"""
    body = golden("success_x_posts_cited.json")
    respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=body))
    run = GrokWebXPostsSearchAdapter(api_key="test-key").ask(make_request())

    assert (run.provider, run.surface, run.model, run.outcome) == ("xai", "grok_web_xposts_search", "grok-4.5",
                                                                  Outcome.SUCCESS)
    x_calls = [item for item in body["output"] if item["type"] == "custom_tool_call"]
    web_calls = [item for item in body["output"] if item["type"] == "web_search_call"]
    assert {item["name"] for item in x_calls} == {"x_semantic_search", "x_keyword_search"} and len(x_calls) == 3
    trace = run.search_trace
    searches = [item for item in web_calls if item["action"]["type"] == "search"]
    opens = [item["action"]["url"] for item in web_calls if item["action"]["type"] == "open_page"]
    assert trace.search_triggered is True and len(opens) == 2 and trace.opened_sources == opens  # 打开的网页照记
    assert len(trace.generated_queries) == len(searches) + len(x_calls)
    assert "What are people saying about Notion lately? Is it still worth using?" in trace.generated_queries
    assert not [source for source in trace.retrieved_sources if source.domain == "x.com"]  # 帖子不在候选里
    assert trace.retrieved_sources and all(source.url.startswith("http") for source in trace.retrieved_sources)
    assert run.answer is not None and run.answer.citations_available is True
    on_x = [citation for citation in run.answer.citations if citation.domain == "x.com"]
    assert [citation.url for citation in on_x] == ["https://x.com/ItzSuds/status/2064444421039587471"]
    assert all(citation.title is None for citation in run.answer.citations)


@respx.mock
def test_posts_fetched_are_billed_per_post_and_usage_matches_the_reported_cost():
    """每抓回一条帖子 $0.005:用量 × 牌价与平台自报费用一个 tick 不差(两份真实回包:29 条、5 条)。
    只搜网页的那台不认这一项——同一份回包交给它算,少的正好是帖子的钱。"""
    for name, model, posts in (("success_x_posts_cited.json", "grok-4.5", 29),
                               ("success_x_unanchored.json", "grok-4.7", 5)):
        respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=golden(name)))
        adapter = GrokWebXPostsSearchAdapter(api_key="test-key")
        run = adapter.ask(make_request(model=model))
        usage = adapter.usage_of(run.raw_payload)
        table = adapter.meta().price_table[model]
        assert set(usage) == set(table) and usage["x_posts"] == posts and table["x_posts"] == pytest.approx(0.005)
        assert run.cost_usd == pytest.approx(run.raw_payload["usage"]["cost_in_usd_ticks"] / 1e10)
        assert priced(table, usage) == pytest.approx(run.cost_usd, abs=1e-12)
        web_only = GrokWebSearchAdapter(api_key="test-key")
        short = priced(web_only.meta().price_table[model], web_only.usage_of(run.raw_payload))
        assert run.cost_usd - short == pytest.approx(posts * 0.005, abs=1e-12)


@respx.mock
def test_unanchored_lists_still_mean_citations_are_hidden():
    """grok-4.7 开着 X 也一样:正文不标出处、只把搜到的(含帖子)挂成零宽度清单 → 引用记「看不到」,帖子也不进候选。"""
    respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=golden("success_x_unanchored.json")))
    run = GrokWebXPostsSearchAdapter(api_key="test-key").ask(make_request(model="grok-4.7"))
    assert run.answer is not None and run.answer.citations_available is False and run.answer.citations == []
    assert "始祖鸟" in run.search_trace.generated_queries  # X 搜索词
    assert not [source for source in run.search_trace.retrieved_sources if source.domain == "x.com"]


@respx.mock
def test_a_failed_x_search_action_is_a_transient_error_with_its_cost():
    body = golden("success_x_posts_cited.json")
    next(item for item in body["output"] if item["type"] == "custom_tool_call")["status"] = "failed"
    respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(AdapterTransientError) as exc_info:
        GrokWebXPostsSearchAdapter(api_key="test-key").ask(make_request())
    assert "检索动作失败" in str(exc_info.value) and exc_info.value.cost_usd == pytest.approx(0.265964)


def test_meta_declares_it_as_a_tickable_version_of_the_web_only_instrument():
    """它是独立的仪器(自己的名字、牌价、估价、说明),同时声明自己是只搜网页那台的可勾选版本(确认页用)。"""
    meta = GrokWebXPostsSearchAdapter(api_key="test-key").meta()
    base = GrokWebSearchAdapter(api_key="test-key").meta()
    assert (meta.provider, meta.surface, meta.display_name, meta.api_name) == (
        "xai", "grok_web_xposts_search", "Grok + X", "Grok API + X")
    assert meta.variant_of == base.surface == "grok_web_search" and base.variant_of is None
    assert meta.variant_label is not None and "X" in meta.variant_label.zh and "X" in meta.variant_label.en
    assert meta.models == base.models and meta.helper_models == []
    assert (meta.default_model, meta.consumer_default_model, meta.main_model) == (
        base.default_model, base.consumer_default_model, base.main_model)
    assert meta.capabilities == base.capabilities
    assert set(meta.price_table) == set(meta.models)  # 裁判用的辅助型号不在这台上
    assert meta.est_cost_per_call_usd == {"grok-4.5": 0.23, "grok-4.7": 0.38}
    assert meta.est_cost_per_call_search_disabled_usd == base.est_cost_per_call_search_disabled_usd
    # 报告里的说明:开了 X(不是「不包含 X」那句);4.7 不标出处那句照旧只对 4.7
    on, only_47 = meta.method_notes
    assert "开了 X 帖子搜索" in on.zh and "turned on" in on.en and on.models == []
    assert only_47.models == ["grok-4.7"]
    assert "不包含 X" not in "".join(note.zh for note in meta.method_notes)
    assert "不包含 X" in base.method_notes[0].zh  # 只搜网页的那台照旧


def test_the_helper_model_is_not_on_this_instrument():
    with pytest.raises(AdapterPermanentError, match="未知模型"):
        GrokWebXPostsSearchAdapter(api_key="test-key").ask(make_request(model="grok-4.3",
                                                                   search_mode=SearchMode.SEARCH_DISABLED))
