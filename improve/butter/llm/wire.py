"""三家接口的请求与回答长什么样(纯层:只拼请求体、只翻译回答,不联网、不碰密钥)。

依据(2026-11-17 核对):
- Anthropic:Messages API,`POST /v1/messages`,版本头 `anthropic-version: 2026-10-21`。Opus 5.5 / Sonnet 5.5 / Haiku 5.5
  不收 temperature 等采样参数;思考常开,深浅用 `output_config.effort`;要 JSON 用 `output_config.format`
  (不支持强制 tool_choice);回答可能以 `stop_reason: "refusal"` 结束,读内容前先判断。
- OpenAI:Responses API,`POST /v1/responses`;Gemini:Interactions API,`POST /v1beta/interactions`。
  这两家的写法照 Toast 适配器里真实跑通的口径(不联网、不带工具)。它们的「强制 JSON」参数没有核对过,
  所以不发:让模型按提示词输出 JSON,再从回答里抠出来校验(设计 ADR-B04)。温度参数同理不发。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2026-10-21"
OPENAI_URL = "https://api.openai.com/v1/responses"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
URLS = {"anthropic": ANTHROPIC_URL, "openai": OPENAI_URL, "gemini": GEMINI_URL}
DEFAULT_EFFORT = "medium"  # Opus 5.5 的默认;写明,免得换型号时默认值跟着变


def build_body(provider: str, model: str, prompt: str, *, system: str = "", schema: dict | None = None,
               max_tokens: int = 16000) -> dict[str, Any]:
    if provider == "anthropic":
        body: dict[str, Any] = {"model": model, "max_tokens": max_tokens,
                                "messages": [{"role": "user", "content": prompt}],
                                "output_config": {"effort": DEFAULT_EFFORT}}
        if system:
            body["system"] = system
        if schema is not None:
            body["output_config"]["format"] = {"type": "json_schema", "schema": schema}
        return body
    text = f"{system}\n\n{prompt}" if system else prompt
    if provider == "openai":
        return {"model": model, "input": text, "max_output_tokens": max_tokens, "store": False}
    if provider == "gemini":
        return {"model": model, "input": text, "store": False}
    raise ValueError(f"不认识的提供方:{provider}")


def auth_headers(provider: str, secret: str) -> dict[str, str]:
    if provider == "anthropic":
        return {"x-api-key": secret, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
    if provider == "openai":
        return {"Authorization": f"Bearer {secret}", "content-type": "application/json"}
    if provider == "gemini":
        return {"x-goog-api-key": secret, "content-type": "application/json"}
    raise ValueError(f"不认识的提供方:{provider}")


def request_hash(provider: str, model: str, prompt_version: str, body: dict[str, Any]) -> str:
    """缓存用的指纹:同一(提供方、型号、提示词版本、请求体)算同一次。里面没有密钥。"""
    raw = json.dumps([provider, model, prompt_version, body], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Parsed:
    __slots__ = ("text", "tokens_in", "tokens_out", "error_kind", "error")

    def __init__(self, text: str = "", tokens_in: int = 0, tokens_out: int = 0, error_kind: str = "", error: str = ""):
        self.text, self.tokens_in, self.tokens_out, self.error_kind, self.error = text, tokens_in, tokens_out, error_kind, error


def _int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) else 0


def parse_response(provider: str, data: Any) -> Parsed:
    """平台回答 → 文字与用量。结构不对记 malformed,不猜。花了多少 token 即使出错也照实带回。"""
    if not isinstance(data, dict):
        return Parsed(error_kind="malformed", error="回答不是 JSON 对象")
    try:
        if provider == "anthropic":
            usage = data.get("usage") or {}
            tin = _int(usage.get("input_tokens")) + _int(usage.get("cache_read_input_tokens")) \
                + _int(usage.get("cache_creation_input_tokens"))
            tout = _int(usage.get("output_tokens"))
            stop = data.get("stop_reason")
            if stop == "refusal":
                return Parsed(tokens_in=tin, tokens_out=tout, error_kind="refusal", error="模型拒绝回答(stop_reason=refusal)")
            text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
            if stop == "max_tokens":
                return Parsed(text, tin, tout, "truncated", "回答被长度上限截断(stop_reason=max_tokens)")
            return Parsed(text, tin, tout)
        if provider == "openai":
            usage = data.get("usage") or {}
            tin, tout = _int(usage.get("input_tokens")), _int(usage.get("output_tokens"))
            if data.get("status") == "failed":
                return Parsed(tokens_in=tin, tokens_out=tout, error_kind="server", error=f"status=failed:{data.get('error')}")
            parts, refusal = [], ""
            for item in data["output"]:
                if item.get("type") != "message":
                    continue
                for part in item.get("content") or []:
                    if part.get("type") == "output_text":
                        parts.append(part["text"])
                    elif part.get("type") == "refusal":
                        refusal = part.get("refusal") or "refusal"
            if refusal and not parts:
                return Parsed(tokens_in=tin, tokens_out=tout, error_kind="refusal", error=f"模型拒绝回答:{refusal[:200]}")
            if data.get("status") == "incomplete":
                return Parsed("".join(parts), tin, tout, "truncated", "回答不完整(status=incomplete)")
            return Parsed("".join(parts), tin, tout)
        if provider == "gemini":
            usage = data.get("usage") or {}
            tin = _int(usage.get("total_input_tokens"))
            tout = _int(usage.get("total_output_tokens")) + _int(usage.get("total_thought_tokens"))  # 思考按输出价
            status = data.get("status")
            if status not in ("completed", "incomplete"):
                return Parsed(tokens_in=tin, tokens_out=tout, error_kind="server", error=f"互动终态异常 status={status!r}")
            text = "".join(block["text"] for step in data["steps"] if step.get("type") == "model_output"
                           for block in step.get("content") or [] if block.get("type") == "text")
            if status == "incomplete":
                return Parsed(text, tin, tout, "truncated", "回答不完整(status=incomplete)")
            return Parsed(text, tin, tout)
    except (KeyError, TypeError, AttributeError) as exc:
        return Parsed(error_kind="malformed", error=f"回答结构看不懂:{type(exc).__name__}: {exc}")
    raise ValueError(f"不认识的提供方:{provider}")


NO_CREDIT_MARKS = ("insufficient_quota", "billing_hard_limit_reached", "credit balance is too low", "quota_exceeded")


def classify_http(status: int, body_text: str) -> str:
    """HTTP 出错归哪一类。账户类(密钥、余额、权限)立即停;限流与平台内部错误可以重试。

    OpenAI 把「没额度」放在 429 里,Anthropic 放在 400 里(credit balance is too low),都靠回答里的文字区分;
    认得出的按账户类,认不出的 429 按限流、400 按请求写错。
    """
    low = body_text.lower()
    if status in (401, 402, 403):
        return "account"
    if status in (400, 429) and any(mark in low for mark in NO_CREDIT_MARKS):
        return "account"
    if status == 429:
        return "rate_limit"
    if status in (500, 502, 503, 504, 529):
        return "server"
    return "bad_request"


def extract_json(text: str) -> Any:
    """从回答里抠出第一个完整的 JSON 对象或数组(可以包在 ``` 代码块或说明文字里)。抠不出来返回 None。"""
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                value, _ = decoder.raw_decode(text, i)
            except json.JSONDecodeError:
                continue
            return value
    return None
