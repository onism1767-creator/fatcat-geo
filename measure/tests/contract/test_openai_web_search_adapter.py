"""OpenAI web_search 通道适配器契约测试(12.1)。

防的是:平台改返回格式 → 翻译静默出错 → 污染入库数据。
golden payload 覆盖:正常带引用 / 正常未检索 / 显式拒答 / 限流错误,
外加本 surface 特有的:截断完成(status=incomplete)、失败状态(status=failed)。
断言逐字段进行;全部 respx 模拟,零真实网络。

口径钉死是第一道防线:OpenAI 工具串无日期版本号(GA 版 "web_search"),
专设测试守住工具串 + search_context_size + include + store 的完整请求形状。
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
from adapters.openai_web_search import OpenAIWebSearchAdapter
from engine.models import AskRequest, Outcome, RunErrorType, SearchMode

GOLDEN = Path(__file__).parent / "golden" / "openai_web_search"
RESP_URL = "https://api.openai.com/v1/responses"


def golden(name: str) -> dict:
    return json.loads((GOLDEN / name).read_text(encoding="utf-8"))


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


# ── 口径钉死 ──────────────────────────────────────────────────────────


@respx.mock
def test_request_calibers_are_pinned():
    """若此测试失败,说明有人动了工具串/上下文档位/观测窗/留存开关——
    改动前必须重做真实 API 对照并过评审(golden 是版本漂移的第一道防线)。"""
    route = respx.post(RESP_URL).mock(
        return_value=httpx.Response(200, json=golden("success_with_citations.json"))
    )
    OpenAIWebSearchAdapter(api_key="test-key").ask(make_request())
    sent = json.loads(route.calls[0].request.content)
    assert sent["tools"] == [{"type": "web_search", "search_context_size": "medium"}]
    assert sent["include"] == ["web_search_call.action.sources"]  # 候选观测窗常开
    assert sent["store"] is False  # 测量调用不在平台侧留存
    assert "tool_choice" not in sent  # DEFAULT 模式不强制
    assert route.calls[0].request.headers["authorization"] == "Bearer test-key"


# ── 正常带引用:逐字段翻译 ────────────────────────────────────────────


@respx.mock
def test_success_with_citations_translates_field_by_field():
    body = golden("success_with_citations.json")
    route = respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=body))
    run = OpenAIWebSearchAdapter(api_key="test-key").ask(make_request())

    assert (run.run_id, run.prompt_id, run.run_index) == ("run-1", "need-1", 1)
    assert (run.provider, run.surface, run.model) == ("openai", "openai_web_search", "gpt-5.6-luna")
    assert run.search_mode is SearchMode.DEFAULT and run.locale == "zh-CN" and run.region is None
    assert run.outcome is Outcome.SUCCESS and run.error is None

    assert run.answer.answer_text == (
        "对于中小团队,HubSpot 与 Zoho CRM 是最常被推荐的选择,两者都提供免费档位。"
        "如果预算敏感,可优先试用免费档。"
    )
    # 引用池:3 个锚注、2 个唯一 URL → 按首次出现去重
    assert run.answer.citations_available is True
    assert [c.domain for c in run.answer.citations] == ["www.example-review.com", "blog.example.org"]
    assert [c.position for c in run.answer.citations] == [1, 2]
    assert run.answer.citations[0].title == "Best CRM for Small Teams (2026)"

    # 检索链路全可观测:检索词(复数字段)+ 有序候选 + 翻页动作
    assert run.search_trace.search_triggered is True
    assert run.search_trace.generated_queries == ["中小团队 CRM 推荐 2026", "best CRM small teams"]
    sources = run.search_trace.retrieved_sources
    assert [s.rank for s in sources] == [1, 2, 3]
    assert sources[0].domain == "www.example-review.com" and sources[0].title is None
    assert run.search_trace.opened_sources == [
        "https://www.example-review.com/best-crm-small-teams"
    ]  # open_page 动作:全链首个可观测的翻页行为

    assert run.raw_payload == body
    # 成本 = token 费(检索内容已计入 input_tokens)+ search 动作数 × $0.01
    # (open_page 不计费,官方口径)
    # 默认型号 gpt-5.6-luna 的价目(S9.0)
    assert run.cost_usd == pytest.approx(9412 / 1e6 * 0.2 + 486 / 1e6 * 1.2 + 1 * 0.01)
    assert run.latency_ms >= 0

    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == "gpt-5.6-luna"  # 未指定 model 时用 meta 默认(ChatGPT 免费版默认,用户定)
    assert sent["max_output_tokens"] == 4096
    assert sent["input"] == "中小团队用什么 CRM 好?"


@respx.mock
def test_usage_is_read_from_the_archive_and_prices_to_the_recorded_cost():
    """成本调研②:计费用量从原始存档读(补老数据走同一条路),用量 × 牌价 = 记的费用;读不出就空,不臆造。"""
    respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=golden("success_with_citations.json")))
    adapter = OpenAIWebSearchAdapter(api_key="test-key")
    run = adapter.ask(make_request())
    usage = adapter.usage_of(run.raw_payload)
    assert usage == {"input_tokens": 9412.0, "cached_input_tokens": 0.0, "cache_write_tokens": 0.0,
                     "output_tokens": 486.0, "searches": 1.0}  # 缓存分档(Q8);golden 里没命中缓存
    assert priced(adapter.meta().price_table[run.model], usage) == pytest.approx(run.cost_usd)
    assert adapter.usage_of(None) == {} and adapter.usage_of("junk") == {}


# ── 正常未检索:引用空是真实负观测 ────────────────────────────────────


@respx.mock
def test_no_search_is_success_with_observable_negative_trace():
    respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=golden("success_no_search.json")))
    run = OpenAIWebSearchAdapter(api_key="test-key").ask(make_request())

    assert run.outcome is Outcome.SUCCESS
    assert run.search_trace.search_triggered is False  # 显式工具调用:没搜是确凿负观测
    assert run.search_trace.generated_queries == []
    assert run.search_trace.retrieved_sources == []
    assert run.search_trace.opened_sources == []
    assert run.answer.citations_available is True
    assert run.answer.citations == []
    assert run.cost_usd == pytest.approx(388 / 1e6 * 0.2 + 92 / 1e6 * 1.2)  # 无检索费;gpt-5.6-luna 价目


# ── 检索模式 ─────────────────────────────────────────────────────────


@respx.mock
def test_search_disabled_omits_tool_and_marks_citations_unavailable():
    route = respx.post(RESP_URL).mock(
        return_value=httpx.Response(200, json=golden("success_no_search.json"))
    )
    run = OpenAIWebSearchAdapter(api_key="test-key").ask(
        make_request(search_mode=SearchMode.SEARCH_DISABLED)
    )
    sent = json.loads(route.calls[0].request.content)
    assert "tools" not in sent and "include" not in sent
    assert run.search_mode is SearchMode.SEARCH_DISABLED
    assert run.search_trace.search_triggered is False
    assert run.answer.citations_available is False  # 结构性无引用,不是负观测


def test_search_disabled_with_region_raises_explicitly():
    adapter = OpenAIWebSearchAdapter(api_key="test-key", client=httpx.Client())
    with pytest.raises(CapabilityNotSupportedError, match="search_disabled"):
        adapter.ask(make_request(search_mode=SearchMode.SEARCH_DISABLED, region="US"))


@respx.mock
def test_forced_search_sends_required_tool_choice():
    """全链首个支持 FORCED_SEARCH 的 surface:单工具下 tool_choice=required 即强制检索。"""
    route = respx.post(RESP_URL).mock(
        return_value=httpx.Response(200, json=golden("success_with_citations.json"))
    )
    run = OpenAIWebSearchAdapter(api_key="test-key").ask(
        make_request(search_mode=SearchMode.FORCED_SEARCH)
    )
    sent = json.loads(route.calls[0].request.content)
    assert sent["tool_choice"] == "required"
    assert run.search_mode is SearchMode.FORCED_SEARCH  # 模式即口径,原样入档


@respx.mock
def test_region_translates_to_user_location():
    route = respx.post(RESP_URL).mock(
        return_value=httpx.Response(200, json=golden("success_with_citations.json"))
    )
    run = OpenAIWebSearchAdapter(api_key="test-key").ask(make_request(region="US"))
    sent = json.loads(route.calls[0].request.content)
    assert sent["tools"][0]["user_location"] == {"type": "approximate", "country": "US"}
    assert run.region == "US"


# ── 显式拒答与异常终态 ────────────────────────────────────────────────


@respx.mock
def test_refusal_content_part_translates_to_refused():
    body = golden("refused_output.json")
    respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=body))
    run = OpenAIWebSearchAdapter(api_key="test-key").ask(make_request())

    assert run.outcome is Outcome.REFUSED
    assert run.error.error_type is RunErrorType.REFUSAL
    assert "refusal" in run.error.message
    assert run.answer is None  # 无 output_text
    assert run.raw_payload == body  # 拒答同样全量存档


@respx.mock
def test_incomplete_status_is_success_with_truncated_answer():
    """max_output_tokens 截断:截断的回答也是回答(SUCCESS),细节在 raw_payload。"""
    body = golden("success_incomplete_max_tokens.json")
    respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=body))
    run = OpenAIWebSearchAdapter(api_key="test-key").ask(make_request())

    assert run.outcome is Outcome.SUCCESS
    assert run.answer.answer_text.startswith("对于中小团队")
    assert run.search_trace.generated_queries == ["中小团队 CRM 对比"]  # 单数 query 字段兼容
    assert run.raw_payload["incomplete_details"] == {"reason": "max_output_tokens"}


@respx.mock
def test_failed_status_raises_transient():
    respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=golden("failed_status.json")))
    with pytest.raises(AdapterTransientError, match="status=failed") as exc_info:
        OpenAIWebSearchAdapter(api_key="test-key").ask(make_request())
    assert '"status": "failed"' in (exc_info.value.response_body or "")  # 失败的证据随失败行存档


@respx.mock
def test_failed_search_action_raises_transient():
    """检索动作本身失败(web_search_call.status=failed):原来记成「搜了、没候选、回答有效」还计检索费(第三轮审核 A10),
    现在与 Claude 工具错误块、Gemini is_error 同口径——本次采样被污染,交调度器重采。"""
    body = golden("success_with_citations.json")
    assert body["output"][1]["type"] == "web_search_call"
    body["output"][1]["status"] = "failed"
    respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(AdapterTransientError, match="检索动作失败") as exc_info:
        OpenAIWebSearchAdapter(api_key="test-key").ask(make_request())
    assert '"status": "failed"' in (exc_info.value.response_body or "")  # 失败的证据随失败行存档
    assert exc_info.value.cost_usd > 0  # 平台回了话、钱照计:花费随失败行落



@respx.mock
def test_cached_input_tokens_are_priced_at_their_own_rates():
    """输入 token 分三档记账(Q8,2026-11-03 对账单核过):没命中缓存的全价、命中缓存的 0.1 倍、写入缓存的 1.25 倍,
    三者都含在平台报的 input_tokens 里;原来一律按全价,最贵的型号一天记高 19%。"""
    body = golden("success_with_citations.json")
    body["usage"] = {"input_tokens": 10000, "input_tokens_details": {"cached_tokens": 4000, "cache_write_tokens": 500},
                     "output_tokens": 400, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 10400}
    respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=body))
    adapter = OpenAIWebSearchAdapter(api_key="test-key")
    run = adapter.ask(make_request())  # 默认型号 gpt-5.6-luna:$0.2 / $1.2 每百万;golden 里 1 次检索
    assert adapter.usage_of(run.raw_payload) == {
        "input_tokens": 5500.0, "cached_input_tokens": 4000.0, "cache_write_tokens": 500.0,
        "output_tokens": 400.0, "searches": 1.0,
    }
    expected = (5500 * 0.2 + 4000 * 0.2 * 0.1 + 500 * 0.2 * 1.25 + 400 * 1.2) / 1e6 + 0.01
    assert run.cost_usd == pytest.approx(expected)
    assert run.cost_usd < (10000 * 0.2 + 400 * 1.2) / 1e6 + 0.01  # 比原来按全价记的少
    assert set(adapter.meta().price_table["gpt-5.6-luna"]) == set(adapter.usage_of(run.raw_payload))  # 键一致



# ── 错误翻译与能力边界 ────────────────────────────────────────────────


@respx.mock
def test_rate_limit_raises_transient_and_does_not_retry():
    body = golden("error_rate_limited.json")
    route = respx.post(RESP_URL).mock(return_value=httpx.Response(429, json=body))
    with pytest.raises(AdapterTransientError) as exc_info:
        OpenAIWebSearchAdapter(api_key="test-key").ask(make_request())
    assert exc_info.value.http_status == 429
    assert "rate_limit" in exc_info.value.response_body  # 错误证据随异常上交存档
    assert route.call_count == 1  # 适配器内禁止自行重试(13.1)


@respx.mock
def test_timeout_raises_transient():
    respx.post(RESP_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
    with pytest.raises(AdapterTransientError):
        OpenAIWebSearchAdapter(api_key="test-key").ask(make_request())


@respx.mock
def test_auth_failure_raises_permanent():
    body = {"error": {"message": "Incorrect API key provided", "type": "invalid_request_error",
                      "param": None, "code": "invalid_api_key"}}
    respx.post(RESP_URL).mock(return_value=httpx.Response(401, json=body))
    with pytest.raises(AdapterPermanentError) as exc_info:
        OpenAIWebSearchAdapter(api_key="bad-key").ask(make_request())
    assert exc_info.value.http_status == 401


@respx.mock
def test_malformed_body_raises_permanent():
    respx.post(RESP_URL).mock(return_value=httpx.Response(200, json={"unexpected": "shape"}))
    with pytest.raises(AdapterPermanentError, match="响应结构"):
        OpenAIWebSearchAdapter(api_key="test-key").ask(make_request())


def test_unknown_model_raises_explicitly():
    adapter = OpenAIWebSearchAdapter(api_key="test-key", client=httpx.Client())
    with pytest.raises(AdapterPermanentError, match="未知模型"):
        adapter.ask(make_request(model="gpt-3.5-turbo"))


@respx.mock
def test_model_list_is_the_current_generation_plus_the_free_default_with_per_model_cache_prices():
    """用户 2026-11-17 定「按清单改」(工程原则 9):清单 = 现役一代 + 免费版默认;6.2 两款冒烟通过后入,上一代三款删掉。
    缓存命中的价钱逐型号照官方价目页写——6.1 / 6.2 代是输入价的 0.05 倍,其余 0.1 倍(原来一律按 0.1 倍,6.1 两款记高)。"""
    adapter = OpenAIWebSearchAdapter(api_key="test-key")
    meta = adapter.meta()
    assert meta.models == ["gpt-5.6-luna", "gpt-6-luna", "gpt-6.1-terra", "gpt-6.2-sol", "gpt-6.2-astra"]
    assert meta.consumer_default_model == "gpt-5.6-luna" and meta.main_model == "gpt-6-luna"
    ratios = {m: round(p["cached_input_tokens"] / p["input_tokens"], 3) for m, p in meta.price_table.items()}
    assert ratios == {"gpt-5.6-luna": 0.1, "gpt-6-luna": 0.1, "gpt-6.1-terra": 0.05, "gpt-6.2-sol": 0.05,
                      "gpt-6.2-astra": 0.05}
    assert all(p["cache_write_tokens"] == pytest.approx(1.25 * p["input_tokens"]) for p in meta.price_table.values())
    assert meta.price_table["gpt-6.2-astra"]["input_tokens"] == pytest.approx(8e-6)
    assert meta.price_table["gpt-6.2-astra"]["output_tokens"] == pytest.approx(40e-6)

    body = golden("success_with_citations.json")
    body["usage"] = {"input_tokens": 10000, "input_tokens_details": {"cached_tokens": 4000, "cache_write_tokens": 500},
                     "output_tokens": 400, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 10400}
    respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=body))
    run = adapter.ask(make_request(model="gpt-6.2-sol"))  # $2 / $10 每百万,缓存命中 $0.10;golden 里 1 次检索
    assert run.model == "gpt-6.2-sol"
    assert run.cost_usd == pytest.approx((5500 * 2.0 + 4000 * 0.10 + 500 * 2.5 + 400 * 10.0) / 1e6 + 0.01)

    # 删掉的型号请求发不出去;再测碰到旧单由档位逻辑换成现型号
    for retired in ("gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol"):
        with pytest.raises(AdapterPermanentError, match="未知模型"):
            adapter.ask(make_request(model=retired))


def test_close_releases_http_client():
    client = httpx.Client()
    OpenAIWebSearchAdapter(api_key="test-key", client=client).close()
    assert client.is_closed


# ── healthcheck ───────────────────────────────────────────────────────


@respx.mock
def test_healthcheck_minimal_request_without_tools():
    route = respx.post(RESP_URL).mock(
        return_value=httpx.Response(200, json=golden("success_no_search.json"))
    )
    assert OpenAIWebSearchAdapter(api_key="test-key").healthcheck() is True
    sent = json.loads(route.calls[0].request.content)
    assert "tools" not in sent and sent["max_output_tokens"] == 16  # 验 key,成本近零

    bad = {"error": {"message": "bad key", "type": "invalid_request_error",
                     "param": None, "code": "invalid_api_key"}}
    respx.post(RESP_URL).mock(return_value=httpx.Response(401, json=bad))
    with pytest.raises(AdapterAccountError):  # 账户问题不吞:开跑前预检靠它停单并说清换 key / 充值(S11.1)
        OpenAIWebSearchAdapter(api_key="bad-key").healthcheck()

    gone = {"error": {"message": "The model `gpt-5.6-luna` does not exist", "type": "invalid_request_error",
                      "param": "model", "code": "model_not_found"}}
    respx.post(RESP_URL).mock(return_value=httpx.Response(404, json=gone))
    with pytest.raises(AdapterPermanentError):  # 4xx 永久错误也不吞:型号下线 / 接口地址变了,重试没用(第三轮 A23)
        OpenAIWebSearchAdapter(api_key="test-key").healthcheck()


@respx.mock
def test_reported_failure_and_odd_structure_carry_the_money_already_spent():
    """平台回了话、token 照计,却报 status=failed:花费与整份响应随异常上交,失败行不再记 0 元(第二轮审核 26)。"""
    from adapters.openai_web_search import _cost_usd

    body = golden("failed_status.json")
    respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=body))
    adapter = OpenAIWebSearchAdapter(api_key="test-key")
    with pytest.raises(AdapterTransientError) as exc_info:
        adapter.ask(make_request())
    assert exc_info.value.cost_usd == pytest.approx(_cost_usd(body, adapter.meta().default_model))
    assert exc_info.value.cost_usd > 0 and json.loads(exc_info.value.response_body or "") == body

    odd = {"output": [{"type": "message", "content": [{"type": "output_text"}]}], "usage": {"input_tokens": 1000}}
    respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=odd))
    with pytest.raises(AdapterPermanentError, match="响应结构") as odd_info:
        adapter.ask(make_request())
    assert odd_info.value.cost_usd > 0 and json.loads(odd_info.value.response_body or "") == odd
    respx.post(RESP_URL).mock(return_value=httpx.Response(200, json={"unexpected": "shape"}))
    with pytest.raises(AdapterPermanentError) as top_info:  # 顶层就不对:也带响应(原来不带)
        adapter.ask(make_request())
    assert "unexpected" in (top_info.value.response_body or "")


@respx.mock
def test_one_malformed_citation_url_keeps_the_answer():
    """一条坏引用网址(方括号不配对):原来取主机名抛错,整条好回答记成永久失败;现在回答照收,那条引用主机名留空
    (第二轮审核 B4)。"""
    body = golden("success_with_citations.json")
    message = next(item for item in body["output"] if item.get("type") == "message")
    note = next(n for part in message["content"] for n in part.get("annotations", []) if n["type"] == "url_citation")
    note["url"] = "http://[bad/x"
    respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=body))
    run = OpenAIWebSearchAdapter(api_key="test-key").ask(make_request())
    assert run.outcome is Outcome.SUCCESS and run.answer is not None
    bad = next(c for c in run.answer.citations if c.url == "http://[bad/x")
    assert bad.domain == ""
