"""L4 成本与时长预估(16.4;v0.4.17 起按库里实测;v0.4.19 按型号;v0.4.26 S2 次数固定;2026-11-03 成本调研②起按
「用量形状 × 牌价」、按语言分桶、同品牌上一单优先)——全项目唯一一份预估公式。

确认页每次勾选变化都向服务端要一次结果(POST /jobs/{id}/estimate),页面脚本只负责显示,不再自带公式。
每个型号的单价依次取:同品牌上一单(续跑时就是本单已问的)实测 → 本语言实测 → 不分语言实测 →
它自己不够数的几次实测的均价(2026-11-20 加)→ 同家默认型号的用量 × 这个型号自己的牌价 →
适配器声明的上界。实测的按「平均用量 × 现牌价」算(牌价变了不用重测);
没牌价的仪器按实测均价。要如实说:它堵不住「这套题让某家多搜一次」那种差额,开跑前没法知道会搜几次。
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from enum import StrEnum

from adapters.base import SurfaceAdapter, priced
from engine.models import FrozenModel, SearchMode, Target
from engine.page_check import BUDGET_S as VERIFY_BUDGET_S
from engine.storage import CallStat
from report.jobs import PLAN_CONCURRENCY, LegProgress

# 兜底值(v0.4.17):库里没有足量历史时用;有历史即按实测均值(香奈儿首单加权均值 20.2 s)
AVG_SECONDS_PER_CALL = 20.0
JUDGE_SECONDS_PER_CALL = 4.0  # 裁判不联网、输出短,一条约几秒(S9.3;有实测后改读实测)
MIN_STAT_SAMPLES = 10  # 少于这么多次历史调用不算「够数的实测」(v0.4.17)
# 不够数、但这个型号自己至少有这么多次实测:先按这几次的均价,不去借别的型号的用量
# (2026-11-20:Gemini 4 Hydrogen 试价只问到 3 次、均 $0.25,借同家默认型号的用量算出来是 $0.018,差 14 倍——
# 同一家的两个型号搜的次数可以差一个量级)。3 这个数是 Claude 定的
MIN_FEW_SAMPLES = 3
# 提问之前的准备(第二轮审核 B14:原来没算):官网审计(十来次爬虫探测、要时用真浏览器打开一次首页)、找官网名单
# (最多打开 20 个首页)、开跑前预检。粗估,有实测后改读实测
PREP_SECONDS = 120.0
# 提问之后核对 AI 引用 / 搜到的网页:要开多少页取决于 AI 引了什么,开跑前不知道;只知道它有上限,确认页单独写明
VERIFY_MAX_MINUTES = VERIFY_BUDGET_S / 60


class PriceBasis(StrEnum):
    """一个单价是怎么来的(确认页据此说明预估依据;越靠前越贴近这单)。"""

    BRAND = "brand"  # 同品牌上一单(续跑时就是本单已问的)的实测
    MEASURED = "measured"  # 库里实测:本语言够数用本语言,不够就不分语言
    FEW = "few"  # 这个型号自己只有几次实测(不到 MIN_STAT_SAMPLES):按这几次记的均价,不太稳,但比借别的型号的用量贴近
    BORROWED = "borrowed"  # 没实测过:同家默认型号的用量 × 这个型号自己的牌价
    DECLARED = "declared"  # 适配器声明的推导上界


class UnitPrice(FrozenModel):
    cost_usd: float
    basis: PriceBasis
    shape_of: str | None = None  # BORROWED 时借的是哪个型号的用量
    # 「贵的时候」的单价(用户 2026-11-03 定 Q1 乙 / Q7 乙):实测最贵的一成调用的平均用量 × 现牌价;没实测过的就是声明上界,
    # 不另加。确认页只多一句「贵的时候约 $N」,续跑弹窗的建议值用同一个数
    high_cost_usd: float


class UnitPrices(FrozenModel):
    """各被测对象的单次调用单价与平均耗时(一单一种语言,按它算)。"""

    locale: str
    default: dict[str, UnitPrice]  # Target.key → 开卷单价
    search_disabled: dict[str, UnitPrice]  # 支持闭卷腿的对象 → 闭卷单价
    seconds_per_call: float

    def cost(self, key: str, mode: SearchMode) -> float:
        table = self.search_disabled if mode is SearchMode.SEARCH_DISABLED else self.default
        return table[key].cost_usd

    def high(self, key: str, mode: SearchMode) -> float:
        """「贵的时候」的单价(Q1 / Q7)。"""
        table = self.search_disabled if mode is SearchMode.SEARCH_DISABLED else self.default
        return table[key].high_cost_usd

    def by_basis(self) -> dict[PriceBasis, list[Target]]:
        """开卷单价按依据分组(确认页说明用),依据按可信度排,组内按对象原序。"""
        groups: dict[PriceBasis, list[Target]] = {}
        for basis in PriceBasis:
            targets = [Target.parse(key) for key, price in self.default.items() if price.basis is basis]
            if targets:
                groups[basis] = targets
        return groups


class _Bucket(FrozenModel):
    """几个统计桶合成的一桶(按次数加权)。"""

    n: int
    n_usage: int
    avg_cost_usd: float
    avg_latency_ms: float
    usage: dict[str, float]
    n_high: int  # 最贵的一成有几次(Q1 / Q7)
    usage_high: dict[str, float]


def _merge(stats: Sequence[CallStat]) -> _Bucket | None:
    n = sum(st.n for st in stats)
    if n == 0:
        return None
    n_usage = sum(st.n_usage for st in stats)
    n_high = sum(st.n_high for st in stats)

    def weighted(means_of: Callable[[CallStat], dict[str, float]], weight_of: Callable[[CallStat], int],
                 total_weight: int) -> dict[str, float]:
        totals: dict[str, float] = {}
        for st in stats:
            for key, mean in means_of(st).items():
                totals[key] = totals.get(key, 0.0) + mean * weight_of(st)
        return {key: total / total_weight for key, total in totals.items()} if total_weight else {}

    return _Bucket(
        n=n, n_usage=n_usage, n_high=n_high,
        avg_cost_usd=sum(st.avg_cost_usd * st.n for st in stats) / n,
        avg_latency_ms=sum(st.avg_latency_ms * st.n for st in stats) / n,
        usage=weighted(lambda st: st.usage, lambda st: st.n_usage, n_usage),
        usage_high=weighted(lambda st: st.usage_high, lambda st: st.n_high, n_high),
    )


def _bucket(
    stats: Sequence[CallStat], target: Target, mode: SearchMode, locale: str, *, same_locale_only: bool = False,
) -> _Bucket | None:
    """够数(≥ MIN_STAT_SAMPLES 次)的实测桶:先本语言,不够再把各语言合成一桶(库里中文比英文贵 8–70%,调研 1.2 节)。"""
    same = [st for st in stats if (st.surface, st.model, st.search_mode) == (target.surface, target.model, mode.value)]
    for group in ([st for st in same if st.locale == locale], [] if same_locale_only else same):
        bucket = _merge(group)
        if bucket is not None and bucket.n >= MIN_STAT_SAMPLES:
            return bucket
    return None


def _few(stats: Sequence[CallStat], target: Target, mode: SearchMode, locale: str) -> _Bucket | None:
    """这个型号自己不够数的那几次实测(先本语言,没有再各语言合起来);少于 MIN_FEW_SAMPLES 次不用。"""
    same = [st for st in stats if (st.surface, st.model, st.search_mode) == (target.surface, target.model, mode.value)]
    for group in ([st for st in same if st.locale == locale], same):
        bucket = _merge(group)
        if bucket is not None and bucket.n >= MIN_FEW_SAMPLES:
            return bucket
    return None


def _shape_price(bucket: _Bucket, prices: dict[str, float] | None) -> float:
    """桶的单价:有牌价且用量够数按「平均用量 × 牌价」;没牌价(测试替身)或用量没补齐按实测均价。"""
    if prices is not None and bucket.n_usage >= MIN_STAT_SAMPLES:
        return priced(prices, bucket.usage)
    return bucket.avg_cost_usd


def _high_price(bucket: _Bucket, prices: dict[str, float] | None, usual: float) -> float:
    """「贵的时候」的单价(Q1 / Q7):最贵的一成调用的平均用量 × 牌价;分不出一成(或没牌价、用量没补齐)就等于平常的单价,
    不臆造;至少不低于平常的单价。"""
    if prices is not None and bucket.n_usage >= MIN_STAT_SAMPLES and bucket.n_high and bucket.usage_high:
        return max(usual, priced(prices, bucket.usage_high))
    return usual


def _seconds_per_call(stats: Sequence[CallStat], locale: str) -> float:
    """平均耗时同样先按语言(中文调用比英文慢 30–70%,调研 1.2 节 k),不够再不分语言,再兜底。"""
    for group in ([st for st in stats if st.locale == locale], stats):
        timed = [st for st in group if st.n >= MIN_STAT_SAMPLES]
        if timed:
            return round(sum(st.avg_latency_ms * st.n for st in timed) / sum(st.n for st in timed) / 1000, 1)
    return AVG_SECONDS_PER_CALL


def unit_prices(
    adapters: dict[str, SurfaceAdapter], stats: Sequence[CallStat], *, locale: str,
    brand_stats: Sequence[CallStat] = (),
) -> UnitPrices:
    """stats = 全库统计;brand_stats = 同品牌各单的统计(含本单已问的),有够数的就先用它(D5)。"""

    def price(target: Target, mode: SearchMode, declared: float) -> UnitPrice:
        meta = adapters[target.surface].meta()
        table = meta.price_table.get(target.model)
        own = _bucket(brand_stats, target, mode, locale, same_locale_only=True)
        if own is not None:
            usual = _shape_price(own, table)
            return UnitPrice(cost_usd=usual, basis=PriceBasis.BRAND, high_cost_usd=_high_price(own, table, usual))
        measured = _bucket(stats, target, mode, locale)
        if measured is not None:
            usual = _shape_price(measured, table)
            return UnitPrice(cost_usd=usual, basis=PriceBasis.MEASURED,
                             high_cost_usd=_high_price(measured, table, usual))
        few = _few(stats, target, mode, locale)
        if few is not None:  # 自己有几次:按记的均价(次数少,不分「贵的时候」)
            return UnitPrice(cost_usd=few.avg_cost_usd, basis=PriceBasis.FEW, high_cost_usd=few.avg_cost_usd)
        if table is not None and target.model != meta.default_model:
            lender = _bucket(stats, Target(surface=target.surface, model=meta.default_model), mode, locale)
            if lender is not None and lender.n_usage >= MIN_STAT_SAMPLES:
                usual = priced(table, lender.usage)  # 借默认型号的用量形状,「贵的时候」也借它最贵一成的形状
                return UnitPrice(cost_usd=usual, basis=PriceBasis.BORROWED, shape_of=meta.default_model,
                                 high_cost_usd=_high_price(lender, table, usual))
        return UnitPrice(cost_usd=declared, basis=PriceBasis.DECLARED, high_cost_usd=declared)  # 声明的本来就是上界

    default: dict[str, UnitPrice] = {}
    search_disabled: dict[str, UnitPrice] = {}
    for name in sorted(adapters):
        meta = adapters[name].meta()
        for model in meta.models:
            target = Target(surface=name, model=model)
            default[target.key] = price(target, SearchMode.DEFAULT, meta.est_cost_per_call_usd[model])
            if meta.est_cost_per_call_search_disabled_usd is not None:
                search_disabled[target.key] = price(
                    target, SearchMode.SEARCH_DISABLED, meta.est_cost_per_call_search_disabled_usd[model])
    return UnitPrices(locale=locale, default=default, search_disabled=search_disabled,
                      seconds_per_call=_seconds_per_call(stats, locale))


class Estimate(FrozenModel):
    calls: int
    cost_usd: float
    high_cost_usd: float  # 「贵的时候约 $N」(Q1):各腿按「贵的时候」的单价算,裁判费照加
    minutes: float
    per_target: list[tuple[Target, float]]  # 各对象的花费(含闭卷腿),与勾选同序
    judge_calls: int  # AI 裁判要判几条回答:高档 = 提问次数;中档 = 最多判几条(名字没风险时 0);低档 0
    judge_cost_usd: float
    judge_upto: bool = False  # True = 上面两个数是上限:只有说不准的回答才请裁判(中档,BR4)


def estimate_job(
    *,
    n_prompts: int,
    runs_per_prompt: int,
    targets: list[Target],
    dependency: bool,
    prices: UnitPrices,
    judge_cost_per_call: float | None = None,
    judge_limit: int | None = None,
) -> Estimate:
    """每条腿(一个对象 × 一种检索模式)的调用数 = 题数 × 每题次数(次数随档位,S9.1);
    启用检索依赖对照时,支持闭卷的对象多一条闭卷腿,按闭卷单价另计;
    judge_cost_per_call 非空时再按裁判单价另计:高档每条回答判一次(S9.3);给了 judge_limit 是中档,
    只判说不准的,开跑前不知道有几条,按上限算(BR4)。"""
    per_leg_calls = n_prompts * runs_per_prompt
    per_target: list[tuple[Target, float]] = []
    legs = 0
    high = 0.0
    for target in targets:
        cost = prices.cost(target.key, SearchMode.DEFAULT) * per_leg_calls
        high += prices.high(target.key, SearchMode.DEFAULT) * per_leg_calls
        legs += 1
        if dependency and target.key in prices.search_disabled:
            cost += prices.cost(target.key, SearchMode.SEARCH_DISABLED) * per_leg_calls
            high += prices.high(target.key, SearchMode.SEARCH_DISABLED) * per_leg_calls
            legs += 1
        per_target.append((target, cost))
    calls = per_leg_calls * legs
    judge_calls = calls if judge_cost_per_call is not None else 0
    if judge_limit is not None:
        judge_calls = min(judge_calls, judge_limit)
    judge_cost = judge_calls * (judge_cost_per_call or 0.0)
    # 时长按真实的并发方式算:并发是按腿分的,一条腿内部严格串行,所以是「几轮腿 × 每腿时长」,不是总调用数 ÷ 3——
    # 只选 1 个型号时原公式会把时长少报到三分之一(审核 2026-10-27 黄 16);裁判是把全部回答摊到 3 路并发上判
    rounds = math.ceil(legs / PLAN_CONCURRENCY)
    sampling_s = rounds * per_leg_calls * prices.seconds_per_call
    judging_s = judge_calls * JUDGE_SECONDS_PER_CALL / PLAN_CONCURRENCY
    return Estimate(
        calls=calls,
        cost_usd=sum(cost for _, cost in per_target) + judge_cost,
        high_cost_usd=high + judge_cost,
        minutes=(PREP_SECONDS + sampling_s + judging_s) / 60,  # 核对网页不在里面(上限另写,见 VERIFY_MAX_MINUTES)
        per_target=per_target,
        judge_calls=judge_calls,
        judge_cost_usd=judge_cost,
        judge_upto=judge_limit is not None,
    )


# 边跑边重估(用户 2026-11-18 定;任务列表第十七节 1):
REESTIMATE_MIN_RUNS = 5  # 一个型号自己真花钱问满这么多次,才用它这一单的均价;不够的先按开跑前的单价
OVERRUN_FACTOR = 1.5  # 预计总花费到了开跑前预估的这么多倍(超出五成)才提醒


class Projection(FrozenModel):
    """照目前的花费,这一单问完要多少钱。"""

    total_usd: float  # 已花的 + 没问的次数 × 各条腿现在的单价(不看上限,假装都问得完)
    estimate_usd: float | None  # 开跑前估的;旧单没有
    over_estimate: bool  # 比开跑前估的贵出 OVERRUN_FACTOR 倍以上
    cap_pct: int | None  # 有腿会在问完之前到它那份费用上限时:整单大约问到百分之几;都问得完 → None


def project_spend(legs: Iterable[LegProgress], *, spent_usd: float, estimate_usd: float | None) -> Projection | None:
    """开跑前的预估猜不准 AI 会搜几次,所以开跑后拿这一单自己的数再算一遍。每条腿各算各的:真花钱问满
    REESTIMATE_MIN_RUNS 次的用它自己的均价,不够的仍用开跑前的单价。还没有哪条腿问够、或者都问完了 → None(不显示)。
    spent_usd 是整单已花的(含出题等辅助调用),腿以外的那部分照实加上。只读数字,不改任何东西。"""
    legs = list(legs)
    if not any(leg.paid_runs >= REESTIMATE_MIN_RUNS for leg in legs) or all(leg.done >= leg.slots for leg in legs):
        return None
    total = spent_usd - sum(leg.spent_usd for leg in legs)
    reach = 0
    capped = False
    for leg in legs:
        unit = _unit_now(leg)
        remaining = max(0, leg.slots - leg.done)
        total += leg.spent_usd + remaining * unit
        affordable = remaining
        if leg.budget_usd is not None and unit > 0:  # 调度器在「已花 ≥ 这条腿的上限」时才停:还发得出几次
            affordable = min(remaining, max(0, math.ceil((leg.budget_usd - leg.spent_usd) / unit)))
            capped = capped or affordable < remaining
        reach += leg.done + affordable
    slots = sum(leg.slots for leg in legs)
    return Projection(
        total_usd=total, estimate_usd=estimate_usd,
        over_estimate=bool(estimate_usd) and total >= OVERRUN_FACTOR * (estimate_usd or 0.0),
        cap_pct=round(100 * reach / slots) if capped and slots else None,
    )


def _unit_now(leg: LegProgress) -> float:
    """这条腿现在按什么单价算:自己真花钱问满 REESTIMATE_MIN_RUNS 次用这一单的均价,不够的用开跑前的单价。"""
    return leg.spent_usd / leg.paid_runs if leg.paid_runs >= REESTIMATE_MIN_RUNS else leg.unit_usd


def suggested_cap(projection: Projection, budget_usd: float) -> float:
    """任务页「把上限提高到 $N」的 N:照目前的花费问完要的钱再留一成余量,向上取到 0.1 美元;至少比现在的上限高 0.1
    (上限只能往上调)。"""
    return max((int(projection.total_usd * 1.1 * 10) + 1) / 10, round(budget_usd + 0.1, 1))


def rebalance(legs: Iterable[LegProgress], budget_usd: float) -> None:
    """中途提高了上限:新上限按「还要花的」比例重新分给还在问的腿,当场改它们的 budget_usd(提问循环每次提问前读)。
    和开跑时同一个分法(split_budget),只是单价换成各条腿现在的单价;这一次已经停了的腿不再占额度,停在它已花的数,
    想补齐它们要等跑完后「接着跑」。"""
    legs = list(legs)
    weights = {i: 0.0 if leg.finished else max(0, leg.slots - leg.done) * _unit_now(leg) for i, leg in enumerate(legs)}
    shares = split_budget(budget_usd, weights, {i: leg.spent_usd for i, leg in enumerate(legs)})
    for i, leg in enumerate(legs):
        if not leg.finished:
            leg.budget_usd = shares[i]


def split_budget[K](
    budget_usd: float | None, weights: dict[K, float], spent: dict[K, float] | None = None,
) -> dict[K, float | None]:
    """费用上限按各条腿「还要花的预估」比例分(用户 2026-11-03 定):每条腿的上限 = 它已花的 + 剩余额度里按比例分到的一份。
    开跑时谁都没花,就是按预估比例分:预估 $6 时设 $10 全跑得完,总数仍不超上限。续跑时问完的腿不再占额度,
    提高的上限全给还没问完的腿——任务页弹窗建议的新上限就是按这个算的(照旧按全部预估分的话,问完的腿白占一份,
    补不齐)。原来每条腿平分,贵的型号先停、便宜的份额用不完(小米真跑:$10 分四份,Claude 问了 33/72 次就停)。
    没设上限 → 每条腿都不限;没有还要花的 → 各腿就是已花的数。"""
    if budget_usd is None:
        return dict.fromkeys(weights)
    spent = spent or dict.fromkeys(weights, 0.0)
    left = max(0.0, budget_usd - sum(spent.values()))
    total = sum(weights.values())
    if total <= 0:
        return {key: spent[key] for key in weights}
    return {key: spent[key] + left * weight / total for key, weight in weights.items()}
