"""调模型:请求长什么样、回答怎么翻译、怎么算钱、出错怎么办、缓存。全部不联网(假的 HTTP 传输)。"""

from __future__ import annotations

import json

import httpx
import pytest

from butter.config import load_pricing
from butter.llm import choose_provider, complete, cost_usd, extract_json, find_price, parse_pricing, upper_bound_usd
from butter.llm import wire
from butter.llm.keys import available_providers, read_env_file, secret_for
from butter.llm.pricing import PricingError, rates

PRICING = load_pricing()
SECRET = "test-secret-value-123"
ENV = {"ANTHROPIC_API_KEY": SECRET, "OPENAI_API_KEY": SECRET, "GEMINI_API_KEY": SECRET}
ANTHROPIC_OK = {"content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": '{"title": "Classic Kettle"}'}],
                "stop_reason": "end_turn", "usage": {"input_tokens": 3000, "output_tokens": 1500}}
OPENAI_OK = {"status": "completed", "usage": {"input_tokens": 3000, "output_tokens": 1500},
             "output": [{"type": "reasoning"}, {"type": "message", "content": [
                 {"type": "output_text", "text": 'Here you go:\n```json\n{"title": "Classic Kettle"}\n```'}]}]}
GEMINI_OK = {"status": "completed",
             "usage": {"total_input_tokens": 3000, "total_output_tokens": 1000, "total_thought_tokens": 500},
             "steps": [{"type": "thought"}, {"type": "model_output", "content": [
                 {"type": "text", "text": '{"title": "Classic Kettle"}'}]}]}


