"""端到端手动 smoke:架构书第 3 章走查的真实执行(L2 全流水线)。

配置装载(surfaces.yaml)→ 问法变体生成(LLM 辅助,走适配器)→
双通道 × 每个问法序贯采样(真实 API)→ 只追加落库(真实库)→
四个判定器 → 需求层聚合(概率 + 95% 区间 + 完整口径)。

策略刻意收紧控制成本(单次全链约 $0.15)。
前置:.env 有 PERPLEXITY_API_KEY;本地库在跑(scripts/pgdev.ps1 start)。
用法: .venv\\Scripts\\python scripts\\smoke_full_chain.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.aggregate import NeedSurfaceMetric, aggregate_need_surface  # noqa: E402
from engine.detectors.brand_mention import BrandMentionDetector  # noqa: E402
from engine.detectors.citation import CitationDetector  # noqa: E402
from engine.detectors.retrieval_rank import RetrievalRankDetector  # noqa: E402
from engine.detectors.runner import observe_runs  # noqa: E402
from engine.detectors.sentiment import SentimentDetector  # noqa: E402
from engine.envfile import load_env_file  # noqa: E402
from engine.loader import build_adapters  # noqa: E402
from engine.models import (  # noqa: E402
    BrandSpec,
    InformationNeed,
    NeedIntent,
    PromptOrigin,
    PromptVariant,
    SurfaceRun,
)
from engine.scheduler import SamplingPolicy, SequentialSampler  # noqa: E402
from engine.storage import Storage, default_dsn  # noqa: E402
from engine.variants import generate_variants  # noqa: E402

POLICY = SamplingPolicy(min_n=5, max_n=6, ci_width_threshold=0.35, budget_usd=0.10)

BRAND = BrandSpec(name="HubSpot", aliases=["hubspot"], domains=["hubspot.com"])

# 各通道用哪个判定器做收敛判据(hit 语义)
CHANNEL_HIT_DETECTORS = {
    "perplexity_agent": ("被引", CitationDetector(), lambda obs: obs.cited),
    "perplexity_search": ("检索在场", RetrievalRankDetector(), lambda obs: obs.present),
}


def print_metric(label: str, metric: NeedSurfaceMetric) -> None:
    acc = metric.accounting
    if metric.estimate:
        e = metric.estimate
        headline = f"{e.point:.0%} [95% 区间(Jeffreys): {e.ci_low:.0%}–{e.ci_high:.0%}]"
    else:
        headline = "无有效样本"
    reasons = ",".join(r.value for r in metric.stop_reasons)
    print(f"\n  {label}{headline}")
    print(f"    口径:surface={metric.surface} model={metric.model} "
          f"N_valid={acc.n_valid}/{acc.n_total}(跨 {len(metric.per_variant)} 个问法)")
    print(f"         stop_reasons=[{reasons}] 拒答率={acc.refusal_rate:.0%} "
          f"失败率={acc.failure_rate:.0%}{' [LOW_CONFIDENCE]' if acc.low_confidence else ''}")


def main() -> int:
    load_env_file()
    adapters = build_adapters(os.environ)
    if not adapters:
        print("没有可用 surface(检查 .env 的 key)", file=sys.stderr)
        return 1
    print(f"配置装载:{', '.join(adapters)}")

    storage = Storage(default_dsn())
    storage.ensure_schema()

    need = InformationNeed(statement="中小团队的 CRM 选型", intent=NeedIntent.DISCOVERY)
    storage.insert_information_need(need)

    user_variant = PromptVariant(need_id=need.need_id, prompt_text="中小团队用什么 CRM 好?",
                                 origin=PromptOrigin.USER)
    assisted = generate_variants(
        adapters["perplexity_agent"], need, count=2,
        ledger=lambda run: storage.insert_aux_call(job_id="__smoke_full_chain__", task="变体生成", run=run),
    )
    variants = [user_variant, assisted[0]]  # 控制成本:原话 + 1 个生成变体
    for variant in variants:
        storage.insert_prompt_variant(variant)
    print("问法:")
    for variant in variants:
        print(f"  [{variant.origin.value}] {variant.prompt_text}")

    sampler = SequentialSampler(storage)
    total_cost = 0.0
    all_runs: list[SurfaceRun] = []
    metrics: list[tuple[str, NeedSurfaceMetric]] = []

    for surface, (label, detector, extract) in CHANNEL_HIT_DETECTORS.items():
        adapter = adapters[surface]

        def hit_fn(run: SurfaceRun, detector=detector, extract=extract) -> bool | None:
            observation = detector.detect(run, BRAND)
            return None if observation is None else extract(observation)

        results = [
            sampler.run_plan(adapter, need, variant, hit_fn=hit_fn, policy=POLICY)
            for variant in variants
        ]
        metric = aggregate_need_surface(results, metric_label=detector.name)
        metrics.append((label, metric))
        total_cost += sum(r.total_cost_usd for r in results)
        for variant in variants:
            all_runs.extend(storage.runs_for_variant(variant.variant_id, surface=surface))

    observations = observe_runs(
        storage, all_runs,
        [BrandMentionDetector(), CitationDetector(), RetrievalRankDetector(), SentimentDetector()],
        BRAND,
    )

    print("\n════ 需求层指标(第 3 章步骤 7)════")
    print(f"需求:{need.statement} / 品牌:{BRAND.name}({', '.join(BRAND.domains)})")
    for label, metric in metrics:
        print_metric(f"{label}:", metric)
    print(f"\n判定观测写入 {observations} 条 / 落库 run {len(all_runs)} 条 / "
          f"采样成本 ${total_cost:.4f}")
    storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
