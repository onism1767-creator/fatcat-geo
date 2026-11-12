"""Perplexity Agent API 适配器契约测试(12.1;S9.0 迁移自 Sonar chat/completions)。

防的是:平台改返回格式 → 翻译静默出错 → 污染入库数据。
golden payload 覆盖:正常带引用(annotations + 正文标记两条通道)/ 搜了但平台没给对应 / 没搜 / status=failed / 限流错误。
断言逐字段进行;全部 respx 模拟,零真实网络。
"""

import json
from pathlib import Path

import httpx
import pytest
import respx

from adapters.base import (
    AdapterAccountError,
    AdapterPermanentError,
    AdapterTransientError,
    CapabilityNotSupportedError,
    priced,
)
from adapters.perplexity_agent import PerplexityAgentAdapter
from engine.models import NOT_OBSERVABLE, AskRequest, Outcome, SearchMode

GOLDEN = Path(__file__).parent / "golden"
AGENT_URL = "https://api.perplexity.ai/v1/agent"


def golden(surface: str, name: str) -> dict:
    return json.loads((GOLDEN / surface / name).read_text(encoding="utf-8"))


def make_request(**overrides) -> AskRequest:
    fields = dict(
        run_id="run-1",
        prompt_id="need-1",
        run_index=1,
        prompt_text="中小团队用什么 CRM 好?",
        locale="zh-CN",
    )
    fields.update(overrides)
    return AskRequest(**fields)


@respx.mock
def test_agent_success_with_citations_translates_field_by_field():
    body = golden("perplexity_agent", "success_with_citations.json")
    route = respx.post(AGENT_URL).mock(return_value=httpx.Response(200, json=body))
    run = PerplexityAgentAdapter(api_key="test-key").ask(make_request())

    assert (run.run_id, run.prompt_id, run.run_index) == ("run-1", "need-1", 1)
    assert (run.provider, run.surface, run.model) == ("perplexity", "perplexity_agent", "perplexity/sonar")
    assert run.search_mode is SearchMode.DEFAULT and run.locale == "zh-CN" and run.region is None
    assert run.outcome is Outcome.SUCCESS and run.error is None
    assert run.answer.answer_text.startswith("对于中小团队")
    assert run.answer.citations_available is True
    # 两条通道合并:annotations 指到结果 2(正文前段)、正文标记 [1] 在后段 → 按正文位置排序;标记指到不存在的 id(9)忽略
    assert [c.domain for c in run.answer.citations] == ["blog.example.org", "www.example-review.com"]
    assert [c.position for c in run.answer.citations] == [1, 2]
    assert run.answer.citations[1].title == "Best CRM for Small Teams (2026)"
    # 检索候选与检索词在 Agent API 可观测(S9.0 起 supports_query_trace=True)
    assert run.search_trace.search_triggered is True
    assert run.search_trace.generated_queries == ["best CRM for small teams 2026"]
    assert [(s.domain, s.rank) for s in run.search_trace.retrieved_sources] == [
        ("www.example-review.com", 1), ("blog.example.org", 2), ("forum.example.net", 3),
    ]
    assert run.search_trace.opened_sources == NOT_OBSERVABLE
    assert run.raw_payload == body
    assert run.cost_usd == pytest.approx(0.03037)
    assert run.latency_ms >= 0

    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == "perplexity/sonar"  # 未指定 model 时用 meta 默认
    assert sent["input"] == "中小团队用什么 CRM 好?"
    assert sent["tools"] == [{"type": "web_search"}] and sent["max_output_tokens"] == 4096
    assert route.calls[0].request.headers["authorization"] == "Bearer test-key"


@respx.mock
def test_agent_usage_is_read_from_the_archive_while_the_cost_stays_the_platforms_own():
    """成本调研②:计费用量从原始存档读(token 数、web_search 次数)给确认页按牌价估;记的费用仍是平台自报的总价。"""
    respx.post(AGENT_URL).mock(return_value=httpx.Response(200, json=golden("perplexity_agent",
                                                                            "success_with_citations.json")))
    adapter = PerplexityAgentAdapter(api_key="test-key")
    run = adapter.ask(make_request())
    assert adapter.usage_of(run.raw_payload) == {"input_tokens": 4718.0, "output_tokens": 450.0, "searches": 1.0}
    assert run.cost_usd == pytest.approx(0.03037)  # 平台自报,不是用量 × 牌价
    assert set(adapter.meta().price_table) == set(adapter.meta().models)
    assert priced(adapter.meta().price_table[run.model], {"searches": 2.0}) == pytest.approx(0.005)
    assert adapter.usage_of(None) == {}


