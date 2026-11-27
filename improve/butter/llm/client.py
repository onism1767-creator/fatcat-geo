"""调模型(外壳:联网、读时钟、等待)。一次 complete() = 查缓存 → 发请求(出错按类退避重试)→ 翻译 → 算钱 → 存缓存。

- 总是返回 Completion,出错写在 error / error_kind 里,不抛异常(起草失败只影响起草,设计 §7.6)。
- 账户类错误(密钥、余额、权限)不重试,由调用方立即停止后面的起草。
- 网络错误、超时、限流、平台内部错误:退避后重试,最多 2 次(设计 §7.2)。
- 密钥只放在请求头里:不进缓存、不进指纹、不进报错文字。
- 命中缓存不发请求、不花钱(cached=True,cost_usd=0)。只缓存成功的回答。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path

import httpx

from butter import store
from butter.contracts.llm import Completion, Pricing
from butter.llm import wire
from butter.llm.keys import secret_for
from butter.llm.pricing import cost_usd, find_price

MAX_RETRIES = 2
BACKOFF_S = (2.0, 6.0)
RETRYABLE = ("rate_limit", "server", "network", "timeout")


def _scrub(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def complete(
    provider: str, model: str, prompt: str, *, pricing: Pricing, prompt_version: str, system: str = "",
    schema: dict | None = None, max_tokens: int = 16000, timeout: float = 120.0, cache_dir: Path | None = None,
    env: Mapping[str, str] | None = None, client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep, today: str | None = None,
) -> Completion:
    base = {"provider": provider, "model": model}
    price = find_price(pricing, provider, model)
    if provider not in wire.URLS or price is None:
        return Completion(**base, error_kind="unknown_model", error=f"价目表里没有 {provider}:{model}")
    body = wire.build_body(provider, model, prompt, system=system, schema=schema, max_tokens=max_tokens)
    digest = wire.request_hash(provider, model, prompt_version, body)
    base["request_hash"] = digest
    if cache_dir is not None:
        hit = store.read_llm_cache(cache_dir, digest)
        if hit is not None:
            return Completion(**base, text=hit["text"], parsed=wire.extract_json(hit["text"]),
                              tokens_in=hit.get("tokens_in", 0), tokens_out=hit.get("tokens_out", 0), cached=True)
    secret = secret_for(provider, env)
    if not secret:
        return Completion(**base, error_kind="no_key", error=f"没有 {provider} 的密钥")

    own_client = client is None
    http = client or httpx.Client()
    today = today or date.today().isoformat()
    spent_in = spent_out = tries = 0
    started = time.monotonic()
    kind = message = ""
    parsed = wire.Parsed()
    try:
        for attempt in range(MAX_RETRIES + 1):
            tries += 1
            try:
                response = http.post(wire.URLS[provider], headers=wire.auth_headers(provider, secret), json=body,
                                     timeout=timeout)
            except httpx.TimeoutException:
                kind, message = "timeout", f"{timeout:.0f} 秒内没有回答"
            except httpx.HTTPError as exc:
                kind, message = "network", f"网络错误:{type(exc).__name__}"
            else:
                if response.status_code >= 400:
                    kind = wire.classify_http(response.status_code, response.text)
                    message = _scrub(f"HTTP {response.status_code}:{response.text[:300]}", secret)
                else:
                    try:
                        data = response.json()
                    except ValueError:
                        data = None
                    parsed = wire.parse_response(provider, data)
                    spent_in += parsed.tokens_in
                    spent_out += parsed.tokens_out
                    kind, message = parsed.error_kind, parsed.error
            if kind not in RETRYABLE or attempt == MAX_RETRIES:
                break
            sleep(BACKOFF_S[attempt])
    finally:
        if own_client:
            http.close()
    latency = int((time.monotonic() - started) * 1000)
    cost = cost_usd(price, spent_in, spent_out, today)
    common = dict(**base, tokens_in=spent_in, tokens_out=spent_out, cost_usd=cost, latency_ms=latency, tries=tries)
    if kind == "account":
        message = f"检查这家的余额或密钥。{message}"
    if kind and kind != "truncated":
        return Completion(**common, error=message, error_kind=kind)
    if cache_dir is not None and not kind:
        store.write_llm_cache(cache_dir, digest, {"provider": provider, "model": model, "prompt_version": prompt_version,
                                                  "text": parsed.text, "tokens_in": parsed.tokens_in,
                                                  "tokens_out": parsed.tokens_out})
    return Completion(**common, text=parsed.text, parsed=wire.extract_json(parsed.text), error=message, error_kind=kind)
