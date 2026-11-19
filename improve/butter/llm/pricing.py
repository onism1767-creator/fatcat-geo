"""价目与算钱(纯层)。用量 × 价目 = 费用;促销价过了到期日用到期后的价。"""

from __future__ import annotations

from typing import Any

from butter.contracts.llm import ModelPrice, Pricing


class PricingError(ValueError):
    pass


def parse_pricing(doc: Any, *, origin: str) -> Pricing:
    try:
        pricing = Pricing.model_validate(doc)
    except Exception as exc:
        raise PricingError(f"{origin}:{exc}") from exc
    seen = set()
    for m in pricing.models:
        if (m.provider, m.model) in seen:
            raise PricingError(f"{origin}:型号重复 {m.provider}:{m.model}")
        seen.add((m.provider, m.model))
    for provider, model in pricing.default_models.items():
        if (provider, model) not in seen:
            raise PricingError(f"{origin}:默认型号 {provider}:{model} 不在价目表里")
    return pricing


def find_price(pricing: Pricing, provider: str, model: str) -> ModelPrice | None:
    return next((m for m in pricing.models if m.provider == provider and m.model == model), None)


def rates(price: ModelPrice, today: str) -> tuple[float, float]:
    """今天适用的(输入价, 输出价),美元 / 百万 token。today 是 ISO 日期。"""
    if price.valid_until and price.after and today > price.valid_until:
        return (price.after.get("input_per_mtok", price.input_per_mtok),
                price.after.get("output_per_mtok", price.output_per_mtok))
    return price.input_per_mtok, price.output_per_mtok


def cost_usd(price: ModelPrice, tokens_in: int, tokens_out: int, today: str) -> float:
    rate_in, rate_out = rates(price, today)
    return round((tokens_in * rate_in + tokens_out * rate_out) / 1_000_000, 6)


def upper_bound_usd(price: ModelPrice, pages: int, max_in_per_page: int, max_out_per_page: int, today: str,
                    calls_per_page: int = 2) -> float:
    """点「生成改动包」前显示的上界:页数上限 × 每页最大 token × 单价;每页最多一次检查后重写,所以乘 2(设计 §7.5)。"""
    return round(pages * calls_per_page * cost_usd(price, max_in_per_page, max_out_per_page, today), 4)


def choose_provider(pricing: Pricing, available: list[str], preferred: str | None = None) -> tuple[str, str] | None:
    """站长有哪家的 key 就用哪家;几家都有时按 provider_order 取第一家(设置里指定了就用指定的)。"""
    if preferred and preferred in available and preferred in pricing.default_models:
        return preferred, pricing.default_models[preferred]
    for provider in pricing.provider_order:
        if provider in available and provider in pricing.default_models:
            return provider, pricing.default_models[provider]
    return None
