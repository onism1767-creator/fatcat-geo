"""Claude web_search 通道适配器契约测试(12.1)。

防的是:平台改返回格式 → 翻译静默出错 → 污染入库数据。
golden payload 覆盖:正常带引用 / 正常未检索(引用空)/ 显式拒答 / 限流错误,
外加本 surface 特有的:检索工具错误块、max_uses 有界检索、pause_turn 续传。
断言逐字段进行;全部 respx 模拟,零真实网络。

版本钉死是第一道防线(附录 v0.4.2 条目 2):对照实验证明新版
web_search_20260209 会吞内联引用(12 vs 0),专设测试守住版本号。
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
)
from adapters.claude_web_search import ClaudeWebSearchAdapter
from engine.models import NOT_OBSERVABLE, AskRequest, Outcome, RunErrorType, SearchMode

GOLDEN = Path(__file__).parent / "golden" / "claude_web_search"
MSG_URL = "https://api.anthropic.com/v1/messages"


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


# ── 版本钉死 ──────────────────────────────────────────────────────────


@respx.mock
def test_tool_and_api_versions_are_pinned():
    """若此测试失败,说明有人动了工具/接口版本号——升级前必须重做引用对照实验
    (新版动态过滤实测内联引用恒为 0,会让 CITATION 指标静默失效)。"""
    route = respx.post(MSG_URL).mock(
        return_value=httpx.Response(200, json=golden("success_with_citations.json"))
    )
    ClaudeWebSearchAdapter(api_key="test-key").ask(make_request())
    sent = json.loads(route.calls[0].request.content)
    assert sent["tools"] == [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
    assert route.calls[0].request.headers["anthropic-version"] == "2023-06-01"
    assert route.calls[0].request.headers["x-api-key"] == "test-key"


# ── 正常带引用:逐字段翻译 ────────────────────────────────────────────


@respx.mock
def test_success_with_citations_translates_field_by_field():
    body = golden("success_with_citations.json")
    route = respx.post(MSG_URL).mock(return_value=httpx.Response(200, json=body))
    run = ClaudeWebSearchAdapter(api_key="test-key").ask(make_request())

    assert (run.run_id, run.prompt_id, run.run_index) == ("run-1", "need-1", 1)
    assert (run.provider, run.surface, run.model) == ("anthropic", "claude_web_search", "claude-sonnet-5-5")
    assert run.search_mode is SearchMode.DEFAULT and run.locale == "zh-CN" and run.region is None
    assert run.outcome is Outcome.SUCCESS and run.error is None

    # 回答文本 = 各 text 块原样拼接(块以引用锚点为界切分)
    assert run.answer.answer_text == (
        "对于中小团队,HubSpot 与 Zoho CRM 是最常被推荐的选择,两者都提供免费档位。"
        "如果预算敏感,可优先试用免费档。"
    )
    # 引用池:3 个内联锚点、2 个唯一 URL → 按首次出现去重
    assert run.answer.citations_available is True
    assert [c.domain for c in run.answer.citations] == ["www.example-review.com", "blog.example.org"]
    assert [c.position for c in run.answer.citations] == [1, 2]
    assert run.answer.citations[0].title == "Best CRM for Small Teams (2026)"

    # 检索链路真实可观测:检索词 + 有序候选集
    assert run.search_trace.search_triggered is True
    assert run.search_trace.generated_queries == ["best CRM small teams 2026"]
    sources = run.search_trace.retrieved_sources
    assert [s.rank for s in sources] == [1, 2, 3]
    assert sources[0].domain == "www.example-review.com"
    assert sources[2].url == "https://www.example-pricing.net/crm"
    assert run.search_trace.opened_sources == NOT_OBSERVABLE

    assert run.raw_payload == {"responses": [body]}
    # 成本 = 官方价目确定性折算:token 价 + 检索次数价
    assert run.cost_usd == pytest.approx(8123 / 1e6 * 2.0 + 542 / 1e6 * 10.0 + 1 * 0.01)
    assert run.latency_ms >= 0

    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == "claude-sonnet-5-5"  # 未指定 model 时用 meta 默认
    assert sent["max_tokens"] == 4096
    assert sent["messages"] == [{"role": "user", "content": "中小团队用什么 CRM 好?"}]


# ── 正常未检索:引用空是真实负观测 ────────────────────────────────────


@respx.mock
def test_no_search_is_success_with_observable_negative_trace():
    body = golden("success_no_search.json")
    respx.post(MSG_URL).mock(return_value=httpx.Response(200, json=body))
    run = ClaudeWebSearchAdapter(api_key="test-key").ask(make_request())

    assert run.outcome is Outcome.SUCCESS
    # 检索是显式工具调用:「模型没搜」真实可观测,不是 UNKNOWN
    assert run.search_trace.search_triggered is False
    assert run.search_trace.generated_queries == []
    assert run.search_trace.retrieved_sources == []
    # 引用机制在位但无引用 → 真实负观测(区别于 search_disabled 的结构性缺席)
    assert run.answer.citations_available is True
    assert run.answer.citations == []
    assert run.cost_usd == pytest.approx(412 / 1e6 * 2.0 + 96 / 1e6 * 10.0)  # usage 无 server_tool_use 也可折算


# ── search_disabled 模式 ─────────────────────────────────────────────


@respx.mock
def test_search_disabled_omits_tool_and_marks_citations_unavailable():
    body = golden("success_no_search.json")
    route = respx.post(MSG_URL).mock(return_value=httpx.Response(200, json=body))
    run = ClaudeWebSearchAdapter(api_key="test-key").ask(
        make_request(search_mode=SearchMode.SEARCH_DISABLED)
    )

    sent = json.loads(route.calls[0].request.content)
    assert "tools" not in sent  # 不带工具调用 = 测参数化知识
    assert run.search_mode is SearchMode.SEARCH_DISABLED
    assert run.search_trace.search_triggered is False
    # 无检索则无引用机制:空引用是结构性缺席,不是「没引用你」的负观测
    assert run.answer.citations_available is False


def test_search_disabled_with_region_raises_explicitly():
    adapter = ClaudeWebSearchAdapter(api_key="test-key", client=httpx.Client())
    with pytest.raises(CapabilityNotSupportedError, match="search_disabled"):
        adapter.ask(make_request(search_mode=SearchMode.SEARCH_DISABLED, region="US"))


# ── 地区钉定 ──────────────────────────────────────────────────────────


@respx.mock
def test_region_translates_to_user_location():
    route = respx.post(MSG_URL).mock(
        return_value=httpx.Response(200, json=golden("success_with_citations.json"))
    )
    run = ClaudeWebSearchAdapter(api_key="test-key").ask(make_request(region="US"))
    sent = json.loads(route.calls[0].request.content)
    assert sent["tools"][0]["user_location"] == {"type": "approximate", "country": "US"}
    assert run.region == "US"


# ── 显式拒答 ──────────────────────────────────────────────────────────


@respx.mock
def test_refusal_stop_reason_translates_to_refused():
    body = golden("refused_stop_reason.json")
    respx.post(MSG_URL).mock(return_value=httpx.Response(200, json=body))
    run = ClaudeWebSearchAdapter(api_key="test-key").ask(make_request())

    assert run.outcome is Outcome.REFUSED
    assert run.error.error_type is RunErrorType.REFUSAL
    assert "refusal" in run.error.message
    assert run.answer is None  # content 为空
    assert run.raw_payload == {"responses": [body]}  # 拒答同样全量存档


# ── 检索工具错误块(HTTP 200)──────────────────────────────────────────


@respx.mock
def test_search_tool_error_block_raises_transient():
    """检索基础设施故障污染本次采样 → 交引擎重采,不当作「没搜到」入库。"""
    respx.post(MSG_URL).mock(return_value=httpx.Response(200, json=golden("tool_error_unavailable.json")))
    with pytest.raises(AdapterTransientError, match="unavailable") as exc_info:
        ClaudeWebSearchAdapter(api_key="test-key").ask(make_request())
    assert "unavailable" in (exc_info.value.response_body or "")  # 失败的证据随失败行存档


@respx.mock
def test_max_uses_exceeded_is_bounded_search_success():
    """max_uses 拦下第 N+1 次检索是配额内的正常行为:已检索结果与回答有效。"""
    body = golden("success_max_uses_exceeded.json")
    respx.post(MSG_URL).mock(return_value=httpx.Response(200, json=body))
    run = ClaudeWebSearchAdapter(api_key="test-key").ask(make_request())

    assert run.outcome is Outcome.SUCCESS
    assert run.search_trace.generated_queries == ["中小团队 CRM 对比 2026", "CRM 免费档 十人以下团队"]
    assert [s.rank for s in run.search_trace.retrieved_sources] == [1]
    assert [c.position for c in run.answer.citations] == [1]


# ── pause_turn 协议续传 ───────────────────────────────────────────────


@respx.mock
def test_pause_turn_continues_and_aggregates_across_segments():
    first = golden("pause_turn_first.json")
    final = golden("success_with_citations.json")
    route = respx.post(MSG_URL).mock(
        side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=final)]
    )
    run = ClaudeWebSearchAdapter(api_key="test-key").ask(make_request())

    assert route.call_count == 2
    # 续传请求:原始提问 + 迄今全部 content 块作为同一 assistant 轮回传,工具不变
    sent = json.loads(route.calls[1].request.content)
    assert sent["messages"][0] == {"role": "user", "content": "中小团队用什么 CRM 好?"}
    assert sent["messages"][1]["role"] == "assistant"
    assert sent["messages"][1]["content"] == first["content"]
    assert sent["tools"][0]["type"] == "web_search_20250305"

    # 两段聚合:检索词、候选位次连续编号、用量与检索次数合并计价
    assert run.outcome is Outcome.SUCCESS
    assert run.search_trace.generated_queries == ["CRM 中小团队 免费 对比", "best CRM small teams 2026"]
    assert [s.rank for s in run.search_trace.retrieved_sources] == [1, 2, 3, 4]
    assert run.search_trace.retrieved_sources[0].domain == "www.example-forum.com"
    assert run.raw_payload == {"responses": [first, final]}
    assert run.cost_usd == pytest.approx(
        (6100 + 8123) / 1e6 * 2.0 + (210 + 542) / 1e6 * 10.0 + 2 * 0.01
    )


@respx.mock
def test_pause_turn_exhaustion_raises_transient_after_bounded_continuations():
    respx.post(MSG_URL).mock(return_value=httpx.Response(200, json=golden("pause_turn_first.json")))
    with pytest.raises(AdapterTransientError, match="pause_turn") as exc_info:
        ClaudeWebSearchAdapter(api_key="test-key").ask(make_request())
    # 1 次首发 + 3 次续传后放弃:有界完成,不无限深搜
    assert respx.calls.call_count == 4
    assert "pause_turn" in (exc_info.value.response_body or "")  # 已拿到的段随失败行存档


# ── 错误翻译与能力边界 ────────────────────────────────────────────────


@respx.mock
def test_rate_limit_raises_transient_and_does_not_retry():
    body = golden("error_rate_limited.json")
    route = respx.post(MSG_URL).mock(return_value=httpx.Response(429, json=body))
    with pytest.raises(AdapterTransientError) as exc_info:
        ClaudeWebSearchAdapter(api_key="test-key").ask(make_request())
    assert exc_info.value.http_status == 429
    assert "rate_limit_error" in exc_info.value.response_body  # 错误证据随异常上交存档
    assert route.call_count == 1  # 适配器内禁止自行重试(13.1)


@respx.mock
def test_timeout_raises_transient():
    respx.post(MSG_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
    with pytest.raises(AdapterTransientError):
        ClaudeWebSearchAdapter(api_key="test-key").ask(make_request())


@respx.mock
def test_auth_failure_raises_permanent():
    body = {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}
    respx.post(MSG_URL).mock(return_value=httpx.Response(401, json=body))
    with pytest.raises(AdapterPermanentError) as exc_info:
        ClaudeWebSearchAdapter(api_key="bad-key").ask(make_request())
    assert exc_info.value.http_status == 401


@respx.mock
def test_malformed_body_raises_permanent():
    respx.post(MSG_URL).mock(return_value=httpx.Response(200, json={"unexpected": "shape"}))
    with pytest.raises(AdapterPermanentError, match="响应结构"):
        ClaudeWebSearchAdapter(api_key="test-key").ask(make_request())


def test_undeclared_capabilities_and_unknown_model_raise_explicitly():
    adapter = ClaudeWebSearchAdapter(api_key="test-key", client=httpx.Client())
    with pytest.raises(CapabilityNotSupportedError):
        adapter.ask(make_request(search_mode=SearchMode.FORCED_SEARCH))
    with pytest.raises(AdapterPermanentError, match="未知模型"):
        adapter.ask(make_request(model="claude-2"))


# ── healthcheck ───────────────────────────────────────────────────────


@respx.mock
def test_healthcheck_minimal_request_without_tools():
    route = respx.post(MSG_URL).mock(return_value=httpx.Response(200, json=golden("success_no_search.json")))
    assert ClaudeWebSearchAdapter(api_key="test-key").healthcheck() is True
    sent = json.loads(route.calls[0].request.content)
    assert "tools" not in sent and sent["max_tokens"] == 1  # 验 key,成本近零

    bad = {"type": "error", "error": {"type": "authentication_error", "message": "bad"}}
    respx.post(MSG_URL).mock(return_value=httpx.Response(401, json=bad))
    with pytest.raises(AdapterAccountError):  # 账户问题不吞:开跑前预检靠它停单并说清换 key / 充值(S11.1)
        ClaudeWebSearchAdapter(api_key="bad-key").healthcheck()


def test_close_releases_http_client():
    client = httpx.Client()
    ClaudeWebSearchAdapter(api_key="test-key", client=client).close()
    assert client.is_closed
