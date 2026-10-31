"""BR3:问几个难品牌,拿真实回答给校准当底本(题目在 config/calibration_real.yaml)。

    .venv\\Scripts\\python -X utf8 scripts/collect_hard_answers.py        # 只列题目和预估,不调用 AI、不花钱
    .venv\\Scripts\\python -X utf8 scripts/collect_hard_answers.py --go   # 真问(花钱,先点头)

每家 AI 问它的消费端默认型号,每题 1 次:同一题问几次是一簇,题多比次数多有用。回答只追加进 geo_toast,不进任何一单;
再跑一遍只补没问到的题,已问过的不重问。每家的花费上限 = 声明价 × 1.1,按语言分开算,撞上就停这一家这种语言。
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


def declared_cost(adapter: SurfaceAdapter, n_prompts: int) -> float:
    meta = adapter.meta()
    return meta.est_cost_per_call_usd[meta.consumer_default_model] * n_prompts * RUNS_PER_PROMPT


def by_locale(prompts: list[Prompt]) -> dict[str, list[Prompt]]:
    """采样计划一次只带一种语言,回答的语言要记对(判定器按它定口径)。"""
    groups: dict[str, list[Prompt]] = {}
    for prompt in prompts:
        groups.setdefault(prompt.locale, []).append(prompt)
    return groups


def collect(
    adapters: Mapping[str, SurfaceAdapter], prompts: list[Prompt], dsn: str,
) -> tuple[list[SamplingResult], list[AccountProblem]]:
    """四家同时问(各是各的平台,限流互不相干),一家内部逐题问。一家账户出问题只停这一家,别家问完的照常留下。"""
    with Storage(dsn) as storage:
        storage.ensure_schema()
        for prompt in prompts:
            storage.insert_prompt(prompt)

    def ask_one_surface(adapter: SurfaceAdapter) -> tuple[list[SamplingResult], list[AccountProblem]]:
        meta = adapter.meta()
        model = meta.consumer_default_model
        results: list[SamplingResult] = []
        with Storage(dsn) as storage:  # psycopg 连接非线程安全:每家独立连接
            sampler = RepeatSampler(storage)
            for locale, group in by_locale(prompts).items():
                prior = {p.prompt_id: storage.runs_for_prompt(p.prompt_id, surface=meta.surface, model=model)
                         for p in group}
                policy = SamplingPolicy(runs_per_prompt=RUNS_PER_PROMPT,
                                        budget_usd=declared_cost(adapter, len(group)) * CAP_MARGIN)
                try:
                    results.append(sampler.run_plan(adapter, group, policy=policy, model=model, locale=locale,
                                                    prior_runs=prior))
                except AccountProblem as exc:
                    return results, [exc]
        return results, []

    with ThreadPoolExecutor(max_workers=len(adapters)) as pool:
        outcomes = list(pool.map(ask_one_surface, adapters.values()))
    return [r for results, _ in outcomes for r in results], [p for _, problems in outcomes for p in problems]


def print_plan(adapters: Mapping[str, SurfaceAdapter], prompts: list[Prompt]) -> None:
    brands = load_real_brands()
    for brand in brands:
        print(f"\n{brand.name}({brand.locale},别名:{'、'.join(brand.aliases) or '无'})")
        for label, questions in (("点名", brand.naming), ("反例", brand.counter), ("不点名", brand.open)):
            for question in questions:
                print(f"  [{label}] {question}")
    answers = len(prompts) * RUNS_PER_PROMPT * len(adapters)
    print(f"\n{len(brands)} 个品牌 {len(prompts)} 道题,{len(adapters)} 家 AI 各问 {RUNS_PER_PROMPT} 次,"
          f"共 {answers} 条回答")
    for name, adapter in adapters.items():
        meta = adapter.meta()
        cost = declared_cost(adapter, len(prompts))
        print(f"  {name} / {meta.consumer_default_model}:声明价 ${cost:.2f},上限 ${cost * CAP_MARGIN:.2f}")
    declared = sum(declared_cost(adapter, len(prompts)) for adapter in adapters.values())
    print(f"合计:声明价 ${declared:.2f},上限 ${declared * CAP_MARGIN:.2f};"
          f"四家同时问,粗估 {len(prompts) * SECONDS_PER_CALL / 60:.0f} 分钟")


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
    args = parser.parse_args()

    load_env_file()
    adapters = build_adapters(os.environ)
    prompts = [prompt for brand in load_real_brands() for prompt in prompts_of(brand)]
    try:
        print_plan(adapters, prompts)
        if not args.go:
            return 0
        missing = {entry.name for entry in load_surface_registry() if entry.enabled} - set(adapters)
        if missing:
            print(f"少了这几家的 key,不开跑:{sorted(missing)}", file=sys.stderr)
            return 1
        results, problems = collect(adapters, prompts, default_dsn())
    finally:
        for adapter in adapters.values():
            adapter.close()
    return 0 if print_results(results, problems) else 1


if __name__ == "__main__":
    raise SystemExit(main())
