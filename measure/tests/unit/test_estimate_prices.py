"""确认页的单价是怎么来的(2026-11-03 成本调研②):同品牌上一单 → 本语言实测 → 不分语言实测 →
借同家默认型号的用量 × 这个型号自己的牌价 → 声明价。实测的按「平均用量 × 现牌价」算,牌价变了预估跟着变、不用重测。"""

import pytest

from adapters.base import CapabilityRegistry, SurfaceAdapter, SurfaceMeta
from engine.models import SearchMode, Target
from engine.storage import CallStat
from report.estimate import (
    AVG_SECONDS_PER_CALL,
    PriceBasis,
    project_spend,
    rebalance,
    suggested_cap,
    unit_prices,
)
from report.jobs import LegProgress

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
                           main_model=PRICEY, models_checked="test",
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
         mode: str = "default", n_usage: int | None = None, usage_high: dict[str, float] | None = None) -> CallStat:
    return CallStat(surface="priced", model=model, search_mode=mode, locale=locale, n=n, avg_cost_usd=cost,
                    avg_latency_ms=latency_ms, usage=usage, n_usage=n if n_usage is None else n_usage,
                    usage_high=usage_high or {}, n_high=max(1, n // 10) if usage_high else 0)


HIGH = {"input_tokens": 30000.0, "output_tokens": 2000.0, "searches": 3.0}  # 最贵的一成调用的用量形状


def test_the_pricey_tenth_gives_a_second_unit_price_for_the_when_pricey_line():
    """用户 2026-11-03 定 Q1 乙:确认页只多一句「贵的时候约 $N」——每个型号按实测最贵的一成的用量 × 牌价;
    没实测过的本来就按声明上界,不另加;借用量的也借最贵一成的形状。"""
    measured = unit_prices(ADAPTERS, [stat(CHEAP, "zh-CN", 20, usage=SHAPE, usage_high=HIGH)], locale="zh-CN")
    unit = measured.default[cheap.key]
    assert unit.cost_usd == pytest.approx(10000 * 1e-6 + 1000 * 2e-6 + 1.5 * 0.01)
    assert unit.high_cost_usd == pytest.approx(30000 * 1e-6 + 2000 * 2e-6 + 3.0 * 0.01)
    borrowed = measured.default[pricey.key]
    assert borrowed.basis is PriceBasis.BORROWED
    assert borrowed.high_cost_usd == pytest.approx(30000 * 10e-6 + 2000 * 50e-6 + 3.0 * 0.01)
    # 分不出一成(没有 usage_high)→ 和平常一样;声明价本来就是上界
    plain = unit_prices(ADAPTERS, [stat(CHEAP, "zh-CN", 12, usage=SHAPE)], locale="zh-CN")
    assert plain.default[cheap.key].high_cost_usd == plain.default[cheap.key].cost_usd
    declared = unit_prices(ADAPTERS, [], locale="zh-CN")
    assert declared.default[pricey.key].high_cost_usd == 0.5 == declared.default[pricey.key].cost_usd
    # 整单的「贵的时候」= 各腿按贵的单价加总(裁判费照加)
    from report.estimate import estimate_job

    est = estimate_job(n_prompts=2, runs_per_prompt=3, targets=[cheap], dependency=False, prices=measured,
                       judge_cost_per_call=0.001)
    assert est.cost_usd == pytest.approx(6 * unit.cost_usd + 6 * 0.001)
    assert est.high_cost_usd == pytest.approx(6 * unit.high_cost_usd + 6 * 0.001)


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
    assert 'id="est-high"' in page and "e.high_cost_usd" in page  # 只多这一句(Q1 乙),页面脚本填数;文案进了 JSON 被转义
    job_id = resp.headers["location"].split("/")[2]
    est = client.post(f"/jobs/{job_id}/estimate?lang=zh", data={
        "prompts_selected": ["0"], "targets_selected": [pricey.key], "tier": "low", "runs": "3"}).json()
    assert est["cost_usd"] == pytest.approx(3 * (10000 * 10e-6 + 1000 * 50e-6 + 1.5 * 0.01))
    assert est["high_cost_usd"] == pytest.approx(est["cost_usd"])  # 这桶分不出最贵的一成:和平常一样


def test_a_bucket_without_usage_records_uses_the_recorded_average_and_lends_nothing():
    """用量还没补齐(或仪器没牌价、费用由平台自报):有实测均价就按均价;没有用量就没法借给别的型号。"""
    prices = unit_prices(ADAPTERS, [stat(CHEAP, "zh-CN", 12, usage={}, cost=0.07, n_usage=0)], locale="zh-CN")
    assert prices.default[cheap.key].cost_usd == 0.07 and prices.default[cheap.key].basis is PriceBasis.MEASURED
    assert prices.default[pricey.key].basis is PriceBasis.DECLARED


# ── 边跑边重估(用户 2026-11-18 定;任务列表第十七节 1) ───────────────────────


def _leg(**fields):
    base = dict(slots=20, done=0, spent_usd=0.0, paid_runs=0, unit_usd=0.10, budget_usd=None)
    return LegProgress(**{**base, **fields})


def test_reestimate_waits_until_one_model_has_five_paid_answers_of_its_own():
    assert project_spend([_leg(done=4, paid_runs=4, spent_usd=1.0)], spent_usd=1.0, estimate_usd=2.0) is None
    assert project_spend([], spent_usd=0.0, estimate_usd=None) is None
    done = _leg(done=20, paid_runs=20, spent_usd=5.0)
    assert project_spend([done], spent_usd=5.0, estimate_usd=2.0) is None  # 都问完了:没什么可预计的
    # 缓存复制来的不花钱,不算进「问满 5 次」:10 条里只有 3 条真花了钱
    assert project_spend([_leg(done=10, paid_runs=3, spent_usd=0.9)], spent_usd=0.9, estimate_usd=2.0) is None


def test_each_leg_uses_its_own_average_once_it_has_five_and_the_prerun_price_until_then():
    pricey = _leg(done=5, paid_runs=5, spent_usd=1.5)  # 自己的均价 $0.30,比开跑前的 $0.10 贵两倍
    fresh = _leg(done=2, paid_runs=2, spent_usd=1.0)  # 还不够 5 次:剩下 18 次仍按开跑前的 $0.10
    p = project_spend([pricey, fresh], spent_usd=2.5 + 0.2, estimate_usd=4.2)  # 另有 $0.2 是出题等辅助调用
    assert p is not None and p.total_usd == pytest.approx(0.2 + (1.5 + 15 * 0.30) + (1.0 + 18 * 0.10))
    assert p.estimate_usd == 4.2 and p.cap_pct is None
    assert p.over_estimate is True  # 9.0 ≥ 1.5 × 4.2
    calm = project_spend([_leg(done=5, paid_runs=5, spent_usd=0.6)], spent_usd=0.6, estimate_usd=2.0)
    assert calm is not None and calm.total_usd == pytest.approx(0.6 + 15 * 0.12)
    assert calm.over_estimate is False  # 1.2 倍:不提醒
    edge = project_spend([_leg(done=5, paid_runs=5, spent_usd=0.75)], spent_usd=0.75, estimate_usd=2.0)
    assert edge is not None and edge.total_usd == pytest.approx(3.0) and edge.over_estimate is True  # 正好五成:提醒
    old = project_spend([_leg(done=5, paid_runs=5, spent_usd=5.0)], spent_usd=5.0, estimate_usd=None)
    assert old is not None and old.over_estimate is False  # 旧单没有开跑前的预估:只报数,不比


def test_a_leg_that_will_hit_its_share_of_the_cap_says_how_far_the_check_will_get():
    # 这条腿分到 $2:已花 $1.5(5 次,均价 $0.30),还发得出 ⌈0.5 ÷ 0.3⌉ = 2 次 → 问到 7/20;另一条腿便宜,问得完
    tight = _leg(done=5, paid_runs=5, spent_usd=1.5, budget_usd=2.0)
    cheap = _leg(done=5, paid_runs=5, spent_usd=0.05, budget_usd=2.0)
    p = project_spend([tight, cheap], spent_usd=1.55, estimate_usd=4.0)
    assert p is not None and p.cap_pct == round(100 * (7 + 20) / 40)
    roomy = project_spend([_leg(done=5, paid_runs=5, spent_usd=0.5, budget_usd=5.0)], spent_usd=0.5, estimate_usd=2.0)
    assert roomy is not None and roomy.cap_pct is None  # 上限够:不提
    over = _leg(done=6, paid_runs=6, spent_usd=2.4, budget_usd=2.0)
    spent_out = project_spend([over], spent_usd=2.4, estimate_usd=2.0)
    assert spent_out is not None and spent_out.cap_pct == 30  # 已经过了它那份:一次也发不出了


def test_raising_the_cap_midway_resplits_it_among_the_legs_still_asking():
    """中途提高上限(任务页按钮):新上限按「还要花的」比例重分——单价用各条腿现在的(问满 5 次用自己的均价);
    这一次已经停了的腿不再占额度,停在它已花的数。"""
    pricey = _leg(done=5, paid_runs=5, spent_usd=1.5, budget_usd=2.0)  # 还要 15 × $0.30 = $4.50
    # 不够 5 次,用开跑前的单价:还要 18 × $0.10 = $1.80
    fresh = _leg(done=2, paid_runs=2, spent_usd=0.2, budget_usd=2.0)
    stopped = _leg(done=4, paid_runs=4, spent_usd=2.0, budget_usd=2.0, finished=True)  # 到上限停了:不再分
    rebalance([pricey, fresh, stopped], 10.0)
    left = 10.0 - (1.5 + 0.2 + 2.0)  # 剩下的额度全给还在问的两条腿
    assert pricey.budget_usd == pytest.approx(1.5 + left * 4.5 / 6.3)
    assert fresh.budget_usd == pytest.approx(0.2 + left * 1.8 / 6.3)
    assert stopped.budget_usd == 2.0  # 原样:想补齐它要等跑完后「接着跑」
    assert pricey.budget_usd + fresh.budget_usd + stopped.spent_usd == pytest.approx(10.0)  # 总数不超新上限


def test_the_suggested_new_cap_covers_the_projection_with_a_tenth_to_spare_and_only_goes_up():
    p = project_spend([_leg(done=5, paid_runs=5, spent_usd=1.5, budget_usd=3.0)], spent_usd=1.5, estimate_usd=2.0)
    assert p is not None and p.total_usd == pytest.approx(6.0)
    assert suggested_cap(p, 3.0) == 6.7  # 6.0 × 1.1 = 6.6,向上取到 0.1
    assert suggested_cap(p, 9.0) == 9.1  # 现在的上限已经更高:至少再高 0.1(上限只能往上调)
