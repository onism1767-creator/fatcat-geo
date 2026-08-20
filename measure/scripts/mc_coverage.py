"""蒙特卡洛覆盖率校验(12.1:不进 CI,手动跑)。

模拟已知 p 的伯努利流走完整序贯采样流程(真实调度器 + 真实停止规则),
统计名义 95% 区间对真值的实际覆盖率。序贯停止(optional stopping)下
Jeffreys 区间应保持在名义值附近——这是选它而非 Wald 的全部理由。

用法: .venv\\Scripts\\python scripts\\mc_coverage.py [--sims 400]
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import UTC, datetime
from pathlib import Path

import structlog

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(30))  # WARNING+

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters.base import CapabilityRegistry, SurfaceAdapter, SurfaceMeta  # noqa: E402
from engine.models import (  # noqa: E402
    Answer,
    AskRequest,
    InformationNeed,
    Outcome,
    PromptOrigin,
    PromptVariant,
    SurfaceRun,
)
from engine.scheduler import SamplingPolicy, SequentialSampler  # noqa: E402


class BernoulliAdapter(SurfaceAdapter):
    def __init__(self, p: float, rng: random.Random):
        self._p, self._rng = p, rng

    def meta(self) -> SurfaceMeta:
        return SurfaceMeta(provider="sim", surface="sim", models=["sim"],
                           default_model="sim", capabilities=CapabilityRegistry())

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
    parser.add_argument("--sims", type=int, default=400)
    args = parser.parse_args()

    need = InformationNeed(need_id="mc", statement="mc")
    variant = PromptVariant(variant_id="mc", need_id="mc", prompt_text="mc",
                            origin=PromptOrigin.ASSISTED)
    policy = SamplingPolicy()
    rng = random.Random(20260820)

    print(f"每个 p 值 {args.sims} 次模拟,策略:min_n={policy.min_n} max_n={policy.max_n} "
          f"width<={policy.ci_width_threshold}\n")
    print(f"{'p_true':>7} {'覆盖率':>7} {'均值N':>6} {'收敛率':>7}")
    for p_true in (0.05, 0.2, 0.5, 0.8):
        covered = converged = 0
        total_n = 0
        for _ in range(args.sims):
            sampler = SequentialSampler(NullStorage(), sleep=lambda s: None, rng=rng)
            result = sampler.run_plan(
                BernoulliAdapter(p_true, rng), need, variant,
                hit_fn=lambda run: run.raw_payload["hit"], policy=policy,
            )
            estimate = result.estimate
            total_n += estimate.n
            if estimate.ci_low <= p_true <= estimate.ci_high:
                covered += 1
            if result.stop_reason.value == "CI_CONVERGED":
                converged += 1
        print(f"{p_true:>7.2f} {covered / args.sims:>7.1%} {total_n / args.sims:>6.1f} "
              f"{converged / args.sims:>7.1%}")
    print("\n覆盖率应在名义 95% 附近(序贯停止下轻微偏离属预期,大幅低于 90% 即为异常)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
