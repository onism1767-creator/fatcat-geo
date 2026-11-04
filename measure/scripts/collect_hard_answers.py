"""BR3:问几个难品牌,拿真实回答给校准当底本(题目在 config/calibration_real.yaml)。

    .venv\\Scripts\\python -X utf8 scripts/collect_hard_answers.py        # 只列题目和预估,不调用 AI、不花钱
    .venv\\Scripts\\python -X utf8 scripts/collect_hard_answers.py --go   # 真问(花钱,先点头)

每家 AI 问它的消费端默认型号,每题 1 次:同一题问几次是一簇,题多比次数多有用。回答只追加进 geo_toast,不进任何一单;
再跑一遍只补没问到的题,已问过的不重问。每个型号的花费上限 = 声明价 × 1.1,按语言分开算,撞上就停这个型号这种语言。

成本调研①(2026-11-03):`--models` 指定型号就只问这些(给没实测过的型号攒实测用量,确认页满 10 次就按实测估),
`--locale zh-CN` 只问这种语言的题。回答照样带计费用量进 call_usage。

    .venv\\Scripts\\python -X utf8 scripts/collect_hard_answers.py --locale zh-CN --models gpt-6-astra claude-fable-5-1
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.base import SurfaceAdapter
from engine.envfile import load_env_file
from engine.hard_cases import load_real_brands, prompts_of
from engine.loader import build_adapters, load_surface_registry
from engine.models import Prompt, StopReason
from engine.scheduler import AccountProblem, RepeatSampler, SamplingPolicy, SamplingResult
from engine.storage import Storage, default_dsn

CAP_MARGIN = 1.1  # 声明价一般比实测高,这点余量只防个别题特别贵
RUNS_PER_PROMPT = 1
SECONDS_PER_CALL = 20.0  # 和 report/estimate.py 的兜底值同一个粗估


def models_to_ask(adapter: SurfaceAdapter, only: list[str] | None) -> list[str]:
    """默认问消费端默认型号(BR3);--models 给了就只问清单里属于这家的那些(成本调研①)。"""
    meta = adapter.meta()
    if only is None:
        return [meta.consumer_default_model]
    return [model for model in meta.models if model in only]


def declared_cost(adapter: SurfaceAdapter, model: str, n_prompts: int) -> float:
    return adapter.meta().est_cost_per_call_usd[model] * n_prompts * RUNS_PER_PROMPT


def by_locale(prompts: list[Prompt]) -> dict[str, list[Prompt]]:
    """采样计划一次只带一种语言,回答的语言要记对(判定器按它定口径)。"""
    groups: dict[str, list[Prompt]] = {}
    for prompt in prompts:
        groups.setdefault(prompt.locale, []).append(prompt)
    return groups


def collect(
    adapters: Mapping[str, SurfaceAdapter], prompts: list[Prompt], dsn: str, *, only: list[str] | None = None,
) -> tuple[list[SamplingResult], list[AccountProblem]]:
    """四家同时问(各是各的平台,限流互不相干),一家内部逐型号、逐题问。一家账户出问题只停这一家,别家问完的照常留下。"""
    with Storage(dsn) as storage:
        storage.ensure_schema()
        for prompt in prompts:
            storage.insert_prompt(prompt)

    def ask_one_surface(adapter: SurfaceAdapter) -> tuple[list[SamplingResult], list[AccountProblem]]:
        meta = adapter.meta()
        results: list[SamplingResult] = []
        with Storage(dsn) as storage:  # psycopg 连接非线程安全:每家独立连接
            sampler = RepeatSampler(storage)
            for model in models_to_ask(adapter, only):
                for locale, group in by_locale(prompts).items():
                    prior = {p.prompt_id: storage.runs_for_prompt(p.prompt_id, surface=meta.surface, model=model)
                             for p in group}
                    policy = SamplingPolicy(runs_per_prompt=RUNS_PER_PROMPT,
                                            budget_usd=declared_cost(adapter, model, len(group)) * CAP_MARGIN)
                    try:
                        results.append(sampler.run_plan(adapter, group, policy=policy, model=model, locale=locale,
                                                        prior_runs=prior))
                    except AccountProblem as exc:
                        return results, [exc]
        return results, []

    with ThreadPoolExecutor(max_workers=len(adapters)) as pool:
        outcomes = list(pool.map(ask_one_surface, adapters.values()))
    return [r for results, _ in outcomes for r in results], [p for _, problems in outcomes for p in problems]


def print_plan(adapters: Mapping[str, SurfaceAdapter], prompts: list[Prompt], only: list[str] | None) -> None:
    brands = [brand for brand in load_real_brands() if any(p.locale == brand.locale for p in prompts)]
    for brand in brands:
        print(f"\n{brand.name}({brand.locale},别名:{'、'.join(brand.aliases) or '无'})")
        for label, questions in (("点名", brand.naming), ("反例", brand.counter), ("不点名", brand.open)):
            for question in questions:
                print(f"  [{label}] {question}")
    plan = [(name, model) for name, adapter in adapters.items() for model in models_to_ask(adapter, only)]
    answers = len(prompts) * RUNS_PER_PROMPT * len(plan)
    print(f"\n{len(brands)} 个品牌 {len(prompts)} 道题,{len(plan)} 个型号各问 {RUNS_PER_PROMPT} 次,共 {answers} 条回答")
    declared = 0.0
    for name, model in plan:
        cost = declared_cost(adapters[name], model, len(prompts))
        declared += cost
        print(f"  {name} / {model}:声明价 ${cost:.2f},上限 ${cost * CAP_MARGIN:.2f}")
    longest = max((len(models_to_ask(adapter, only)) for adapter in adapters.values()), default=0)
    print(f"合计:声明价 ${declared:.2f},上限 ${declared * CAP_MARGIN:.2f};"
          f"各家同时问、一家内部逐型号,粗估 {longest * len(prompts) * SECONDS_PER_CALL / 60:.0f} 分钟")


def print_results(results: list[SamplingResult], problems: list[AccountProblem]) -> bool:
    """每家问得怎么样;全问完了才返回 True。"""
    for r in results:
        failures = f",没拿到回答:{r.failure_counts}" if r.failure_counts else ""
        print(f"{r.surface} / {r.model}:{r.n_attempts} 次尝试,有效 {r.accounting.n_valid} 条,"
              f"花费 ${r.total_cost_usd:.4f},{r.stop_reason.value}{failures}")
    for problem in problems:
        print(f"账户问题,这家停了,补好再跑一遍:{problem}")
    print(f"合计花费 ${sum(r.total_cost_usd for r in results):.4f}")
    return not problems and all(r.stop_reason is StopReason.COMPLETED for r in results)


def main() -> int:
    parser = argparse.ArgumentParser(description="问几个难品牌,拿真实回答给校准当底本")
    parser.add_argument("--go", action="store_true", help="真问(花钱);不写只列题目和预估")
    parser.add_argument("--models", nargs="*", default=None, help="只问这些型号(默认每家问消费端默认型号)")
    parser.add_argument("--locale", default=None, help="只问这种语言的题,如 zh-CN(默认全部)")
    args = parser.parse_args()

    load_env_file()
    adapters = build_adapters(os.environ)
    prompts = [prompt for brand in load_real_brands() for prompt in prompts_of(brand)
               if args.locale is None or brand.locale == args.locale]
    if args.models is not None:
        known = {model for adapter in adapters.values() for model in adapter.meta().models}
        unknown = sorted(set(args.models) - known)
        if unknown:
            print(f"这些型号不在已加载的仪器清单里:{unknown}", file=sys.stderr)
            return 1
        adapters = {name: adapter for name, adapter in adapters.items() if models_to_ask(adapter, args.models)}
    try:
        print_plan(adapters, prompts, args.models)
        if not args.go:
            return 0
        missing = {entry.name for entry in load_surface_registry() if entry.enabled} - set(adapters)
        if missing and args.models is None:
            print(f"少了这几家的 key,不开跑:{sorted(missing)}", file=sys.stderr)
            return 1
        results, problems = collect(adapters, prompts, default_dsn(), only=args.models)
    finally:
        for adapter in adapters.values():
            adapter.close()
    return 0 if print_results(results, problems) else 1


if __name__ == "__main__":
    raise SystemExit(main())
