"""Perplexity Agent API 适配器契约测试(12.1;S9.0 迁移自 Sonar chat/completions;2026-11-15 用户定改测 low 预设)。

防的是:平台改返回格式 → 翻译静默出错 → 污染入库数据。
golden 来自 11-15 low 预设冒烟的真实回包(真身裁剪:长提示词与摘录截短,编号 / 标记 / 用量 / 费用原样):
带引用(两轮搜索,编号接着编)/ 打开过网页 / 没搜(由真身去掉搜索项改造)/ status=failed / 限流错误。
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
from adapters.perplexity_agent import PRESET, PerplexityAgentAdapter
from engine.models import AskRequest, Outcome, SearchMode

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


def test_the_measured_object_is_the_pinned_low_preset():
    """用户 2026-11-15 定:测 Perplexity 自己维护的 low 预设、固定版本;sonar 删掉(旧回答留在库里)。"""
    meta = PerplexityAgentAdapter(api_key="test-key").meta()
    assert PRESET == "low@2026-11-11.0"
    assert meta.models == [PRESET] and meta.default_model == PRESET
    assert meta.consumer_default_model == PRESET and meta.main_model == PRESET
    assert "perplexity/sonar" not in meta.models


@respx.mock
def test_agent_success_with_citations_translates_field_by_field():
    body = golden("perplexity_agent", "success_with_citations.json")
    route = respx.post(AGENT_URL).mock(return_value=httpx.Response(200, json=body))
    run = PerplexityAgentAdapter(api_key="test-key").ask(make_request())

    assert (run.run_id, run.prompt_id, run.run_index) == ("run-1", "need-1", 1)
    assert (run.provider, run.surface, run.model) == ("perplexity", "perplexity_agent", PRESET)
    assert run.search_mode is SearchMode.DEFAULT and run.locale == "zh-CN" and run.region is None
    assert run.outcome is Outcome.SUCCESS and run.error is None
    assert run.answer.answer_text.startswith("如果没有更多背景")
    assert run.answer.citations_available is True
    # 正文标记 [web:25][web:25][web:26][web:27][web:2]:搜两轮时编号接着编(1–15、16–30),按编号合并指得对;同一来源只记一次
    assert [c.domain for c in run.answer.citations] == ["www.bigin.com", "play.google.com", "www.ssme.sh.gov.cn",
                                                        "www.hubspot.com"]
    assert [c.position for c in run.answer.citations] == [1, 2, 3, 4]
    assert run.answer.citations[0].title.startswith("Bigin Pricing")
    # 两轮搜索的检索词与候选全部照记,rank = 平台返回顺序
    assert run.search_trace.search_triggered is True
    assert len(run.search_trace.generated_queries) == 6
    assert run.search_trace.generated_queries[0].startswith("中小企业 CRM 推荐")
    assert len(run.search_trace.retrieved_sources) == 30
    assert [(s.domain, s.rank) for s in run.search_trace.retrieved_sources[:1]] == [("www.zoho.com.cn", 1)]
    assert run.search_trace.retrieved_sources[15].rank == 16
    assert run.search_trace.opened_sources == []  # 这次没打开网页:工具调用可见,空就是没开
    assert run.raw_payload == body
    assert run.cost_usd == pytest.approx(0.0039)  # 平台自报的总价
    assert run.latency_ms >= 0

    sent = json.loads(route.calls[0].request.content)
    # 不放 model / tools,都用预设自带的
    assert sent == {"effort": PRESET, "input": "中小团队用什么 CRM 好?", "max_output_tokens": 4096}
    assert route.calls[0].request.headers["authorization"] == "Bearer test-key"


@respx.mock
def test_agent_usage_splits_the_input_tiers_and_prices_to_the_platforms_total():
    """用量从原始存档读(输入三档、输出、搜索次数、打开网页的费用),给确认页按牌价估;记的费用仍是平台自报的总价。
    11-15 拿真实回包验算:用量 × 牌价与平台总价对得上。"""
    respx.post(AGENT_URL).mock(return_value=httpx.Response(200, json=golden("perplexity_agent",
                                                                            "success_with_citations.json")))
    adapter = PerplexityAgentAdapter(api_key="test-key")
    run = adapter.ask(make_request())
    usage = adapter.usage_of(run.raw_payload)
    assert usage == {"input_tokens": 9.0, "cache_write_tokens": 11441.0, "cached_input_tokens": 9920.0,
                     "output_tokens": 736.0, "searches": 2.0, "fetch_usd": 0.0}
    table = adapter.meta().price_table[PRESET]
    assert set(table) == set(usage)
    assert priced(table, usage) == pytest.approx(run.cost_usd, abs=5e-5)
    assert adapter.usage_of(None) == {}


@respx.mock
def test_agent_opened_pages_come_from_fetch_url_results():
    body = golden("perplexity_agent", "success_with_fetch.json")
    respx.post(AGENT_URL).mock(return_value=httpx.Response(200, json=body))
    adapter = PerplexityAgentAdapter(api_key="test-key")
    run = adapter.ask(make_request(prompt_text="2026 年性价比高的扫地机器人推荐哪些?"))

    assert run.search_trace.opened_sources == ["https://www.sina.cn/gc/article/niykvcr1640325.html"]
    assert [c.domain for c in run.answer.citations] == ["www.sina.cn"]
    usage = adapter.usage_of(run.raw_payload)
    assert usage["fetch_usd"] == pytest.approx(0.00213) and usage["searches"] == 1.0
    assert priced(adapter.meta().price_table[PRESET], usage) == pytest.approx(run.cost_usd, abs=5e-5)


@respx.mock
def test_agent_searched_without_markers_is_zero_citations_not_unobservable():
    """预设给正文标记,对应这条通道是通的:搜了却没标记,就是真的没引(0 条)。
    (原来 sonar 从不给对应,这种情况记「看不到」;11-15 换预设后改,方法版本 0.4.34。)"""
    body = golden("perplexity_agent", "success_with_citations.json")
    part = body["output"][2]["content"][0]
    part["text"] = part["text"].replace("[web:25]", "").replace("[web:26]", "").replace("[web:27]", "").replace(
        "[web:2]", "")
    respx.post(AGENT_URL).mock(return_value=httpx.Response(200, json=body))
    run = PerplexityAgentAdapter(api_key="test-key").ask(make_request())

    assert run.outcome is Outcome.SUCCESS
    assert run.answer.citations_available is True and run.answer.citations == []
    assert run.search_trace.search_triggered is True and len(run.search_trace.retrieved_sources) == 30


@respx.mock
def test_agent_markers_and_annotations_merge_by_text_position_and_dedupe_by_url():
    body = golden("perplexity_agent", "success_with_citations.json")
    part = body["output"][2]["content"][0]
    part["text"] = "甲[web:16] 乙[2] 丙[1] 丁[1]"  # 同一来源两种写法、重复标记只记一次;跨两轮的编号都认
    part["annotations"] = [{"type": "url_citation", "start_index": 0, "end_index": 1,
                            "url": "https://forum.example.net/t/crm", "title": "CRM 讨论串"}]
    respx.post(AGENT_URL).mock(return_value=httpx.Response(200, json=body))
    run = PerplexityAgentAdapter(api_key="test-key").ask(make_request())

    assert [c.domain for c in run.answer.citations] == ["forum.example.net", "www.zoho.com", "www.hubspot.com",
                                                        "www.zoho.com.cn"]
    assert [c.position for c in run.answer.citations] == [1, 2, 3, 4]
    assert run.answer.citations[0].title == "CRM 讨论串"  # annotations 自带标题


@respx.mock
def test_agent_no_search_is_recorded_as_not_triggered():
    body = golden("perplexity_agent", "success_no_search.json")
    respx.post(AGENT_URL).mock(return_value=httpx.Response(200, json=body))
    run = PerplexityAgentAdapter(api_key="test-key").ask(make_request(prompt_text="What are the best running shoes?",
                                                                      locale="en-US"))

    assert run.outcome is Outcome.SUCCESS and run.answer.citations == []
    assert run.answer.citations_available is True  # 没搜就没有来源可引:「没引」是真实观测
    assert run.search_trace.search_triggered is False and run.search_trace.retrieved_sources == []
    assert run.search_trace.generated_queries == [] and run.search_trace.opened_sources == []
    assert run.cost_usd == pytest.approx(0.00074)


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
def test_agent_reported_failure_carries_the_cost_the_platform_reported():
    """status=failed 时平台在 usage.cost 里报了多少就记多少(第二轮审核 26:原来失败行一律记 0 元)。"""
    body = golden("perplexity_agent", "failed_status.json")
    body["usage"]["cost"]["total_cost"] = 0.004
    respx.post(AGENT_URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(AdapterTransientError) as exc_info:  # A9 起瞬时,钱照记
        PerplexityAgentAdapter(api_key="test-key").ask(make_request())
    assert exc_info.value.cost_usd == pytest.approx(0.004)


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
    """删掉的 sonar 与没登记的预设版本一样:请求发不出去(再测碰到旧单由 engine/tiers.py 换成现在的型号)。"""
    for name in ("perplexity/sonar", "low@2026-10-16.0"):
        with pytest.raises(AdapterPermanentError, match="未知模型"):
            PerplexityAgentAdapter(api_key="test-key", client=httpx.Client()).ask(make_request(model=name))


@respx.mock
def test_agent_healthcheck_probes_the_pinned_preset_cheaply():
    body = golden("perplexity_agent", "success_no_search.json")
    route = respx.post(AGENT_URL).mock(return_value=httpx.Response(200, json=body))
    assert PerplexityAgentAdapter(api_key="test-key").healthcheck() is True
    sent = json.loads(route.calls[0].request.content)
    assert sent == {"effort": PRESET, "input": "ping", "max_output_tokens": 16, "max_steps": 1}  # 顺带确认版本还在
    respx.post(AGENT_URL).mock(return_value=httpx.Response(401, json={"error": "bad key"}))
    with pytest.raises(AdapterAccountError):  # 账户问题不吞:开跑前预检靠它停单并说清换 key / 充值(S11.1)
        PerplexityAgentAdapter(api_key="bad-key").healthcheck()
    respx.post(AGENT_URL).mock(return_value=httpx.Response(400, json={"error": "unknown effort version"}))
    with pytest.raises(AdapterPermanentError):  # 版本没了:预检报 PRECHECK_REJECTED,重试没用(第三轮 A23)
        PerplexityAgentAdapter(api_key="test-key").healthcheck()


def test_close_releases_http_client():
    client = httpx.Client()
    PerplexityAgentAdapter(api_key="test-key", client=client).close()
    assert client.is_closed


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
