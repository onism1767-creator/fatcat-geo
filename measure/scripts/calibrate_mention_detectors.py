"""自动校准三种「提到你」判法(S9.4):读旧库 geo_tool 的真实回答(只读),跑 engine/calibration.py 的三件套,
结果写 config/calibration.yaml(确认页读)与 docs/校准/校准-日期.md。

    .venv\\Scripts\\python scripts/calibrate_mention_detectors.py                 # 只比规则 / 模糊,不花钱
    .venv\\Scripts\\python scripts/calibrate_mention_detectors.py --judge-sample 100
        # 再抽 100 条请两家裁判(花钱,先点头)

旧库是旧表结构(information_need / prompt_variant),这里自己读,不走 engine/storage.py;品牌卡从旧任务快照取。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.calibration import (  # noqa: E402
    DEFAULT_CALIBRATION_PATH,
    Sample,
    calibrate,
    render_markdown,
    save_calibration,
)
from engine.envfile import load_env_file  # noqa: E402
from engine.judge import JudgeConfig, MentionJudge, load_judges  # noqa: E402
from engine.loader import build_adapters  # noqa: E402
from engine.models import Answer, BrandSpec, Outcome, SearchMode, SearchTrace, SurfaceRun  # noqa: E402
from engine.storage import default_dsn  # noqa: E402

DOCS_DIR = Path(__file__).resolve().parents[1] / "docs" / "校准"


def load_old_samples(dsn: str) -> tuple[list[Sample], str]:
    """旧库:每个任务快照里的品牌卡 + 题目(need)清单;有效回答 = SUCCESS 且有正文。"""
    samples: list[Sample] = []
    parts: list[str] = []
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        for job_id, snapshot in conn.execute("SELECT job_id, snapshot FROM diagnosis_job ORDER BY created_at"):
            snap = snapshot if isinstance(snapshot, dict) else json.loads(snapshot)
            brand = BrandSpec(name=snap["brand"]["name"], aliases=snap["brand"].get("aliases", []),
                              domains=snap["brand"].get("domains", []))
            need_ids = [n["need_id"] for n in snap.get("needs", [])]
            if not need_ids:
                continue
            rows = conn.execute(
                "SELECT run_id, information_need_id, provider, surface, model, search_mode, locale, region, answer, "
                "run_timestamp, latency_ms, cost_usd FROM surface_run "
                "WHERE outcome = 'SUCCESS' AND answer IS NOT NULL AND information_need_id = ANY(%s) "
                "ORDER BY run_timestamp",
                (need_ids,),
            ).fetchall()
            count = 0
            for run_id, need_id, provider, surface, model, mode, locale, region, answer, ts, latency, cost in rows:
                text = (answer or {}).get("answer_text") or ""
                if not text.strip():
                    continue
                run = SurfaceRun(
                    run_id=run_id, prompt_id=need_id, run_index=1, provider=provider, surface=surface, model=model,
                    search_mode=SearchMode(mode), locale=locale, region=region, outcome=Outcome.SUCCESS,
                    answer=Answer(answer_text=text, citations_available=bool(answer.get("citations_available")),
                                  citations=[]),
                    search_trace=SearchTrace(), raw_payload={}, timestamp=ts, latency_ms=latency or 0,
                    cost_usd=cost or 0.0,
                )
                samples.append(Sample(run=run, brand=brand))
                count += 1
            parts.append(f"{brand.name}({job_id},{count} 条)")
    return samples, "旧库 geo_tool:" + "、".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description="自动校准三种提及判法(计数,非人工核对)")
    parser.add_argument("--dsn", default=default_dsn().rsplit("/", 1)[0] + "/geo_tool", help="旧库连接串(只读)")
    parser.add_argument("--judge-sample", type=int, default=0, help="抽几条真实回答请两家裁判(花钱;0 = 不请)")
    parser.add_argument("--seed", type=int, default=20261021)
    parser.add_argument("--out", default=str(DEFAULT_CALIBRATION_PATH))
    args = parser.parse_args()

    samples, source = load_old_samples(args.dsn)
    print(f"真实回答 {len(samples)} 条:{source}")
    judge_a = judge_b = None
    spent: list[float] = []
    adapters = {}
    if args.judge_sample > 0:
        load_env_file()
        adapters = build_adapters(os.environ)
        config = load_judges()
        swapped = JudgeConfig(primary=config.backup, backup=config.primary)
        first = MentionJudge(config, adapters, ledger=lambda r: spent.append(r.cost_usd), locale="zh-CN")
        second = MentionJudge(swapped, adapters, ledger=lambda r: spent.append(r.cost_usd), locale="zh-CN")

        def as_bool(judge: MentionJudge):
            def says(run, brand):
                verdict = judge.judge(run, brand)
                return None if verdict is None else verdict.mentioned
            return says

        judge_a, judge_b = as_bool(first), as_bool(second)
    try:
        report = calibrate(samples, source=source, judge_a=judge_a, judge_b=judge_b,
                           judge_sample=args.judge_sample, seed=args.seed)
    finally:
        for adapter in adapters.values():
            adapter.close()
    out = Path(args.out)
    save_calibration(report, out)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    md = DOCS_DIR / f"校准-{datetime.now(UTC):%Y-%m-%d}.md"
    md.write_text(render_markdown(report), encoding="utf-8")
    print(render_markdown(report))
    print(f"写入 {out} 与 {md};裁判调用 {len(spent)} 次,花费 ${sum(spent):.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