def transport(*responses):
    """按顺序回放的假 HTTP。每个元素是 (状态码, JSON) 或一个异常。记下收到的请求。"""
    queue, seen = list(responses), []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        status, payload = item
        return httpx.Response(status, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def call(provider, model, client, **kw):
    kw.setdefault("env", ENV)
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("today", "2026-11-17")
    return complete(provider, model, "Write a title.", pricing=PRICING, prompt_version="page_v1", client=client, **kw)


# ---- 价目 ----

def test_pricing_table_is_complete_and_sourced():
    assert PRICING.provider_order == ["anthropic", "openai", "gemini"]
    assert PRICING.default_models["anthropic"] == "claude-opus-5-5"
    for m in PRICING.models:
        assert m.input_per_mtok > 0 and m.output_per_mtok > m.input_per_mtok
        assert m.source.strip() and m.checked_on.startswith("2026-"), m.model
    for provider, model in PRICING.default_models.items():
        assert find_price(PRICING, provider, model) is not None


def test_cost_follows_usage_times_price_and_promo_expiry():
    opus = find_price(PRICING, "anthropic", "claude-opus-5-5")
    assert cost_usd(opus, 3000, 1500, "2026-11-17") == pytest.approx(0.042)   # 3000×$4 + 1500×$20,每百万
    argon = find_price(PRICING, "gemini", "gemini-4-argon")
    assert rates(argon, "2026-11-30") == (2.0, 10.0)
    assert rates(argon, "2026-12-01") == (4.0, 20.0)                         # 优惠价到期后用新价
    assert cost_usd(argon, 1_000_000, 0, "2026-12-01") == pytest.approx(4.0)
    # 上界:10 页 × 每页最多 2 次调用(设计 §7.5 的量级:Opus 5.5 一个站约 $0.3–0.5,上界是它的两倍)
    assert upper_bound_usd(opus, 10, 3000, 2000, "2026-11-17") == pytest.approx(1.04)


def test_choose_provider_follows_available_keys():
    assert choose_provider(PRICING, ["openai", "anthropic"]) == ("anthropic", "claude-opus-5-5")
    assert choose_provider(PRICING, ["gemini"]) == ("gemini", "gemini-4-argon")
    assert choose_provider(PRICING, ["openai", "anthropic"], preferred="openai") == ("openai", "gpt-6.2-sol")
    assert choose_provider(PRICING, ["openai"], preferred="gemini") == ("openai", "gpt-6.2-sol")  # 指定的那家没有 key
    assert choose_provider(PRICING, []) is None


def test_bad_pricing_files_are_rejected():
    doc = PRICING.model_dump()
    with pytest.raises(PricingError):
        parse_pricing({**doc, "models": doc["models"] + [doc["models"][0]]}, origin="t")
    with pytest.raises(PricingError):
        parse_pricing({**doc, "default_models": {"openai": "no-such-model"}}, origin="t")
    with pytest.raises(PricingError):
        parse_pricing({"models": []}, origin="t")


# ---- 密钥 ----

def test_keys_come_from_environment_only(tmp_path):
    assert secret_for("anthropic", {}) is None and available_providers({}) == []
    assert available_providers({"GOOGLE_API_KEY": "x", "OPENAI_API_KEY": " "}) == ["gemini"]
    env_file = tmp_path / ".env"
    env_file.write_text("# c\nOPENAI_API_KEY='abc'\nexport GEMINI_API_KEY=xyz\nOTHER_SECRET=nope\nBROKEN LINE\n", encoding="utf-8")
    assert read_env_file(env_file) == {"OPENAI_API_KEY": "abc", "GEMINI_API_KEY": "xyz"}  # 别的变量一概不碰


# ---- 请求长什么样 ----

def test_anthropic_request_shape():
    client, seen = transport((200, ANTHROPIC_OK))
    schema = {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"],
              "additionalProperties": False}
    out = call("anthropic", "claude-opus-5-5", client, system="You write titles.", schema=schema)
    req = seen[0]
    body = json.loads(req.content)
    assert str(req.url) == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == SECRET and req.headers["anthropic-version"] == "2026-10-21"
    assert body["model"] == "claude-opus-5-5" and body["max_tokens"] == 16000
    assert body["system"] == "You write titles." and body["messages"] == [{"role": "user", "content": "Write a title."}]
    assert body["output_config"] == {"effort": "medium", "format": {"type": "json_schema", "schema": schema}}
    for banned in ("temperature", "top_p", "top_k", "thinking", "tool_choice"):  # Opus 5.5 不收这些
        assert banned not in body
    assert out.text == '{"title": "Classic Kettle"}' and out.parsed == {"title": "Classic Kettle"}
    assert (out.tokens_in, out.tokens_out, out.tries, out.cached) == (3000, 1500, 1, False)
    assert out.cost_usd == pytest.approx(0.042) and not out.error


def test_openai_and_gemini_request_shapes():
    client, seen = transport((200, OPENAI_OK))
    out = call("openai", "gpt-6.2-sol", client, system="S")
    body = json.loads(seen[0].content)
    assert str(seen[0].url) == "https://api.openai.com/v1/responses"
    assert seen[0].headers["authorization"] == f"Bearer {SECRET}"
    assert body == {"model": "gpt-6.2-sol", "input": "S\n\nWrite a title.", "max_output_tokens": 16000, "store": False}
    assert out.parsed == {"title": "Classic Kettle"} and out.cost_usd == pytest.approx(0.021)  # 抠出代码块里的 JSON

    client, seen = transport((200, GEMINI_OK))
    out = call("gemini", "gemini-4-argon", client)
    body = json.loads(seen[0].content)
    assert str(seen[0].url) == "https://generativelanguage.googleapis.com/v1beta/interactions"
    assert seen[0].headers["x-goog-api-key"] == SECRET
    assert body == {"model": "gemini-4-argon", "input": "Write a title.", "store": False}
    assert out.tokens_out == 1500 and out.cost_usd == pytest.approx(0.021)  # 思考 token 按输出价


# ---- 出错怎么办 ----

def test_retries_rate_limit_then_succeeds_and_counts_tries():
    waits = []
    client, seen = transport((429, {"error": {"type": "rate_limit_error"}}), (529, {}), (200, ANTHROPIC_OK))
    out = call("anthropic", "claude-opus-5-5", client, sleep=waits.append)
    assert out.tries == 3 and len(seen) == 3 and waits == [2.0, 6.0] and not out.error and out.text


def test_gives_up_after_two_retries():
    client, seen = transport(httpx.ConnectError("boom"), httpx.ReadTimeout("slow"), (503, {}))
    out = call("openai", "gpt-6.2-sol", client)
    assert out.tries == 3 and out.error_kind == "server" and out.text == "" and out.cost_usd == 0


def test_account_errors_stop_at_once_and_never_leak_the_secret():
    for status, payload in ((401, {"error": {"message": f"bad key {SECRET}"}}), (402, {}), (403, {}),
                            (429, {"error": {"code": "insufficient_quota"}})):
        client, seen = transport((status, payload))
        out = call("openai", "gpt-6.2-sol", client)
        assert out.error_kind == "account" and out.tries == 1 and len(seen) == 1, status
        assert "余额或密钥" in out.error and SECRET not in out.error and SECRET not in out.model_dump_json()


def test_bad_request_is_not_retried():
    client, seen = transport((400, {"error": {"type": "invalid_request_error"}}))
    out = call("anthropic", "claude-opus-5-5", client)
    assert out.error_kind == "bad_request" and out.tries == 1


def test_refusal_truncation_and_malformed_answers():
    client, _ = transport((200, {"content": [], "stop_reason": "refusal", "usage": {"input_tokens": 100, "output_tokens": 5}}))
    out = call("anthropic", "claude-opus-5-5", client)
    assert out.error_kind == "refusal" and out.text == "" and out.tokens_in == 100 and out.cost_usd > 0  # 花了的钱照记
    client, _ = transport((200, {**ANTHROPIC_OK, "stop_reason": "max_tokens"}))
    out = call("anthropic", "claude-opus-5-5", client)
    assert out.error_kind == "truncated" and out.text  # 截断的回答带回去,由调用方决定
    client, seen = transport(*[(200, {"unexpected": True})] * 3)
    out = call("gemini", "gemini-4-argon", client)
    assert out.error_kind == "server" and len(seen) == 3  # 没有 status:当平台出错,重试后放弃
    client, _ = transport((200, {"status": "completed", "steps": "oops"}))
    assert call("gemini", "gemini-4-argon", client).error_kind == "malformed"
    client, _ = transport((200, {"status": "completed", "usage": {},
                                 "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]}))
    assert call("openai", "gpt-6.2-sol", client).error_kind == "refusal"


def test_unknown_model_and_missing_key_do_not_call_out():
    client, seen = transport()
    assert call("anthropic", "claude-made-up", client).error_kind == "unknown_model"
    assert call("perplexity", "sonar", client).error_kind == "unknown_model"
    assert call("anthropic", "claude-opus-5-5", client, env={}).error_kind == "no_key"
    assert seen == []


# ---- 缓存 ----

def test_cache_hit_costs_nothing_and_holds_no_secret(tmp_path):
    client, seen = transport((200, ANTHROPIC_OK))
    first = call("anthropic", "claude-opus-5-5", client, cache_dir=tmp_path)
    again = call("anthropic", "claude-opus-5-5", client, cache_dir=tmp_path)   # 队列已空:真发请求会报错
    assert len(seen) == 1 and again.cached and again.cost_usd == 0 and again.tries == 0
    assert again.text == first.text and again.parsed == first.parsed and again.request_hash == first.request_hash
    files = list(tmp_path.rglob("*.json"))
    assert len(files) == 1 and SECRET not in files[0].read_text(encoding="utf-8")
    client, seen = transport((200, ANTHROPIC_OK))
    other = complete("anthropic", "claude-opus-5-5", "Write a title.", pricing=PRICING, prompt_version="page_v2",
                     client=client, env=ENV, cache_dir=tmp_path, today="2026-11-17")
    assert len(seen) == 1 and not other.cached and other.request_hash != first.request_hash  # 提示词版本变了就不算同一次


def test_failed_answers_are_not_cached(tmp_path):
    client, _ = transport((400, {}))
    call("anthropic", "claude-opus-5-5", client, cache_dir=tmp_path)
    assert list(tmp_path.rglob("*.json")) == []


# ---- 抠 JSON ----

def test_extract_json_from_messy_text():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Sure!\n```json\n[{"a": 1}]\n```\nDone.') == [{"a": 1}]
    assert extract_json('note {not json} then {"ok": true} tail') == {"ok": True}
    assert extract_json("no json here") is None and extract_json("") is None


def test_request_hash_ignores_nothing_but_the_secret():
    body = wire.build_body("anthropic", "claude-opus-5-5", "p")
    assert wire.request_hash("anthropic", "claude-opus-5-5", "v1", body) == wire.request_hash("anthropic", "claude-opus-5-5", "v1", body)
    assert wire.request_hash("anthropic", "claude-opus-5-5", "v1", body) != wire.request_hash("anthropic", "claude-opus-5-5", "v2", body)
    assert SECRET not in json.dumps(body)


def test_out_of_credit_is_an_account_problem_whatever_the_status():
    # Anthropic 把"余额不足"放在 400 里,OpenAI 放在 429 里:都算账户问题(立刻停、提示查余额),不是"请求写错"或"限流"
    anthropic = ('{"type":"error","error":{"type":"invalid_request_error",'
                 '"message":"Your credit balance is too low to access the Anthropic API."}}')
    assert wire.classify_http(400, anthropic) == "account"
    assert wire.classify_http(429, '{"error":{"code":"insufficient_quota"}}') == "account"
    assert wire.classify_http(400, '{"error":{"message":"max_tokens: field required"}}') == "bad_request"
    assert wire.classify_http(429, '{"error":{"message":"slow down"}}') == "rate_limit"


def test_env_file_saved_with_a_bom_still_gives_the_first_key(tmp_path):
    env_file = tmp_path / ".env"   # 记事本存成"带 BOM 的 UTF-8":BOM 不能粘在第一行的变量名上
    env_file.write_bytes(b"\xef\xbb\xbfANTHROPIC_API_KEY=abc\nOPENAI_API_KEY=def\n")
    assert read_env_file(env_file) == {"ANTHROPIC_API_KEY": "abc", "OPENAI_API_KEY": "def"}