@respx.mock
def test_agent_searched_but_platform_linked_no_sources_is_not_observable():
    """搜了、annotations 空、正文没标记(2026-10-14 实测 sonar 的常态):平台没给回答与来源的对应,
    记「看不到」(citations_available=False),不得记成「引用 0 条」(铁律 7)。"""
    body = golden("perplexity_agent", "success_empty_citations.json")
    respx.post(AGENT_URL).mock(return_value=httpx.Response(200, json=body))
    run = PerplexityAgentAdapter(api_key="test-key").ask(make_request())

    assert run.outcome is Outcome.SUCCESS  # 回答有效,不是失败(6.3)
    assert run.answer.citations_available is False and run.answer.citations == []
    assert run.search_trace.search_triggered is True
    assert len(run.search_trace.retrieved_sources) == 2  # 候选照记


@respx.mock
def test_agent_markers_and_annotations_merge_by_text_position_and_dedupe_by_url():
    body = golden("perplexity_agent", "success_with_citations.json")
    part = body["output"][1]["content"][0]
    part["text"] = "甲[web:2] 乙[2] 丙[1] 丁[1]"  # 同一来源两种写法、重复标记只记一次
    part["annotations"] = [{"type": "url_citation", "start_index": 0, "end_index": 1,
                            "url": "https://forum.example.net/t/crm-for-small-teams", "title": "CRM 讨论串"}]
    respx.post(AGENT_URL).mock(return_value=httpx.Response(200, json=body))
    run = PerplexityAgentAdapter(api_key="test-key").ask(make_request())

    assert run.answer.citations_available is True
    assert [c.domain for c in run.answer.citations] == ["forum.example.net", "blog.example.org",
                                                        "www.example-review.com"]
    assert [c.position for c in run.answer.citations] == [1, 2, 3]
    assert run.answer.citations[0].title == "CRM 讨论串"  # annotations 自带标题


@respx.mock
def test_agent_no_search_is_recorded_as_not_triggered():
    body = golden("perplexity_agent", "success_no_search.json")
    respx.post(AGENT_URL).mock(return_value=httpx.Response(200, json=body))
    run = PerplexityAgentAdapter(api_key="test-key").ask(make_request())

    assert run.outcome is Outcome.SUCCESS and run.answer.citations == []
    assert run.answer.citations_available is True  # 没搜就没有来源可引:「没引」是真实观测
    assert run.search_trace.search_triggered is False and run.search_trace.retrieved_sources == []
    assert run.search_trace.generated_queries == []


