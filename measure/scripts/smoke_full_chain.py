"""端到端手动 smoke:需求 → 序贯采样(真实 API)→ 只追加落库(真实库)
→ 判定 → 指标(概率 + 95% 区间 + 完整口径)。

这是第 10 章"打通全链"的验证脚本。默认用一个公认品牌做正例观测,
策略刻意收紧(max_n=8、宽阈值)控制成本,单次全链 < $0.06。

前置:.env 有 PERPLEXITY_API_KEY;本地库在跑(scripts/pgdev.ps1 start)。
用法: .venv\\Scripts\\python scripts\\smoke_full_chain.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters.perplexity_agent import PerplexityAgentAdapter  # noqa: E402
from engine.detectors.brand_mention import BrandMentionDetector  # noqa: E402
from engine.detectors.citation import CitationDetector  # noqa: E402
from engine.detectors.runner import observe_runs  # noqa: E402
from engine.models import (  # noqa: E402
    BrandSpec,
    InformationNeed,
    PromptOrigin,
    PromptVariant,
    SurfaceRun,
)
from engine.scheduler import SamplingPolicy, SequentialSampler  # noqa: E402
from engine.storage import Storage  # noqa: E402
from scripts.smoke_perplexity import load_env_file  # noqa: E402

DEFAULT_DB = "postgresql://geo@localhost:5433/geo_tool"


def main() -> int:
    load_env_file()
    api_key = os.environ.get("PERPLEXITY_API_KEY", "")
    if not api_key:
        print("PERPLEXITY_API_KEY 未设置", file=sys.stderr)
        return 1

    storage = Storage(os.environ.get("GEO_DATABASE_URL", DEFAULT_DB))
    storage.ensure_schema()

    need = InformationNeed(statement="中小团队的 CRM 选型")
    variant = PromptVariant(need_id=need.need_id, prompt_text="中小团队用什么 CRM 好?",
                            origin=PromptOrigin.USER)
    storage.insert_information_need(need)
    storage.insert_prompt_variant(variant)

    brand = BrandSpec(name="HubSpot", aliases=["hubspot"], domains=["hubspot.com"])
    citation_detector = CitationDetector()

    def hit_fn(run: SurfaceRun) -> bool | None:
        observation = citation_detector.detect(run, brand)
        return None if observation is None else observation.cited

    policy = SamplingPolicy(min_n=5, max_n=8, ci_width_threshold=0.35, budget_usd=0.15)
    sampler = SequentialSampler(storage)
    result = sampler.run_plan(
        PerplexityAgentAdapter(api_key=api_key), need, variant,
        hit_fn=hit_fn, policy=policy,
    )

    runs = storage.runs_for_variant(variant.variant_id, surface=result.surface)
    observations = observe_runs(
        storage, runs, [BrandMentionDetector(), CitationDetector()], brand
    )

    acc = result.accounting
    print("\n════ 全链 smoke 结果 ════")
    print(f"需求:{need.statement} / 问法:{variant.prompt_text}")
    print(f"品牌:{brand.name}(域名 {', '.join(brand.domains)})")
    if result.estimate:
        e = result.estimate
        print(f"\n被引概率:{e.point:.0%} [95% 区间(Jeffreys): {e.ci_low:.0%}–{e.ci_high:.0%}]")
    else:
        print("\n被引概率:无有效样本")
    print(f"口径:surface={result.surface} model={result.model} "
          f"N_valid={acc.n_valid}/{acc.n_total} stop_reason={result.stop_reason.value}")
    print(f"      拒答率={acc.refusal_rate:.0%} 失败率={acc.failure_rate:.0%}"
          f"{' [LOW_CONFIDENCE]' if acc.low_confidence else ''}")
    print(f"成本:${result.total_cost_usd:.4f} / 判定观测写入 {observations} 条")
    print(f"落库 run:{len(result.run_ids)} 条(只追加,含全部终态)")
    storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
