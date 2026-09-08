"""手动 smoke:对 Gemini 接地通道打真实请求(开卷 + 闭卷各一发)。

12.3:真实 API 调用只存在于手动 smoke 脚本与 golden payload 更新流程,
本脚本就是前者,永不进 CI。

用法:
    .venv\\Scripts\\python scripts\\smoke_gemini.py [--capture DIR]

预期成本:healthcheck 近零 + 开卷一发(上界约 $0.06)+ 闭卷一发(约 $0.015);
若在每月 5000 条免费检索额度内则更低。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters.base import AdapterError  # noqa: E402
from adapters.gemini_grounding import GeminiGroundingAdapter  # noqa: E402
from engine.envfile import load_env_file  # noqa: E402
from engine.models import NOT_OBSERVABLE, AskRequest, SearchMode  # noqa: E402

SMOKE_PROMPT = "中小团队用什么 CRM 好?"


def make_request(prompt: str, mode: SearchMode) -> AskRequest:
    return AskRequest(
        run_id=f"smoke-{uuid.uuid4().hex[:8]}",
        information_need_id="smoke-need",
        prompt_variant_id="smoke-variant",
        prompt_text=prompt,
        search_mode=mode,
        locale="zh-CN",
        timeout_s=180.0,
    )


def summarize(label: str, run) -> None:
    print(f"\n── {label} ──")
    print(f"  outcome={run.outcome}  search_mode={run.search_mode}  "
          f"latency={run.latency_ms}ms  cost=${run.cost_usd:.4f}")
    trace = run.search_trace
    print(f"  search_triggered={trace.search_triggered}")
    if trace.generated_queries != NOT_OBSERVABLE:
        print(f"  模型自拟检索词 {len(trace.generated_queries)} 条:")
        for query in trace.generated_queries:
            print(f"    「{query}」")
    if trace.retrieved_sources == NOT_OBSERVABLE:
        print("  候选集:不暴露(NOT_OBSERVABLE,本 surface 口径)")
    else:
        print(f"  候选集:{len(trace.retrieved_sources)} 条")
    if run.answer is not None:
        print(f"  回答(前 80 字):{run.answer.answer_text[:80]!r}")
        print(f"  citations_available={run.answer.citations_available},"
              f"引用 {len(run.answer.citations)} 条(域名取自 title):")
        for c in run.answer.citations[:5]:
            print(f"    [{c.position}] {c.domain}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture", type=Path, default=None, help="原始返回体落盘目录")
    args = parser.parse_args()

    load_env_file()
    api_key = os.environ.get("GOOGLE_API_KEY", "")
    if not api_key:
        print("GOOGLE_API_KEY 未设置(.env 或环境变量)", file=sys.stderr)
        return 1
    print(f"使用 key:****{api_key[-4:]}")

    adapter = GeminiGroundingAdapter(api_key=api_key)
    exit_code = 0
    try:
        healthy = adapter.healthcheck()
        print(f"gemini_grounding: healthcheck={'OK' if healthy else 'FAILED'}")
        legs = {
            "开卷(默认,接地检索)": SearchMode.DEFAULT,
            "闭卷(search_disabled)": SearchMode.SEARCH_DISABLED,
        }
        for label, mode in legs.items():
            run = adapter.ask(make_request(SMOKE_PROMPT, mode))
            summarize(label, run)
            if args.capture:
                args.capture.mkdir(parents=True, exist_ok=True)
                out = args.capture / f"gemini_grounding.{mode.value}.json"
                out.write_text(
                    json.dumps(run.raw_payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(f"  原始返回已写入 {out}")
    except AdapterError as exc:
        exit_code = 1
        status = getattr(exc, "http_status", None)
        print(f"gemini_grounding: 契约异常 {type(exc).__name__} http_status={status}: {exc}")
    finally:
        adapter.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
