"""把"起不起草、用哪家"这件事接起来(外壳):看手上有哪家的 key,选型号,估上界,给出一次调模型的动作。

命令行和网页都用它。没有 key 就不起草:返回 None 和一句说明,规则生成的部分照常出(设计 §7.6)。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path

from butter.config import load_pricing
from butter.draft import PROMPT_VERSION, SYSTEM
from butter.llm import choose_provider, complete, find_price, upper_bound_usd
from butter.llm.keys import available_providers
from butter.pipeline import DraftSettings

MAX_IN_PER_PAGE = 4000
MAX_OUT_PER_PAGE = 4000


def draft_offer(env: Mapping[str, str], *, max_pages: int = 10, budget_usd: float = 1.0,
                provider: str | None = None, model: str | None = None) -> dict | None:
    """现在能不能起草、会用哪家、最多花多少。不调模型。没有 key 返回 None。"""
    pricing = load_pricing()
    choice = choose_provider(pricing, available_providers(env), provider)
    if choice is None:
        return None
    provider, model = choice[0], model or choice[1]
    price = find_price(pricing, provider, model)
    if price is None:
        return None
    bound = min(budget_usd, upper_bound_usd(price, max_pages, MAX_IN_PER_PAGE, MAX_OUT_PER_PAGE,
                                            date.today().isoformat()))
    return {"provider": provider, "model": model, "upper_bound_usd": round(bound, 2), "budget_usd": budget_usd}


def draft_settings(env: Mapping[str, str], cache_dir: Path, *, max_pages: int = 10, budget_usd: float = 1.0,
                   provider: str | None = None, model: str | None = None) -> tuple[DraftSettings | None, dict | None]:
    offer = draft_offer(env, max_pages=max_pages, budget_usd=budget_usd, provider=provider, model=model)
    if offer is None:
        return None, None
    pricing = load_pricing()

    def ask(prompt: str, schema: dict):
        return complete(offer["provider"], offer["model"], prompt, pricing=pricing, prompt_version=PROMPT_VERSION,
                        system=SYSTEM, schema=schema, cache_dir=cache_dir, env=env)

    return DraftSettings(provider=offer["provider"], model=offer["model"], ask=ask, budget_usd=budget_usd,
                         max_pages=max_pages), offer
