"""Gemini 接地通道适配器契约测试(12.1)。

防的是:平台改返回格式 → 翻译静默出错 → 污染入库数据。
golden payload 按 2026-09-08 真实探针裁剪:正常带引用 / 正常未检索(实测
模型可自主选择不搜)/ 检索结果错误 / 失败终态 / 限流错误。
断言逐字段进行;全部 respx 模拟,零真实网络。

本 surface 的两条关键翻译口径(适配器注释详述,此处测试把守):
- 引用 URL 为平台跳转链,真实域名取 title 字段;跳转链按来源稳定复用,
  按 url 去重成立(实测 75 锚注 → 16 唯一 URL);
- 候选集平台不暴露 → retrieved_sources 记 NOT_OBSERVABLE,禁止引用反推。
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
from adapters.gemini_grounding import GeminiGroundingAdapter
from engine.models import NOT_OBSERVABLE, AskRequest, Outcome, SearchMode

GOLDEN = Path(__file__).parent / "golden" / "gemini_grounding"
API_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"


def golden(name: str) -> dict:
    return json.loads((GOLDEN / name).read_text(encoding="utf-8"))


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


# ── 口径钉死 ──────────────────────────────────────────────────────────


@respx.mock
def test_request_calibers_are_pinned():
    """守住:GA 版 Interactions 端点、google_search 工具、store=false、无生成参数。"""
    route = respx.post(API_URL).mock(
        return_value=httpx.Response(200, json=golden("success_with_citations.json"))
    )
    GeminiGroundingAdapter(api_key="test-key").ask(make_request())
    sent = json.loads(route.calls[0].request.content)
    assert sent["tools"] == [{"type": "google_search"}]
    assert sent["store"] is False  # 测量调用不在平台侧留存
    assert "generation_config" not in sent  # 测平台默认态
    assert route.calls[0].request.headers["x-goog-api-key"] == "test-key"


# ── 正常带引用:逐字段翻译 ────────────────────────────────────────────


@respx.mock
def test_success_with_citations_translates_field_by_field():
    body = golden("success_with_citations.json")
    route = respx.post(API_URL).mock(return_value=httpx.Response(200, json=body))
    run = GeminiGroundingAdapter(api_key="test-key").ask(make_request())

    assert (run.run_id, run.information_need_id, run.prompt_variant_id) == ("run-1", "need-1", "variant-1")
    assert (run.provider, run.surface, run.model) == ("gemini", "gemini_grounding", "gemini-3.8-flash")
    assert run.outcome is Outcome.SUCCESS and run.error is None

    assert run.answer.answer_text == (
        "对于中小团队,HubSpot 与 Zoho CRM 是最常被推荐的选择,两者都提供免费档位。"
        "如果预算敏感,可优先试用免费档。"
    )
    # 引用池:3 个锚注、2 个唯一跳转链 → 按 url 去重;域名取 title(平台以域名作标题)
    assert run.answer.citations_available is True
    assert [c.domain for c in run.answer.citations] == ["example-review.com", "zoho.com.cn"]
    assert [c.position for c in run.answer.citations] == [1, 2]
    assert run.answer.citations[0].url.startswith(
        "https://vertexaisearch.cloud.google.com/grounding-api-redirect/"
    )  # 跳转链原样保留,核验器重抓可解析

    # 检索词可观测(一步多词);候选集平台不暴露,如实 NOT_OBSERVABLE
    assert run.search_trace.search_triggered is True
    assert run.search_trace.generated_queries == [
        "中小团队 CRM 推荐 2026", "best CRM small teams pricing 2026"
    ]
    assert run.search_trace.retrieved_sources == NOT_OBSERVABLE
    assert run.search_trace.opened_sources == NOT_OBSERVABLE

    assert run.raw_payload == body
    # 成本 = token 费(思考按输出价)+ 唯一非空查询数 × $0.014
    assert run.cost_usd == pytest.approx(
        197 / 1e6 * 0.75 + (2097 + 925) / 1e6 * 3.75 + 2 * 0.014
    )

    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == "gemini-3.8-flash"
    assert sent["input"] == "中小团队用什么 CRM 好?"


# ── 正常未检索(实测行为):引用空是真实负观测 ─────────────────────────


@respx.mock
def test_no_search_is_success_with_observable_negative_trigger():
    respx.post(API_URL).mock(return_value=httpx.Response(200, json=golden("success_no_search.json")))
    run = GeminiGroundingAdapter(api_key="test-key").ask(make_request())

    assert run.outcome is Outcome.SUCCESS
    assert run.search_trace.search_triggered is False  # 无检索步骤 = 确凿负观测
    assert run.search_trace.generated_queries == []
    assert run.answer.citations_available is True
    assert run.answer.citations == []
    assert run.cost_usd == pytest.approx(8 / 1e6 * 0.75 + (1351 + 956) / 1e6 * 3.75)


# ── search_disabled 模式 ─────────────────────────────────────────────


@respx.mock
def test_search_disabled_omits_tool_and_marks_citations_unavailable():
    route = respx.post(API_URL).mock(
        return_value=httpx.Response(200, json=golden("success_no_search.json"))
    )
    run = GeminiGroundingAdapter(api_key="test-key").ask(
        make_request(search_mode=SearchMode.SEARCH_DISABLED)
    )
    sent = json.loads(route.calls[0].request.content)
    assert "tools" not in sent
    assert run.search_mode is SearchMode.SEARCH_DISABLED
    assert run.answer.citations_available is False  # 结构性无引用,不是负观测


# ── 异常终态与错误翻译 ────────────────────────────────────────────────


@respx.mock
def test_failed_status_raises_transient():
    respx.post(API_URL).mock(return_value=httpx.Response(200, json=golden("failed_status.json")))
    with pytest.raises(AdapterTransientError, match="status="):
        GeminiGroundingAdapter(api_key="test-key").ask(make_request())


@respx.mock
def test_search_result_error_flag_raises_transient():
    respx.post(API_URL).mock(return_value=httpx.Response(200, json=golden("search_result_error.json")))
    with pytest.raises(AdapterTransientError, match="is_error"):
        GeminiGroundingAdapter(api_key="test-key").ask(make_request())


@respx.mock
def test_rate_limit_raises_transient_and_does_not_retry():
    body = golden("error_rate_limited.json")
    route = respx.post(API_URL).mock(return_value=httpx.Response(429, json=body))
    with pytest.raises(AdapterTransientError) as exc_info:
        GeminiGroundingAdapter(api_key="test-key").ask(make_request())
    assert exc_info.value.http_status == 429
    assert "RESOURCE_EXHAUSTED" in exc_info.value.response_body  # 错误证据随异常上交
    assert route.call_count == 1  # 适配器内禁止自行重试(13.1)


@respx.mock
def test_timeout_raises_transient():
    respx.post(API_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
    with pytest.raises(AdapterTransientError):
        GeminiGroundingAdapter(api_key="test-key").ask(make_request())


@respx.mock
def test_auth_failure_raises_permanent():
    body = {"error": {"code": 403, "message": "API key not valid.", "status": "PERMISSION_DENIED"}}
    respx.post(API_URL).mock(return_value=httpx.Response(403, json=body))
    with pytest.raises(AdapterPermanentError) as exc_info:
        GeminiGroundingAdapter(api_key="bad-key").ask(make_request())
    assert exc_info.value.http_status == 403


@respx.mock
def test_malformed_body_raises_permanent():
    respx.post(API_URL).mock(return_value=httpx.Response(200, json={"unexpected": "shape"}))
    with pytest.raises(AdapterPermanentError, match="响应结构"):
        GeminiGroundingAdapter(api_key="test-key").ask(make_request())


def test_undeclared_capabilities_and_unknown_model_raise_explicitly():
    adapter = GeminiGroundingAdapter(api_key="test-key", client=httpx.Client())
    with pytest.raises(CapabilityNotSupportedError):
        adapter.ask(make_request(search_mode=SearchMode.FORCED_SEARCH))
    with pytest.raises(CapabilityNotSupportedError):
        adapter.ask(make_request(region="US"))  # 接地工具无地区参数
    with pytest.raises(AdapterPermanentError, match="未知模型"):
        adapter.ask(make_request(model="gemini-1.5-pro"))


def test_close_releases_http_client():
    client = httpx.Client()
    GeminiGroundingAdapter(api_key="test-key", client=client).close()
    assert client.is_closed


# ── healthcheck ───────────────────────────────────────────────────────


@respx.mock
def test_healthcheck_minimal_request_without_tools():
    route = respx.post(API_URL).mock(
        return_value=httpx.Response(200, json=golden("success_no_search.json"))
    )
    assert GeminiGroundingAdapter(api_key="test-key").healthcheck() is True
    sent = json.loads(route.calls[0].request.content)
    assert "tools" not in sent and sent["store"] is False  # 验 key,零检索费

    bad = {"error": {"code": 403, "message": "API key not valid.", "status": "PERMISSION_DENIED"}}
    respx.post(API_URL).mock(return_value=httpx.Response(403, json=bad))
    assert GeminiGroundingAdapter(api_key="bad-key").healthcheck() is False
