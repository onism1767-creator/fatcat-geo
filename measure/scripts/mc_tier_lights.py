"""档位亮灯把握的蒙特卡洛(12.1:不进 CI,手动跑)。确认页「两个档位差在哪」对照表的数字来源。

已知真实命中率 p 的伯努利流走完整流程:每个问题若干种说法 → 真实调度器 + 真实停止规则 →
真实合并(aggregate_need_surface)→ 真实亮灯规则(interval_status + DiagnosisThresholds 默认值),
统计各档位亮红 / 黄 / 绿的比例,以及把灯亮反(真实经常却亮红,或真实几乎不却亮绿)的比例。

前提:同一问题的各种说法真实命中率相同——问法层的不确定度不在模拟内(v0.4.13 条目 2),
表下注释必须如实交代这一点。

用法: .venv\\Scripts\\python scripts\\mc_tier_lights.py [--sims 2000] [--variants 3]
"""

from __future__ import annotations

import argparse
import random
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import structlog

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(30))  # WARNING+

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters.base import CapabilityRegistry, SurfaceAdapter, SurfaceMeta  # noqa: E402
from diagnosis.findings import DiagnosisThresholds, interval_status  # noqa: E402
from engine.models import (  # noqa: E402
    Answer,
    AskRequest,
    InformationNeed,
    NeedIntent,
    Outcome,
    PromptOrigin,
    PromptVariant,
    SurfaceRun,
)
from engine.scheduler import SequentialSampler, aggregate_need_surface  # noqa: E402
from report.jobs import TIERS  # noqa: E402


class BernoulliAdapter(SurfaceAdapter):
    def __init__(self, p: float, rng: random.Random):
        self._p, self._rng = p, rng

    def meta(self) -> SurfaceMeta:
        return SurfaceMeta(display_name="sim", provider="sim", surface="sim", models=["sim"],
                           default_model="sim", capabilities=CapabilityRegistry(),
                           est_cost_per_call_usd={"sim": 0.005})

    def ask(self, request: AskRequest) -> SurfaceRun:
        return SurfaceRun(
            run_id=request.run_id,
            information_need_id=request.information_need_id,
            prompt_variant_id=request.prompt_variant_id,
            provider="sim", surface="sim", model="sim",
            search_mode=request.search_mode, locale=request.locale,
            outcome=Outcome.SUCCESS,
            answer=Answer(answer_text="sim", citations_available=False),
            raw_payload={"hit": self._rng.random() < self._p},
            timestamp=datetime.now(UTC), latency_ms=0, cost_usd=0.0,
        )

    def healthcheck(self) -> bool:
        return True


class NullStorage:
    def insert_surface_run(self, run: SurfaceRun) -> None:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sims", type=int, default=2000)
    parser.add_argument("--variants", type=int, default=3)
    args = parser.parse_args()

    thresholds = DiagnosisThresholds()
    fail_high, pass_low = thresholds.mention_fail_ci_high, thresholds.mention_pass_ci_low
    need = InformationNeed(need_id="mc", statement="mc", intent=NeedIntent.DISCOVERY)
    variants = [
        PromptVariant(variant_id=f"mc-{i}", need_id="mc", prompt_text="mc", origin=PromptOrigin.ASSISTED)
        for i in range(args.variants)
    ]
    rng = random.Random(20260921)

    print(f"每格 {args.sims} 回模拟,每个问题 {args.variants} 种说法;"
          f"亮灯规则:区间上界 <= {fail_high} 红 / 区间下界 >= {pass_low} 绿 / 其余黄")
    for tier_id in ("quick", "standard"):
        policy = TIERS[tier_id].policy
        print(f"\n== {tier_id}: min_n={policy.min_n} max_n={policy.max_n} width<={policy.ci_width_threshold}")
        print(f"{'p_true':>7} {'红':>7} {'黄':>7} {'绿':>7} {'亮反':>7} {'均值N':>6}")
        for p_true in (0.0, 0.02, 0.05, 0.10, 0.20, 0.35, 0.50, 0.65, 0.80, 0.95, 1.0):
            lights: Counter[str] = Counter()
            total_n = 0
            for _ in range(args.sims):
                sampler = SequentialSampler(NullStorage(), sleep=lambda s: None, rng=rng)
                metric = aggregate_need_surface(
                    [
                        sampler.run_plan(BernoulliAdapter(p_true, rng), need, variant,
                                         hit_fn=lambda run: run.raw_payload["hit"], policy=policy)
                        for variant in variants
                    ],
                    metric_label="sim",
                )
                assert metric.estimate is not None
                status, _ = interval_status(metric.estimate, low_confidence=metric.accounting.low_confidence,
                                            fail_ci_high=fail_high, pass_ci_low=pass_low)
                lights[status.value] += 1
                total_n += metric.estimate.n
            opposite = lights["PASS"] if p_true <= fail_high else lights["FAIL"] if p_true >= pass_low else 0
            print(f"{p_true:>7.2f} {lights['FAIL'] / args.sims:>7.1%} {lights['WEAK'] / args.sims:>7.1%} "
                  f"{lights['PASS'] / args.sims:>7.1%} {opposite / args.sims:>7.2%} {total_n / args.sims:>6.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
