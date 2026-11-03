"""L4 成本与时长预估(16.4;v0.4.17 起按库里实测;v0.4.19 按型号;v0.4.26 S2 次数固定)——全项目唯一一份预估公式。

确认页每次勾选变化都向服务端要一次结果(POST /jobs/{id}/estimate),页面脚本只负责显示,不再自带公式。
"""

from __future__ import annotations

import math

from adapters.base import SurfaceAdapter
from engine.models import FrozenModel, SearchMode, Target
from engine.page_check import BUDGET_S as VERIFY_BUDGET_S
from engine.storage import CallStat
from report.jobs import PLAN_CONCURRENCY

# 兜底值(v0.4.17):库里没有足量历史时用;有历史即按实测均值(香奈儿首单加权均值 20.2 s)
AVG_SECONDS_PER_CALL = 20.0
JUDGE_SECONDS_PER_CALL = 4.0  # 裁判不联网、输出短,一条约几秒(S9.3;有实测后改读实测)
MIN_STAT_SAMPLES = 10  # 少于这么多次历史调用不算数,仍用声明单价(v0.4.17)
# 提问之前的准备(第二轮审核 B14:原来没算):官网审计(十来次爬虫探测、要时用真浏览器打开一次首页)、找官网名单
# (最多打开 20 个首页)、开跑前预检。粗估,有实测后改读实测
PREP_SECONDS = 120.0
# 提问之后核对 AI 引用 / 搜到的网页:要开多少页取决于 AI 引了什么,开跑前不知道;只知道它有上限,确认页单独写明
VERIFY_MAX_MINUTES = VERIFY_BUDGET_S / 60


class UnitPrices(FrozenModel):
    """各被测对象的单次调用单价与平均耗时:有足量历史按实测均值,否则按适配器声明。"""

    default: dict[str, float]  # Target.key → 开卷单价
    search_disabled: dict[str, float]  # 支持闭卷腿的对象 → 闭卷单价
    seconds_per_call: float
    measured: list[Target]  # 按实测计价的对象(确认页据此说明预估依据)
    declared: list[Target]  # 历史不足、按声明单价计价的对象


def unit_prices(adapters: dict[str, SurfaceAdapter], stats: list[CallStat]) -> UnitPrices:
    by_key = {(st.surface, st.model, st.search_mode): st for st in stats}

    def price(target: Target, mode: SearchMode, declared: float) -> tuple[float, bool]:
        stat = by_key.get((target.surface, target.model, mode.value))
        if stat is not None and stat.n >= MIN_STAT_SAMPLES:
            return stat.avg_cost_usd, True
        return declared, False

    default: dict[str, float] = {}
    search_disabled: dict[str, float] = {}
    measured: list[Target] = []
    declared: list[Target] = []
    for name in sorted(adapters):
        meta = adapters[name].meta()
        for model in meta.models:
            target = Target(surface=name, model=model)
            default[target.key], is_measured = price(target, SearchMode.DEFAULT, meta.est_cost_per_call_usd[model])
            (measured if is_measured else declared).append(target)
            if meta.est_cost_per_call_search_disabled_usd is not None:
                search_disabled[target.key] = price(
                    target, SearchMode.SEARCH_DISABLED, meta.est_cost_per_call_search_disabled_usd[model])[0]
    timed = [st for st in stats if st.n >= MIN_STAT_SAMPLES]
    seconds = (
        sum(st.avg_latency_ms * st.n for st in timed) / sum(st.n for st in timed) / 1000
        if timed else AVG_SECONDS_PER_CALL
    )
    return UnitPrices(default=default, search_disabled=search_disabled, seconds_per_call=round(seconds, 1),
                      measured=measured, declared=declared)


class Estimate(FrozenModel):
    calls: int
    cost_usd: float
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
    for target in targets:
        cost = prices.default[target.key] * per_leg_calls
        legs += 1
        if dependency and target.key in prices.search_disabled:
            cost += prices.search_disabled[target.key] * per_leg_calls
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
        minutes=(PREP_SECONDS + sampling_s + judging_s) / 60,  # 核对网页不在里面(上限另写,见 VERIFY_MAX_MINUTES)
        per_target=per_target,
        judge_calls=judge_calls,
        judge_cost_usd=judge_cost,
        judge_upto=judge_limit is not None,
    )


def split_budget[K](budget_usd: float | None, weights: dict[K, float]) -> dict[K, float | None]:
    """费用上限按各条腿的预估费用比例分(用户 2026-11-03 定):预估 $6 时设 $10 全跑得完,总数仍不超上限。
    原来每条腿平分,贵的型号先停、便宜的份额用不完(小米真跑:$10 分四份,Claude 问了 31/72 次就停,另外三家各用不到 $1)。
    没设上限 → 每条腿都不限;预估全是 0(没法比)→ 平分。"""
    if budget_usd is None:
        return dict.fromkeys(weights)
    total = sum(weights.values())
    if total <= 0:
        return dict.fromkeys(weights, budget_usd / len(weights))
    return {key: budget_usd * weight / total for key, weight in weights.items()}