@respx.mock
def test_agent_failed_status_raises_transient_with_platform_message():
    """平台回了话却报 failed(golden 里是「模型过载」):瞬时错误交调度器重试,与 OpenAI / Gemini 同口径
    (第三轮审核 A9;原来记永久失败不重试、还进熔断窗口)。"""
    body = golden("perplexity_agent", "failed_status.json")
    respx.post(AGENT_URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(AdapterTransientError, match="status='failed'") as exc_info:
        PerplexityAgentAdapter(api_key="test-key").ask(make_request())
    assert '"status": "failed"' in (exc_info.value.response_body or "")  # 失败的证据随失败行存档


@respx.mock
def test_agent_rate_limit_raises_transient_and_does_not_retry():
    body = golden("perplexity_agent", "error_rate_limited.json")
    route = respx.post(AGENT_URL).mock(return_value=httpx.Response(429, json=body))
    with pytest.raises(AdapterTransientError) as exc_info:
        PerplexityAgentAdapter(api_key="test-key").ask(make_request())
    assert exc_info.value.http_status == 429
    assert route.call_count == 1  # 适配器内禁止自行重试(13.1)


@respx.mock
def test_agent_timeout_raises_transient():
    respx.post(AGENT_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
    with pytest.raises(AdapterTransientError):
        PerplexityAgentAdapter(api_key="test-key").ask(make_request())


@respx.mock
def test_agent_auth_failure_raises_permanent():
    respx.post(AGENT_URL).mock(return_value=httpx.Response(401, json={"error": "bad key"}))
    with pytest.raises(AdapterPermanentError) as exc_info:
        PerplexityAgentAdapter(api_key="bad-key").ask(make_request())
    assert exc_info.value.http_status == 401


@respx.mock
def test_agent_malformed_body_raises_permanent():
    respx.post(AGENT_URL).mock(return_value=httpx.Response(200, json={"unexpected": "shape"}))
    with pytest.raises(AdapterPermanentError, match="响应结构"):
        PerplexityAgentAdapter(api_key="test-key").ask(make_request())


def test_agent_undeclared_capabilities_raise_explicitly():
    adapter = PerplexityAgentAdapter(api_key="test-key", client=httpx.Client())
    with pytest.raises(CapabilityNotSupportedError):
        adapter.ask(make_request(search_mode=SearchMode.SEARCH_DISABLED))
    with pytest.raises(CapabilityNotSupportedError):
        adapter.ask(make_request(search_mode=SearchMode.FORCED_SEARCH))
    with pytest.raises(CapabilityNotSupportedError):
        adapter.ask(make_request(region="US"))


def test_unknown_model_is_rejected_before_any_call():
    with pytest.raises(AdapterPermanentError, match="未知模型"):
        PerplexityAgentAdapter(api_key="test-key", client=httpx.Client()).ask(make_request(model="perplexity/nope"))


@respx.mock
def test_agent_healthcheck():
    body = golden("perplexity_agent", "success_no_search.json")
    respx.post(AGENT_URL).mock(return_value=httpx.Response(200, json=body))
    assert PerplexityAgentAdapter(api_key="test-key").healthcheck() is True
    respx.post(AGENT_URL).mock(return_value=httpx.Response(401, json={"error": "bad key"}))
    with pytest.raises(AdapterAccountError):  # 账户问题不吞:开跑前预检靠它停单并说清换 key / 充值(S11.1)
        PerplexityAgentAdapter(api_key="bad-key").healthcheck()


def test_close_releases_http_client():
    client = httpx.Client()
    PerplexityAgentAdapter(api_key="test-key", client=client).close()
    assert client.is_closed


@respx.mock
def test_agent_reported_failure_carries_the_cost_the_platform_reported():
    """status=failed 时平台在 usage.cost 里报了多少就记多少(第二轮审核 26:原来失败行一律记 0 元)。"""
    body = golden("perplexity_agent", "failed_status.json")
    body["usage"]["cost"]["total_cost"] = 0.004
    respx.post(AGENT_URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(AdapterTransientError) as exc_info:  # A9 起瞬时,钱照记
        PerplexityAgentAdapter(api_key="test-key").ask(make_request())
    assert exc_info.value.cost_usd == pytest.approx(0.004)


@respx.mock
def test_agent_odd_field_types_are_a_structure_error_with_the_response_kept():
    """这家原来没有结构防线:字段类型怪(位置不是数字、标题不是字符串)直接冒原始异常,响应也没存(第二轮审核 26)。"""
    for note in ({"type": "url_citation", "url": "https://a.test/x", "start_index": "abc"},
                 {"type": "url_citation", "url": "https://a.test/x", "title": {"not": "a string"}}):
        odd = {"status": "completed", "output": [
            {"type": "search_results", "queries": ["q"], "results": [{"id": 1, "url": "https://a.test/x"}]},
            {"type": "message", "content": [{"type": "output_text", "text": "答 [1]", "annotations": [note]}]},
        ], "usage": {"cost": {"total_cost": 0.003}}}
        respx.post(AGENT_URL).mock(return_value=httpx.Response(200, json=odd))
        with pytest.raises(AdapterPermanentError, match="响应结构") as exc_info:
            PerplexityAgentAdapter(api_key="test-key").ask(make_request())
        assert json.loads(exc_info.value.response_body or "") == odd and exc_info.value.cost_usd == pytest.approx(0.003)
    respx.post(AGENT_URL).mock(return_value=httpx.Response(200, json=["not", "an", "object"]))
    with pytest.raises(AdapterPermanentError) as top_info:
        PerplexityAgentAdapter(api_key="test-key").ask(make_request())
    assert "not" in (top_info.value.response_body or "")
