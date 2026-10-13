"""端到端手动 smoke:架构书第 3 章走查的真实执行(L2 全流水线;v0.4.26 S2 方法)。

配置装载(surfaces.yaml)→ 两道题原样各问 2 次(真实 API)→ 只追加落库(真实库)→
规则判定器 → 题型层聚合(计数 + 比例,逐题并列,题目覆盖率)。

策略刻意收紧控制成本(单次全链约 $0.05)。
前置:.env 有 PERPLEXITY_API_KEY;本地库在跑(scripts/pgdev.ps1 start)。
用法: .venv\\Scripts\\python scripts\\smoke_full_chain.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.aggregate import ClassSurfaceMetric, measure_target  # noqa: E402
from engine.envfile import load_env_file  # noqa: E402
from engine.loader import build_adapters  # noqa: E402
from engine.models import BrandSpec, Prompt, PromptIntent, PromptOrigin, Target  # noqa: E402
from engine.observe import ObserveContext, observe, persist_observations  # noqa: E402
from engine.scheduler import RepeatSampler, SamplingPolicy  # noqa: E402
from engine.storage import Storage, default_dsn  # noqa: E402

POLICY = SamplingPolicy(runs_per_prompt=2, budget_usd=0.10)
SURFACE = "perplexity_agent"
BRAND = BrandSpec(name="HubSpot", aliases=["hubspot"], domains=["hubspot.com"])
PROMPTS = [
    Prompt(text="中小团队用什么 CRM 好?", intent=PromptIntent.CATEGORY_DISCOVERY, origin=PromptOrigin.USER,
           locale="zh-CN"),
    Prompt(text="十人团队怎么选客户管理系统?", intent=PromptIntent.CATEGORY_DISCOVERY, origin=PromptOrigin.USER,
           locale="zh-CN"),
]


def print_metric(label: str, metric: ClassSurfaceMetric) -> None:
    acc = metric.accounting
    headline = f"{metric.rate.k}/{metric.rate.n} = {metric.rate.share:.0%}" if metric.rate else "无有效样本"
    per_prompt = ", ".join(f"题{i} {r.k}/{r.n}" if r else f"题{i} 无" for i, r in enumerate(metric.per_prompt, 1))
    coverage = (f"{metric.prompt_coverage.k}/{metric.prompt_coverage.n} 道题至少命中一次"
                if metric.prompt_coverage else "—")
    print(f"\n  {label}{headline}(逐题:{per_prompt};题目覆盖率 {coverage})")
    print(f"    口径:surface={metric.surface} model={metric.model} 题数={len(metric.per_prompt)} "
          f"N_valid={acc.n_valid}/{acc.n_total} stop_reasons=[{', '.join(r.value for r in metric.stop_reasons)}] "
          f"拒答率={acc.refusal_rate:.0%} 失败率={acc.failure_rate:.0%}"
          f"{' [LOW_CONFIDENCE]' if acc.low_confidence else ''}")


def main() -> int:
    load_env_file()
    adapters = build_adapters(os.environ)
    if SURFACE not in adapters:
        print(f"{SURFACE} 不可用(检查 .env 的 key)", file=sys.stderr)
        return 1
    adapter = adapters[SURFACE]
    meta = adapter.meta()
    target = Target(surface=SURFACE, model=meta.default_model)

    storage = Storage(default_dsn())
    storage.ensure_schema()
    for prompt in PROMPTS:
        storage.insert_prompt(prompt)
        print(f"题:{prompt.text}")

    result = RepeatSampler(storage).run_plan(adapter, PROMPTS, policy=POLICY, model=target.model)
    runs = [r for p in PROMPTS for r in storage.runs_for_prompt(p.prompt_id, surface=SURFACE, model=target.model)]
    observations = [observe(run, BRAND, ObserveContext(competitors=[])) for run in runs]
    persist_observations(storage, runs, BRAND, ObserveContext(competitors=[]))
    measurement = measure_target(
        target, meta.provider, PromptIntent.CATEGORY_DISCOVERY, observations, [p.prompt_id for p in PROMPTS],
        stop_reason=result.stop_reason, competitor_names=[], exposes_retrieval=meta.capabilities.supports_query_trace,
    )

    print("\n════ 题型层指标(第 3 章步骤 7)════")
    print(f"品牌:{BRAND.name}({', '.join(BRAND.domains)})")
    print_metric("被引:", measurement.citation)
    print_metric("提及:", measurement.mention)
    print(f"\n落库 run {len(runs)} 条 / 采样成本 ${result.total_cost_usd:.4f} / 停止原因 {result.stop_reason.value}")
    storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
