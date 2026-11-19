"""Grok web_search 通道适配器契约测试(12.1)。

防的是:平台改返回格式 → 翻译静默出错 → 污染入库数据。
golden 都是 2026-11-17 / 18 的**真实回包**裁出来的(结构与字段名原样;每次搜索只留前 3 个候选 + 被引用到的,
正文只留开头一段,用量与费用原样;报错回包里的团队编号抹掉了):
- success_with_citations.json      grok-4.5:5 次搜索、两个 message 项(搜索中途先冒一段、搜完给正式回答)、锚注落在正文上
- success_unanchored_sources.json  grok-4.7:6 次搜索、正文没有任何引用标记,搜到的清单被整份挂成零宽度锚注
- success_no_search.json           grok-4.5 不带工具(闭卷)
- error_bad_key.http400.json / error_unknown_model.http404.json  真实报错
限流(429)没撞到过,测试里用的回包体是按这家的报错样子写的,不是真实回包。
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
from adapters.grok_web_search import HELPER_MODEL, GrokWebSearchAdapter
from engine.models import NOT_OBSERVABLE, AskRequest, Outcome, RunErrorType, SearchMode

GOLDEN = Path(__file__).parent / "golden" / "grok_web_search"
RESP_URL = "https://api.x.ai/v1/responses"
SUCCESSES = ("success_with_citations.json", "success_unanchored_sources.json", "success_no_search.json")


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


def _messages(body: dict) -> list[dict]:
    return [item for item in body["output"] if item["type"] == "message"]


# ── 口径钉死 ──────────────────────────────────────────────────────────


@respx.mock
def test_request_calibers_are_pinned():
    """若此测试失败,说明有人动了工具串 / 留存开关 / 长度保险丝,或者加了没核对过的参数——
    改动前必须重做真实对照并过评审。只搜网页:X 帖子搜索用户 2026-11-17 定砍掉。"""
    route = respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=golden("success_with_citations.json")))
    GrokWebSearchAdapter(api_key="test-key").ask(make_request())
    sent = json.loads(route.calls[0].request.content)
    assert sent == {
        "model": "grok-4.5",  # 未指定 model 时用 meta 默认
        "input": "中小团队用什么 CRM 好?",
        "max_output_tokens": 4096,
        "store": False,  # 测量调用不在平台侧留存
        "tools": [{"type": "web_search"}],  # 只放官方文档写了的参数
    }
    assert route.calls[0].request.headers["authorization"] == "Bearer test-key"


# ── 正常带引用:逐字段翻译 ────────────────────────────────────────────


@respx.mock
def test_success_with_citations_translates_field_by_field():
    body = golden("success_with_citations.json")
    respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=body))
    run = GrokWebSearchAdapter(api_key="test-key").ask(make_request())

    assert (run.run_id, run.prompt_id, run.run_index) == ("run-1", "need-1", 1)
    assert (run.provider, run.surface, run.model) == ("xai", "grok_web_search", "grok-4.5")
    assert run.search_mode is SearchMode.DEFAULT and run.locale == "zh-CN" and run.region is None
    assert run.outcome is Outcome.SUCCESS and run.error is None

    # 正文:两个 message 项按出现顺序全收,项与项之间空一行;正文里的 [[n]](网址) 原样保留
    first, second = (item["content"][0]["text"] for item in _messages(body))
    assert run.answer.answer_text == f"{first}\n\n{second}"
    assert run.answer.answer_text.startswith("[[1]](https://monday.com/")
    assert "**中小团队（大概 5–50 人）选 CRM" in run.answer.answer_text

    # 引用池:11 个落在正文上的锚注、7 个唯一网址 → 按首次出现去重;锚注的 title 是引用编号,不当网页标题
    assert run.answer.citations_available is True
    assert [c.domain for c in run.answer.citations] == [
        "monday.com", "vettlo.com", "worktile.com", "www.sohu.com", "weibanzhushou.com", "www.scrm.com",
        "claritysoft.com",
    ]
    assert [c.position for c in run.answer.citations] == [1, 2, 3, 4, 5, 6, 7]
    assert all(c.title is None for c in run.answer.citations)
    assert run.answer.citations[0].url == "https://monday.com/blog/zh_TW/crm-and-sales-zh/best_crm_for_small_business/"

    # 检索链路:检索词(一次动作一个)+ 有序候选;浏览了哪些页平台没暴露
    assert run.search_trace.search_triggered is True
    assert run.search_trace.generated_queries == [
        "中小团队 推荐 CRM 2024",
        "best CRM for small teams 2024",
        "small business CRM comparison HubSpot Salesforce Zoho Freshworks",
        "纷享销客 vs Zoho CRM vs HubSpot 中小企业",
        "HubSpot CRM free plan limits 2024 OR 2025 OR 2026",
    ]
    sources = run.search_trace.retrieved_sources
    expected_urls = [s["url"] for item in body["output"] if item["type"] == "web_search_call"
                     for s in item["action"]["sources"]]
    assert [s.url for s in sources] == expected_urls and len(sources) == 18
    assert [s.rank for s in sources] == list(range(1, 19))
    assert sources[0].domain == "monday.com" and sources[0].title is None
    assert run.search_trace.opened_sources == NOT_OBSERVABLE

    assert run.raw_payload == body
    assert run.cost_usd == pytest.approx(0.1200648)  # 平台自报 1,200,648,000 tick
    assert run.latency_ms >= 0


@respx.mock
@pytest.mark.parametrize("name, model", [("success_with_citations.json", "grok-4.5"),
                                         ("success_unanchored_sources.json", "grok-4.7"),
                                         ("success_no_search.json", "grok-4.5")])
def test_reported_cost_equals_usage_times_list_price(name, model):
    """费用记平台自报的;用量从原始存档读(补老数据走同一条路),用量 × 牌价与自报的数一个 tick 不差——
    三份真实回包都对得上,牌价表(确认页估价用)因此可信。"""
    respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=golden(name)))
    adapter = GrokWebSearchAdapter(api_key="test-key")
    run = adapter.ask(make_request(model=model))
    usage = adapter.usage_of(run.raw_payload)
    assert set(usage) == set(adapter.meta().price_table[model])  # 键一致
    assert run.cost_usd == pytest.approx(run.raw_payload["usage"]["cost_in_usd_ticks"] / 1e10)
    assert priced(adapter.meta().price_table[model], usage) == pytest.approx(run.cost_usd, abs=1e-12)
    assert adapter.usage_of(None) == {} and adapter.usage_of("junk") == {}


@respx.mock
def test_usage_splits_cached_input_and_cost_falls_back_to_list_price_when_not_reported():
    body = golden("success_with_citations.json")
    del body["usage"]["cost_in_usd_ticks"]  # 平台哪天不报了:按牌价折算,预算闸门不能失明
    respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=body))
    adapter = GrokWebSearchAdapter(api_key="test-key")
    run = adapter.ask(make_request())
    assert adapter.usage_of(run.raw_payload) == {
        "input_tokens": 35602.0, "cached_input_tokens": 27136.0, "output_tokens": 2620.0, "searches": 5.0,
    }  # 62,738 输入里 27,136 命中缓存;输出含推理
    assert run.cost_usd == pytest.approx((35602 * 2.0 + 27136 * 0.30 + 2620 * 6.0) / 1e6 + 5 * 0.005)


# ── 搜到 ≠ 引用 ───────────────────────────────────────────────────────


@respx.mock
def test_sources_relisted_without_a_place_in_the_text_are_not_citations():
    """grok-4.7 三次真实回答都这样:正文没有任何 [[n]] 标记,搜到的网页被整份挂成锚注、位置全是 0。
    那是「搜到的」又报了一遍(已在 retrieved_sources),不是「引用的」;哪些被引用了看不到——
    记 citations_available=False,不记「引用 0 条」(铁律 7;用户 2026-11-18 确认搜到与引用分两格)。"""
    body = golden("success_unanchored_sources.json")
    notes = _messages(body)[0]["content"][0]["annotations"]
    listed = {s["url"] for item in body["output"] if item["type"] == "web_search_call"
              for s in item["action"]["sources"]}
    assert len(notes) == 18 and all(n["start_index"] == n["end_index"] == 0 for n in notes)
    assert {n["url"] for n in notes} == listed  # 和候选一个不差
    respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=body))
    run = GrokWebSearchAdapter(api_key="test-key").ask(make_request(model="grok-4.7"))

    assert run.outcome is Outcome.SUCCESS and run.model == "grok-4.7"
    assert run.answer.citations == [] and run.answer.citations_available is False
    assert run.search_trace.search_triggered is True
    assert len(run.search_trace.generated_queries) == 6 and len(run.search_trace.retrieved_sources) == 18
    assert "[[" not in run.answer.answer_text


@respx.mock
def test_one_anchored_note_among_relisted_sources_keeps_citations_observable():
    body = golden("success_unanchored_sources.json")
    part = _messages(body)[0]["content"][0]
    part["annotations"][3].update(start_index=10, end_index=30)
    respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=body))
    run = GrokWebSearchAdapter(api_key="test-key").ask(make_request(model="grok-4.7"))
    assert run.answer.citations_available is True
    assert [c.url for c in run.answer.citations] == [part["annotations"][3]["url"]]


# ── 没搜 / 闭卷 ───────────────────────────────────────────────────────


@respx.mock
def test_no_search_is_success_with_observable_negative_trace():
    respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=golden("success_no_search.json")))
    run = GrokWebSearchAdapter(api_key="test-key").ask(make_request())

    assert run.outcome is Outcome.SUCCESS
    assert run.search_trace.search_triggered is False  # 显式工具调用:没搜是确凿负观测
    assert run.search_trace.generated_queries == [] and run.search_trace.retrieved_sources == []
    assert run.answer.citations_available is True and run.answer.citations == []  # 什么锚注都没有:真的没引
    assert run.cost_usd == pytest.approx(0.0077992)


@respx.mock
def test_search_disabled_omits_tool_and_marks_citations_unavailable():
    route = respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=golden("success_no_search.json")))
    run = GrokWebSearchAdapter(api_key="test-key").ask(make_request(search_mode=SearchMode.SEARCH_DISABLED))
    sent = json.loads(route.calls[0].request.content)
    assert "tools" not in sent
    assert run.search_mode is SearchMode.SEARCH_DISABLED
    assert run.search_trace.search_triggered is False
    assert run.answer.citations_available is False  # 结构性无引用,不是负观测


def test_undeclared_capabilities_raise_instead_of_degrading():
    """地区、强制检索:官方文档没写对应参数,不声明;请求了就报错(硬边界 5),不发请求。"""
    adapter = GrokWebSearchAdapter(api_key="test-key", client=httpx.Client())
    with pytest.raises(CapabilityNotSupportedError):
        adapter.ask(make_request(search_mode=SearchMode.FORCED_SEARCH))
    with pytest.raises(CapabilityNotSupportedError):
        adapter.ask(make_request(region="US"))
    caps = adapter.meta().capabilities
    assert caps.supports_query_trace and caps.supports_citations and caps.supports_search_disabled
    assert not caps.supports_region and not caps.supports_forced_search and not caps.citation_urls_redirect


# ── 异常终态 ─────────────────────────────────────────────────────────


@respx.mock
def test_incomplete_status_is_success_with_truncated_answer():
    """max_output_tokens 截断:截断的回答也是回答(SUCCESS),细节在 raw_payload。"""
    body = golden("success_no_search.json")
    body["status"] = "incomplete"
    body["incomplete_details"] = {"reason": "max_output_tokens"}
    respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=body))
    run = GrokWebSearchAdapter(api_key="test-key").ask(make_request())
    assert run.outcome is Outcome.SUCCESS and run.answer.answer_text.startswith("中小团队选 CRM")
    assert run.raw_payload["incomplete_details"] == {"reason": "max_output_tokens"}


@respx.mock
def test_failed_status_raises_transient_and_carries_the_money_already_spent():
    """平台回了话、钱照计,却报失败:花费与整份响应随异常上交,失败行不记 0 元。"""
    body = golden("success_with_citations.json")
    body["status"] = "failed"
    body["error"] = {"message": "upstream error"}
    respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(AdapterTransientError, match="status='failed'") as exc_info:
        GrokWebSearchAdapter(api_key="test-key").ask(make_request())
    assert exc_info.value.cost_usd == pytest.approx(0.1200648)
    assert json.loads(exc_info.value.response_body or "") == body  # 失败的证据随失败行存档


@respx.mock
def test_failed_search_action_raises_transient():
    """检索动作本身失败 = 本次采样被检索基础设施污染,交调度器重采(与另外几家同口径)。"""
    body = golden("success_with_citations.json")
    call = next(item for item in body["output"] if item["type"] == "web_search_call")
    call["status"] = "failed"
    respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(AdapterTransientError, match="检索动作失败") as exc_info:
        GrokWebSearchAdapter(api_key="test-key").ask(make_request())
    assert exc_info.value.cost_usd > 0 and '"status": "failed"' in (exc_info.value.response_body or "")


@respx.mock
def test_an_action_type_never_seen_is_left_in_the_archive_not_guessed():
    body = golden("success_with_citations.json")
    call = next(item for item in body["output"] if item["type"] == "web_search_call")
    call["action"] = {"type": "something_new", "url": "https://example.test/page"}
    respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=body))
    run = GrokWebSearchAdapter(api_key="test-key").ask(make_request())
    assert len(run.search_trace.generated_queries) == 4  # 少了被改掉的那一次,其余照记
    assert run.search_trace.opened_sources == NOT_OBSERVABLE and run.raw_payload == body


# ── 错误翻译与能力边界 ────────────────────────────────────────────────


@respx.mock
def test_rate_limit_raises_transient_and_does_not_retry():
    body = {"code": "resource-exhausted", "error": "Too many requests"}  # 不是真实回包(没撞到过),只验状态码怎么翻译
    route = respx.post(RESP_URL).mock(return_value=httpx.Response(429, json=body, headers={"retry-after": "7"}))
    with pytest.raises(AdapterTransientError) as exc_info:
        GrokWebSearchAdapter(api_key="test-key").ask(make_request())
    assert exc_info.value.http_status == 429 and exc_info.value.retry_after_s == 7.0
    assert "Too many requests" in (exc_info.value.response_body or "")  # 错误证据随异常上交存档
    assert route.call_count == 1  # 适配器内禁止自行重试(13.1)


@respx.mock
def test_timeout_raises_transient():
    respx.post(RESP_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
    with pytest.raises(AdapterTransientError):
        GrokWebSearchAdapter(api_key="test-key").ask(make_request())


@respx.mock
def test_a_wrong_key_comes_back_as_http_400_and_is_still_a_key_problem():
    """真实回包(2026-11-18):这家 key 写错回 HTTP 400 code=invalid-argument,不是 401。靠平台原话认成账户问题,
    开跑前预检才会说「换 key」而不是「要改适配器」。"""
    respx.post(RESP_URL).mock(return_value=httpx.Response(400, json=golden("error_bad_key.http400.json")))
    adapter = GrokWebSearchAdapter(api_key="bad-key")
    with pytest.raises(AdapterAccountError) as exc_info:
        adapter.ask(make_request())
    assert exc_info.value.error_type is RunErrorType.ACCOUNT_AUTH and exc_info.value.http_status == 400
    with pytest.raises(AdapterAccountError):  # 预检同样不吞
        adapter.healthcheck()


@respx.mock
def test_healthcheck_minimal_request_without_tools():
    route = respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=golden("success_no_search.json")))
    assert GrokWebSearchAdapter(api_key="test-key").healthcheck() is True
    sent = json.loads(route.calls[0].request.content)
    assert sent == {"model": "grok-4.5", "input": "ping", "max_output_tokens": 16, "store": False}

    respx.post(RESP_URL).mock(return_value=httpx.Response(404, json=golden("error_unknown_model.http404.json")))
    with pytest.raises(AdapterPermanentError) as exc_info:  # 型号下线:重试没用,不吞(第三轮审核 A23)
        GrokWebSearchAdapter(api_key="test-key").healthcheck()
    assert not isinstance(exc_info.value, AdapterAccountError)

    respx.post(RESP_URL).mock(return_value=httpx.Response(503, text="unavailable"))
    assert GrokWebSearchAdapter(api_key="test-key").healthcheck() is False


@respx.mock
def test_malformed_bodies_raise_permanent_with_the_evidence():
    adapter = GrokWebSearchAdapter(api_key="test-key")
    respx.post(RESP_URL).mock(return_value=httpx.Response(200, json={"unexpected": "shape"}))
    with pytest.raises(AdapterPermanentError, match="响应结构") as top_info:
        adapter.ask(make_request())
    assert "unexpected" in (top_info.value.response_body or "")

    odd = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text"}]}],
           "usage": {"input_tokens": 1000}}
    respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=odd))
    with pytest.raises(AdapterPermanentError, match="响应结构") as odd_info:
        adapter.ask(make_request())
    assert odd_info.value.cost_usd > 0 and json.loads(odd_info.value.response_body or "") == odd


@respx.mock
def test_one_malformed_citation_url_keeps_the_answer():
    body = golden("success_with_citations.json")
    note = _messages(body)[0]["content"][0]["annotations"][0]
    for same in _messages(body)[0]["content"][0]["annotations"][:2]:
        same["url"] = "http://[bad/x"
    respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=body))
    run = GrokWebSearchAdapter(api_key="test-key").ask(make_request())
    assert run.outcome is Outcome.SUCCESS and note["url"] == "http://[bad/x"
    assert run.answer.citations[0].url == "http://[bad/x" and run.answer.citations[0].domain == ""


# ── 型号清单 ─────────────────────────────────────────────────────────


@respx.mock
def test_model_list_flags_and_the_helper_that_only_answers_closed_book():
    """用户 2026-11-17 定:被测 = grok-4.5(应用里用的那款)+ grok-4.7(最新一代);辅助型号只做不联网小调用、不被测。"""
    adapter = GrokWebSearchAdapter(api_key="test-key")
    meta = adapter.meta()
    assert (meta.provider, meta.surface, meta.display_name, meta.api_name) == (
        "xai", "grok_web_search", "Grok", "Grok API")
    assert meta.models == ["grok-4.5", "grok-4.7"] and meta.helper_models == [HELPER_MODEL] == ["grok-4.3"]
    assert (meta.default_model, meta.consumer_default_model, meta.main_model) == ("grok-4.5", "grok-4.5", "grok-4.7")
    assert "用户 11-18 确认" in meta.models_checked and meta.made_by == {}  # 应用默认是 4.5,用户看过后确认
    assert set(meta.price_table) == {"grok-4.5", "grok-4.7", "grok-4.3"}
    assert meta.price_table["grok-4.5"]["cached_input_tokens"] == pytest.approx(0.30e-6)
    assert meta.price_table["grok-4.7"]["cached_input_tokens"] == pytest.approx(0.50e-6)
    assert meta.price_table["grok-4.3"] == pytest.approx(
        {"input_tokens": 1.25e-6, "cached_input_tokens": 0.20e-6, "output_tokens": 2.50e-6, "searches": 0.005})
    assert set(meta.est_cost_per_call_usd) == set(meta.est_cost_per_call_search_disabled_usd) == set(meta.models)
    # 联网问一次的估价 = 实测均值(样本少);4.7 有一次过了 20 万 token 整单双倍价,所以不写「上界」
    assert meta.est_cost_per_call_usd == {"grok-4.5": 0.18, "grok-4.7": 0.32}
    # 报告「这次是怎么测的」里的一句(用户 2026-11-18 定的原话):接口这边没开 X 帖子搜索
    whole, only_47 = meta.method_notes
    assert (whole.zh, whole.en, whole.models) == (
        "本次 Grok 的结果不包含 X 内容。", "Grok's results in this run do not include content from X.", [])
    # 单个型号的状况(用户 2026-11-18):只在测了 grok-4.7 的单上写——它通常不标出处,引用看不到
    assert only_47.models == ["grok-4.7"]
    assert only_47.zh == "Grok 4.7 的回答通常不标出处,它引用了哪些网页看不到(不是 0);它搜到了哪些网页照常统计。"
    assert only_47.en.startswith("Grok 4.7 usually does not mark its sources in the answer")

    route = respx.post(RESP_URL).mock(return_value=httpx.Response(200, json=golden("success_no_search.json")))
    run = adapter.ask(make_request(model=HELPER_MODEL, search_mode=SearchMode.SEARCH_DISABLED))
    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == "grok-4.3" and "tools" not in sent and run.model == "grok-4.3"
    with pytest.raises(CapabilityNotSupportedError, match="不联网"):
        adapter.ask(make_request(model=HELPER_MODEL))  # 辅助型号不被测:联网问直接报错
    for unlisted in ("grok-4.6", "grok-4.20-0309-reasoning", "grok-3"):
        with pytest.raises(AdapterPermanentError, match="未知模型"):
            adapter.ask(make_request(model=unlisted))


def test_close_releases_http_client():
    client = httpx.Client()
    GrokWebSearchAdapter(api_key="test-key", client=client).close()
    assert client.is_closed
