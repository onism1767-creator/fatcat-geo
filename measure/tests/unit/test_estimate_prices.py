"""确认页的单价是怎么来的(2026-11-03 成本调研②):同品牌上一单 → 本语言实测 → 不分语言实测 →
借同家默认型号的用量 × 这个型号自己的牌价 → 声明价。实测的按「平均用量 × 现牌价」算,牌价变了预估跟着变、不用重测。"""

import pytest

from adapters.base import CapabilityRegistry, SurfaceAdapter, SurfaceMeta
from engine.models import SearchMode, Target
from engine.storage import CallStat
from report.estimate import AVG_SECONDS_PER_CALL, PriceBasis, unit_prices

CHEAP, PRICEY = "m-cheap", "m-pricey"
PRICES = {
    CHEAP: {"input_tokens": 1e-6, "output_tokens": 2e-6, "searches": 0.01},
    PRICEY: {"input_tokens": 10e-6, "output_tokens": 50e-6, "searches": 0.01},
}
SHAPE = {"input_tokens": 10000.0, "output_tokens": 1000.0, "searches": 1.5}


class PricedAdapter(SurfaceAdapter):
    def meta(self) -> SurfaceMeta:
        return SurfaceMeta(display_name="假 AI", api_name="假 AI", provider="test_provider", surface="priced",
                           models=[CHEAP, PRICEY], default_model=CHEAP, consumer_default_model=CHEAP,
                           strongest_model=PRICEY, models_checked="test",
                           capabilities=CapabilityRegistry(supports_search_disabled=True), price_table=PRICES,
                           est_cost_per_call_usd={CHEAP: 0.05, PRICEY: 0.5},
                           est_cost_per_call_search_disabled_usd={CHEAP: 0.01, PRICEY: 0.1})

    def ask(self, request):
        raise NotImplementedError

    def healthcheck(self) -> bool:
        return True


ADAPTERS = {"priced": PricedAdapter()}
cheap, pricey = Target(surface="priced", model=CHEAP), Target(surface="priced", model=PRICEY)


def stat(model: str, locale: str, n: int, *, usage: dict[str, float], cost: float = 0.0, latency_ms: float = 10000,
         mode: str = "default", n_usage: int | None = None) -> CallStat:
    return CallStat(surface="priced", model=model, search_mode=mode, locale=locale, n=n, avg_cost_usd=cost,
                    avg_latency_ms=latency_ms, usage=usage, n_usage=n if n_usage is None else n_usage)


def test_measured_price_is_average_usage_times_current_list_price():
    prices = unit_prices(ADAPTERS, [stat(CHEAP, "zh-CN", 12, usage=SHAPE, cost=0.9)], locale="zh-CN")
    unit = prices.default[cheap.key]
    assert unit.basis is PriceBasis.MEASURED
    assert unit.cost_usd == pytest.approx(10000 * 1e-6 + 1000 * 2e-6 + 1.5 * 0.01)  # 不是库里记的均价 0.9


def test_an_untested_model_borrows_the_default_models_usage_at_its_own_price():
    prices = unit_prices(ADAPTERS, [stat(CHEAP, "zh-CN", 12, usage=SHAPE)], locale="zh-CN")
    unit = prices.default[pricey.key]
    assert unit.basis is PriceBasis.BORROWED and unit.shape_of == CHEAP
    assert unit.cost_usd == pytest.approx(10000 * 10e-6 + 1000 * 50e-6 + 1.5 * 0.01)
    assert prices.by_basis() == {PriceBasis.MEASURED: [cheap], PriceBasis.BORROWED: [pricey]}


def test_without_any_history_the_declared_upper_bound_is_used():
    prices = unit_prices(ADAPTERS, [], locale="zh-CN")
    assert prices.default[pricey.key].basis is PriceBasis.DECLARED and prices.default[pricey.key].cost_usd == 0.5
    assert prices.cost(cheap.key, SearchMode.SEARCH_DISABLED) == 0.01
    assert prices.seconds_per_call == AVG_SECONDS_PER_CALL


