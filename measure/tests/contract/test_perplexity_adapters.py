"""Perplexity 双通道适配器契约测试(12.1)。

防的是:平台改返回格式 → 翻译静默出错 → 污染入库数据。
golden payload 覆盖四类样本:正常带引用 / 正常引用空 / 显式拒答 / 限流错误
(裸检索通道无拒答概念,以空结果样本替代,共同凑齐四类)。
断言逐字段进行,含 outcome 与 NOT_OBSERVABLE 标记;全部 respx 模拟,零真实网络。
"""

import json
from pathlib import Path

import httpx
import pytest
import respx

from adapters.base import (
    AdapterPermanentError,
    AdapterTransientError,
    CapabilityNotSupportedError,
)
from adapters.perplexity_agent import PerplexityAgentAdapter
from adapters.perplexity_search import PerplexitySearchAdapter
from engine.models import NOT_OBSERVABLE, UNKNOWN, AskRequest, Outcome, RunErrorType, SearchMode

GOLDEN = Path(__file__).parent / "golden"
CHAT_URL = "https://api.perplexity.ai/chat/completions"
SEARCH_URL = "https://api.perplexity.ai/search"


def golden(surface: str, name: str) -> dict:
    return json.loads((GOLDEN / surface / name).read_text(encoding="utf-8"))


def make_request(**overrides) -> AskRequest:
    fields = dict(
        run_id="run-1",
        information_need_id="need-1",
        prompt_variant_id="variant-1",
        prompt_text="中小团队用什么 CRM 好?",
        locale="zh-CN",
    )
    fields.update(overrides)
    return AskRequest(**fields)


# ── 生成+引用通道 ─────────────────────────────────────────────────────


@respx.mock
def test_agent_success_with_citations_translates_field_by_field():
    body = golden("perplexity_agent", "success_with_citations.json")
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=body))
    run = PerplexityAgentAdapter(api_key="test-key").ask(make_request())

    assert (run.run_id, run.information_need_id, run.prompt_variant_id) == ("run-1", "need-1", "variant-1")
    assert (run.provider, run.surface, run.model) == ("perplexity", "perplexity_agent", "sonar")
    assert run.search_mode is SearchMode.DEFAULT and run.locale == "zh-CN" and run.region is None
    assert run.outcome is Outcome.SUCCESS and run.error is None
    assert run.answer.answer_text.startswith("对于中小团队")
    assert run.answer.citations_available is True
    assert [c.domain for c in run.answer.citations] == ["www.example-review.com", "blog.example.org"]
    assert [c.position for c in run.answer.citations] == [1, 2]
    assert run.answer.citations[0].title == "Best CRM for Small Teams (2026)"
    # 检索候选在本通道不可观测,禁止用引用集反推
    assert run.search_trace.retrieved_sources == NOT_OBSERVABLE
    assert run.search_trace.generated_queries == NOT_OBSERVABLE
    assert run.search_trace.opened_sources == NOT_OBSERVABLE
    assert run.search_trace.search_triggered is True
    assert run.raw_payload == body
    assert run.cost_usd == pytest.approx(0.0051)
    assert run.latency_ms >= 0

    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == "sonar"  # 未指定 model 时用 meta 默认
    assert sent["messages"] == [{"role": "user", "content": "中小团队用什么 CRM 好?"}]
    assert route.calls[0].request.headers["authorization"] == "Bearer test-key"


@respx.mock
def test_agent_empty_citation_set_is_success_negative_observation():
    body = golden("perplexity_agent", "success_empty_citations.json")
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=body))
    run = PerplexityAgentAdapter(api_key="test-key").ask(make_request())

    assert run.outcome is Outcome.SUCCESS  # 空引用集是真实负观测,不是失败(6.3)
    assert run.answer.citations_available is True
    assert run.answer.citations == []
    assert run.search_trace.search_triggered == UNKNOWN  # 搜了没结果 vs 没搜,不可区分


@respx.mock
def test_agent_content_filter_translates_to_refused():
    body = golden("perplexity_agent", "refused_content_filter.json")
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=body))
    run = PerplexityAgentAdapter(api_key="test-key").ask(make_request())

    assert run.outcome is Outcome.REFUSED
    assert run.error.error_type is RunErrorType.REFUSAL
    assert "content_filter" in run.error.message
    assert run.answer is None  # content 为空
    assert run.raw_payload == body  # 拒答同样全量存档


