"""手动 smoke:对 Grok web_search 通道打真实请求(每个被测型号开卷一发 + 默认型号闭卷一发 + 辅助型号闭卷一发)。

12.3:真实 API 调用只存在于手动 smoke 脚本与 golden payload 更新流程,
本脚本就是前者,永不进 CI。

用法:
    .venv\\Scripts\\python scripts\\smoke_grok.py [--capture DIR]

--capture DIR 时把原始返回体写入该目录(golden payload 更新流程的输入,
入库前须人工 diff 审查)。key 从 .env / 环境变量读取,输出中一律脱敏。

预期成本:healthcheck 近零 + 开卷每个型号一发(2026-11-17 实测 grok-4.5 一发 $0.14)+ 闭卷两发(各约 $0.01–0.03),
合计上界约 $0.5。
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
from adapters.grok_web_search import HELPER_MODEL, GrokWebSearchAdapter  # noqa: E402
from engine.envfile import load_env_file, utf8_output  # noqa: E402
from engine.models import AskRequest, SearchMode, SurfaceRun  # noqa: E402

SMOKE_PROMPT = "中小团队用什么 CRM 好?"


def make_request(model: str, mode: SearchMode) -> AskRequest:
    return AskRequest(
        run_id=f"smoke-{uuid.uuid4().hex[:8]}",
        prompt_id="smoke-prompt",
        run_index=1,
        prompt_text=SMOKE_PROMPT,
        search_mode=mode,
        model=model,
        locale="zh-CN",
        timeout_s=300.0,  # 推理型检索可能较慢
    )


def summarize(label: str, run: SurfaceRun) -> None:
    raw = run.raw_payload if isinstance(run.raw_payload, dict) else {}
    usage = raw.get("usage") or {}
    print(f"\n── {label} ──")
    print(f"  outcome={run.outcome}  latency={run.latency_ms}ms  cost=${run.cost_usd:.4f}  "
          f"status={raw.get('status')}  incomplete_details={raw.get('incomplete_details')}")
    print(f"  输出 token {usage.get('output_tokens')}(其中推理 "
          f"{(usage.get('output_tokens_details') or {}).get('reasoning_tokens')}),"
          f"输入 token {usage.get('input_tokens')}")
    trace = run.search_trace
    print(f"  search_triggered={trace.search_triggered}  opened_sources={trace.opened_sources}")
    print(f"  模型自拟检索词 {len(trace.generated_queries)} 条:")
    for query in trace.generated_queries:
        print(f"    「{query}」")
    print(f"  检索候选 {len(trace.retrieved_sources)} 条,前 5:")
    for s in list(trace.retrieved_sources)[:5]:
        print(f"    #{s.rank}  {s.domain}")  # type: ignore[union-attr]
    if run.answer is not None:
        print(f"  回答 {len(run.answer.answer_text)} 字(前 80 字):{run.answer.answer_text[:80]!r}")
        print(f"  citations_available={run.answer.citations_available},引用 {len(run.answer.citations)} 条:")
        for c in run.answer.citations[:5]:
            print(f"    [{c.position}] {c.domain}")
    else:
        print("  没有正文")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture", type=Path, default=None, help="原始返回体落盘目录")
    args = parser.parse_args()

    utf8_output()
    load_env_file()
    api_key = os.environ.get("XAI_API_KEY", "")
    if not api_key:
        print("XAI_API_KEY 未设置(.env 或环境变量)", file=sys.stderr)
        return 1
    print(f"使用 key:****{api_key[-4:]}")

    adapter = GrokWebSearchAdapter(api_key=api_key)
    meta = adapter.meta()
    exit_code = 0
    total = 0.0
    try:
        healthy = adapter.healthcheck()
        print(f"grok_web_search: healthcheck={'OK' if healthy else 'FAILED'}")
        legs = [(model, SearchMode.DEFAULT) for model in meta.models]
        legs += [(meta.default_model, SearchMode.SEARCH_DISABLED), (HELPER_MODEL, SearchMode.SEARCH_DISABLED)]
        for model, mode in legs:
            run = adapter.ask(make_request(model, mode))
            total += run.cost_usd
            summarize(f"{model} · {mode.value}", run)
            if args.capture:
                args.capture.mkdir(parents=True, exist_ok=True)
                out = args.capture / f"grok_web_search.{model}.{mode.value}.json"
                out.write_text(json.dumps(run.raw_payload, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"  原始返回已写入 {out}")
    except AdapterError as exc:
        exit_code = 1
        status = getattr(exc, "http_status", None)
        body = (getattr(exc, "response_body", None) or "")[:500]
        print(f"grok_web_search: 契约异常 {type(exc).__name__} http_status={status}: {exc} {body}")
    finally:
        adapter.close()
    print(f"\n合计花费 ${total:.4f}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