def test_own_language_bucket_wins_and_too_few_falls_back_to_all_languages():
    zh = stat(CHEAP, "zh-CN", 12, usage={**SHAPE, "searches": 2.0}, latency_ms=20000)
    en = stat(CHEAP, "en-US", 30, usage={**SHAPE, "searches": 1.0}, latency_ms=8000)
    prices = unit_prices(ADAPTERS, [zh, en], locale="zh-CN")
    assert prices.default[cheap.key].cost_usd == pytest.approx(10000 * 1e-6 + 1000 * 2e-6 + 2.0 * 0.01)
    assert prices.seconds_per_call == 20.0
    # 本语言不够 10 次:各语言合成一桶,按次数加权;耗时同理
    few = zh.model_copy(update={"n": 6, "n_usage": 6})
    prices = unit_prices(ADAPTERS, [few, en], locale="zh-CN")
    searches = (2.0 * 6 + 1.0 * 30) / 36
    assert prices.default[cheap.key].cost_usd == pytest.approx(10000 * 1e-6 + 1000 * 2e-6 + searches * 0.01)
    assert prices.seconds_per_call == 8.0


def test_the_brands_own_previous_check_comes_first_but_only_in_the_same_language():
    everyone = stat(CHEAP, "zh-CN", 100, usage=SHAPE)
    this_brand = stat(CHEAP, "zh-CN", 12, usage={**SHAPE, "searches": 3.0})
    prices = unit_prices(ADAPTERS, [everyone], locale="zh-CN", brand_stats=[this_brand])
    assert prices.default[cheap.key].basis is PriceBasis.BRAND
    assert prices.default[cheap.key].cost_usd == pytest.approx(10000 * 1e-6 + 1000 * 2e-6 + 3.0 * 0.01)
    other_language = this_brand.model_copy(update={"locale": "en-US"})
    prices = unit_prices(ADAPTERS, [everyone], locale="zh-CN", brand_stats=[other_language])
    assert prices.default[cheap.key].basis is PriceBasis.MEASURED


def test_the_closed_book_leg_has_its_own_usage_and_lends_it_too():
    open_book = stat(CHEAP, "zh-CN", 12, usage=SHAPE)
    closed = stat(CHEAP, "zh-CN", 12, usage={"input_tokens": 500.0, "output_tokens": 800.0, "searches": 0.0},
                  mode="search_disabled")
    prices = unit_prices(ADAPTERS, [open_book, closed], locale="zh-CN")
    assert prices.cost(cheap.key, SearchMode.SEARCH_DISABLED) == pytest.approx(500 * 1e-6 + 800 * 2e-6)
    assert prices.cost(pricey.key, SearchMode.SEARCH_DISABLED) == pytest.approx(500 * 10e-6 + 800 * 50e-6)


def test_confirm_page_says_which_models_borrow_the_default_models_usage():
    """确认页按依据各说一句;实时预估对没实测的型号按借来的用量 × 它自己的牌价算。"""
    from fastapi.testclient import TestClient

    from report.app import create_app
    from tests.unit.test_report_page import REGISTRY

    stats = [stat(CHEAP, "zh-CN", 12, usage=SHAPE, latency_ms=15000)]
    app = create_app(call_stats=lambda brand=None: [] if brand else stats, adapters={"priced": PricedAdapter()},
                     dsn="postgresql://unused", evidence=REGISTRY, job_runner=lambda **kw: None)
    client = TestClient(app)
    resp = client.post("/jobs?lang=zh", data={"brand_name": "极光CRM", "aliases": "", "website": "aurora-crm.com",
                                              "category": "CRM", "questions": "问题一?", "generate": ""},
                       follow_redirects=False)
    page = client.get(resp.headers["location"]).text
    assert "预估按过去实测的平均费用和耗时算:假 AI · m-cheap" in page
    assert "假 AI · m-pricey 还没实测过,按同一家默认型号的用量乘它自己的单价算" in page
    assert "实际费用主要随 AI 搜几次、回答多长浮动" in page
    job_id = resp.headers["location"].split("/")[2]
    est = client.post(f"/jobs/{job_id}/estimate?lang=zh", data={
        "prompts_selected": ["0"], "targets_selected": [pricey.key], "tier": "low", "runs": "3"}).json()
    assert est["cost_usd"] == pytest.approx(3 * (10000 * 10e-6 + 1000 * 50e-6 + 1.5 * 0.01))


def test_a_bucket_without_usage_records_uses_the_recorded_average_and_lends_nothing():
    """用量还没补齐(或仪器没牌价、费用由平台自报):有实测均价就按均价;没有用量就没法借给别的型号。"""
    prices = unit_prices(ADAPTERS, [stat(CHEAP, "zh-CN", 12, usage={}, cost=0.07, n_usage=0)], locale="zh-CN")
    assert prices.default[cheap.key].cost_usd == 0.07 and prices.default[cheap.key].basis is PriceBasis.MEASURED
    assert prices.default[pricey.key].basis is PriceBasis.DECLARED
