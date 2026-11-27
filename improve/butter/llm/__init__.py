"""调模型(设计 §7.2)。wire、pricing 是纯层;keys 读环境变量;client 联网。三家的最小客户端自己写,不用厂商 SDK(ADR-B04)。"""

from butter.llm.client import complete
from butter.llm.keys import available_providers
from butter.llm.pricing import choose_provider, cost_usd, find_price, parse_pricing, upper_bound_usd
from butter.llm.wire import extract_json

__all__ = ["available_providers", "choose_provider", "complete", "cost_usd", "extract_json", "find_price",
           "parse_pricing", "upper_bound_usd"]
