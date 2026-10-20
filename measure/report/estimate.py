"""L4 成本与时长预估(16.4;v0.4.17 起按库里实测;v0.4.19 按型号;v0.4.26 S2 次数固定)——全项目唯一一份预估公式。

确认页每次勾选变化都向服务端要一次结果(POST /jobs/{id}/estimate),页面脚本只负责显示,不再自带公式。
"""

from __future__ import annotations

from adapters.base import SurfaceAdapter
from engine.models import FrozenModel, SearchMode, Target
from engine.storage import CallStat
from report.jobs import PLAN_CONCURRENCY

# 兜底值(v0.4.17):库里没有足量历史时用;有历史即按实测均值(香奈儿首单加权均值 20.2 s)
AVG_SECONDS_PER_CALL = 20.0
JUDGE_SECONDS_PER_CALL = 4.0  # 裁判不联网、输出短,一条约几秒(S9.3;有实测后改读实测)
MIN_STAT_SAMPLES = 10  # 少于这么多次历史调用不算数,仍用声明单价(v0.4.17)


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
    judge_calls: int  # 高档:AI 裁判要判几条回答(= 提问次数),其余档 0
    judge_cost_usd: float


def estimate_job(
    *,
    n_prompts: int,
    runs_per_prompt: int,
    targets: list[Target],
    dependency: bool,
    prices: UnitPrices,
    judge_cost_per_call: float | None = None,
) -> Estimate:
    """每条腿(一个对象 × 一种检索模式)的调用数 = 题数 × 每题次数(次数随档位,S9.1);
    启用检索依赖对照时,支持闭卷的对象多一条闭卷腿,按闭卷单价另计;
    高档(judge_cost_per_call 非空)每条回答再请 AI 裁判判一次,按裁判单价另计(S9.3)。"""
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
    judge_cost = judge_calls * (judge_cost_per_call or 0.0)
    return Estimate(
        calls=calls,
        cost_usd=sum(cost for _, cost in per_target) + judge_cost,
        minutes=(calls * prices.seconds_per_call + judge_calls * JUDGE_SECONDS_PER_CALL) / 60 / PLAN_CONCURRENCY,
        per_target=per_target,
        judge_calls=judge_calls,
        judge_cost_usd=judge_cost,
    )