@respx.mock
def test_agent_rate_limit_raises_transient_and_does_not_retry():
    body = golden("perplexity_agent", "error_rate_limited.json")
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(429, json=body))
    with pytest.raises(AdapterTransientError) as exc_info:
        PerplexityAgentAdapter(api_key="test-key").ask(make_request())
    assert exc_info.value.http_status == 429
    assert route.call_count == 1  # 适配器内禁止自行重试(13.1)


@respx.mock
def test_agent_timeout_raises_transient():
    respx.post(CHAT_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
    with pytest.raises(AdapterTransientError):
        PerplexityAgentAdapter(api_key="test-key").ask(make_request())


@respx.mock
def test_agent_auth_failure_raises_permanent():
    respx.post(CHAT_URL).mock(return_value=httpx.Response(401, json={"error": "bad key"}))
    with pytest.raises(AdapterPermanentError) as exc_info:
        PerplexityAgentAdapter(api_key="bad-key").ask(make_request())
    assert exc_info.value.http_status == 401


@respx.mock
def test_agent_malformed_body_raises_permanent():
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json={"unexpected": "shape"}))
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


@respx.mock
def test_agent_healthcheck():
    body = golden("perplexity_agent", "success_empty_citations.json")
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=body))
    assert PerplexityAgentAdapter(api_key="test-key").healthcheck() is True
    respx.post(CHAT_URL).mock(return_value=httpx.Response(401, json={"error": "bad key"}))
    assert PerplexityAgentAdapter(api_key="bad-key").healthcheck() is False


# ── 裸检索通道 ────────────────────────────────────────────────────────


@respx.mock
def test_search_success_translates_ranked_results():
    body = golden("perplexity_search", "success_ranked_results.json")
    route = respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json=body))
    run = PerplexitySearchAdapter(api_key="test-key").ask(make_request())

    assert (run.provider, run.surface, run.model) == ("perplexity", "perplexity_search", "unversioned")
    assert run.outcome is Outcome.SUCCESS and run.error is None
    assert run.answer is None  # 该通道无生成回答
    sources = run.search_trace.retrieved_sources
    assert [s.rank for s in sources] == [1, 2, 3]
    assert sources[0].domain == "www.example-review.com"
    assert sources[2].url == "https://www.example-pricing.net/crm"
    assert run.search_trace.search_triggered is True
    assert run.search_trace.generated_queries == NOT_OBSERVABLE
    assert run.raw_payload == body
    assert run.cost_usd == pytest.approx(0.005)

    sent = json.loads(route.calls[0].request.content)
    assert sent == {"query": "中小团队用什么 CRM 好?", "max_results": 20}


@respx.mock
def test_search_empty_results_is_success_negative_observation():
    body = golden("perplexity_search", "success_empty_results.json")
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json=body))
    run = PerplexitySearchAdapter(api_key="test-key").ask(make_request())
    assert run.outcome is Outcome.SUCCESS
    assert run.search_trace.retrieved_sources == []


@respx.mock
def test_search_rate_limit_raises_transient_and_does_not_retry():
    body = golden("perplexity_search", "error_rate_limited.json")
    route = respx.post(SEARCH_URL).mock(return_value=httpx.Response(429, json=body))
    with pytest.raises(AdapterTransientError) as exc_info:
        PerplexitySearchAdapter(api_key="test-key").ask(make_request())
    assert exc_info.value.http_status == 429
    assert route.call_count == 1


@respx.mock
def test_search_server_error_raises_transient():
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(503, text="unavailable"))
    with pytest.raises(AdapterTransientError):
        PerplexitySearchAdapter(api_key="test-key").ask(make_request())


def test_search_undeclared_capabilities_raise_explicitly():
    adapter = PerplexitySearchAdapter(api_key="test-key", client=httpx.Client())
    with pytest.raises(CapabilityNotSupportedError):
        adapter.ask(make_request(search_mode=SearchMode.SEARCH_DISABLED))
    with pytest.raises(AdapterPermanentError):
        adapter.ask(make_request(model="sonar"))  # 该 surface 无模型可选


def test_close_releases_http_client():
    for adapter_cls in (PerplexitySearchAdapter, PerplexityAgentAdapter):
        client = httpx.Client()
        adapter_cls(api_key="test-key", client=client).close()
        assert client.is_closed


@respx.mock
def test_search_healthcheck():
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json={"id": "s", "results": []}))
    assert PerplexitySearchAdapter(api_key="test-key").healthcheck() is True
