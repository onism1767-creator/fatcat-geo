"""调模型的数据形状(设计 §7.2、§7.5)。llm/1、pricing/1。"""

from __future__ import annotations

from typing import Any, Literal

from butter.contracts.common import Frozen

Provider = Literal["anthropic", "openai", "gemini"]
# 出错的种类:account 立即停止起草并提示查余额或密钥;rate_limit / server / network / timeout 退避重试;
# refusal 模型拒答;truncated 回答被截断;malformed 平台回的结构看不懂;bad_request 请求本身不对
ErrorKind = Literal["", "account", "rate_limit", "server", "network", "timeout", "refusal", "truncated", "malformed",
                    "bad_request", "no_key", "unknown_model"]


class ModelPrice(Frozen):
    provider: Provider
    model: str
    input_per_mtok: float
    output_per_mtok: float
    source: str
    checked_on: str
    valid_until: str | None = None          # 促销价的最后一天(ISO 日期)
    after: dict[str, float] | None = None   # 到期之后的价


class Pricing(Frozen):
    provider_order: list[Provider]
    default_models: dict[str, str]
    models: list[ModelPrice]


class Completion(Frozen):
    provider: str
    model: str
    text: str = ""
    parsed: Any = None            # 从 text 里抠出来的 JSON;抠不出来是 None
    tokens_in: int = 0
    tokens_out: int = 0           # 含思考
    cost_usd: float = 0.0
    cached: bool = False          # 命中本机缓存,没有发请求,没有花钱
    latency_ms: int = 0
    tries: int = 0                # 发了几次请求
    error: str = ""
    error_kind: ErrorKind = ""
    request_hash: str = ""
